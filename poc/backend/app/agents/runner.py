"""The agent loop — one function that runs any of the four agents.

WHY THIS IS A MANUAL LOOP AND NOT THE SDK TOOL RUNNER.

The spike concluded "use the SDK tool runner". That was right about the runner
working on Foundry and wrong about it fitting this system, for three reasons
that only became visible once the tool layer existed:

  1. The runner builds tool schemas from decorated function signatures. We have
     twelve hand-written strict schemas with enums and per-agent surfaces from
     `gate.tools_for_agent()`, and 43 tests over them. Adopting the runner would
     mean two sources of truth for every schema.

  2. A refusal has to come back as `is_error: true`. The gate's whole shape is
     "denied, here is why, adapt" — a decorated function returning a string
     cannot mark its result as an error.

  3. The budget must be able to abort mid-loop. The runner owns the loop, so a
     ceiling reached on turn four is discovered on turn five at the earliest.

The SDK docs name exactly this case — "request shapes the SDK cannot build" —
as the reason to drop to a manual loop. Everything else the spike established
(adaptive thinking, strict tools, prompt caching, `messages.parse`) is used.

WHAT THIS LOOP GUARANTEES

Every tool call goes through `tools.dispatch()`, so the gate, the budget, the
audit trail and the tool-call record apply to model-initiated calls exactly as
they do to the scripted ones. There is no faster path for the model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlmodel import Session, select

from .. import guidelines, store, tools
from ..config import get_settings
from ..gate import RunContext
from ..models import ApprovalStatus, Confirmation, Loan
from ..policy import BudgetExceeded, RunBudget
from .prompts import SYSTEM_PROMPTS, TASKS

# One turn is one request. Twelve is generous for these tasks and low enough
# that a loop stuck in a retry cycle stops rather than grinding at the budget.
MAX_TURNS = 12
MAX_TOKENS = 8_000


@dataclass(slots=True)
class Event:
    """One line of the live agent log."""

    agent: str
    kind: str          # say | tool | denied | error | done
    text: str
    tool: str | None = None
    ok: bool = True


@dataclass(slots=True)
class AgentResult:
    agent: str
    loan_id: str
    run_id: str
    text: str
    turns: int
    stopped: str       # end_turn | budget | max_turns | max_tokens | error
    budget: RunBudget
    events: list[Event] = field(default_factory=list)
    error: str | None = None

    @property
    def tool_calls(self) -> int:
        return self.budget.tool_calls


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_client: Any = None


def get_client() -> Any:
    """The Foundry client, built once from settings.

    `foundry_client_kwargs()` returns exactly one of `resource` or `base_url` —
    the SDK rejects both together.
    """
    global _client
    if _client is None:
        from anthropic import AnthropicFoundry

        settings = get_settings()
        _client = AnthropicFoundry(
            api_key=settings.foundry_api_key, **settings.foundry_client_kwargs()
        )
    return _client


def set_client(client: Any) -> None:
    """Inject a client. Tests use this to run the loop with no network."""
    global _client
    _client = client


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
def build_system(agent: str, program: str) -> list[dict[str, Any]]:
    """The two stable system blocks, with the cache breakpoint on the second.

    Order is deliberate and the bytes must not vary per loan. The agent prompt
    is fixed per agent; the guideline pack is fixed per program. The breakpoint
    sits at the end of the pack, which is the end of everything stable — so the
    second FHA loan of a run reads both blocks from cache.

    Anything loan-specific goes in `messages`, after this. Putting one loan id
    up here would not error; it would silently stop the cache from ever hitting
    and nothing but the bill would show it.
    """
    return [
        {"type": "text", "text": SYSTEM_PROMPTS[agent]},
        {
            "type": "text",
            "text": guidelines.build_context_pack(program),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def approved_confirmations(session: Session, loan_id: str) -> set[str]:
    """Confirmation tokens a human has approved for this loan.

    This is how a supervisor's approval reaches the agent: the gated call was
    refused and queued on an earlier run, a person approved it, and the token is
    now in the run context so the identical call succeeds this time.
    """
    rows = session.exec(
        select(Confirmation).where(
            Confirmation.loan_id == loan_id,
            Confirmation.status == ApprovalStatus.APPROVED,
        )
    ).all()
    return {r.token for r in rows}


def _text_of(content: Sequence[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def run_agent(
    session: Session,
    *,
    agent: str,
    loan_id: str,
    run_id: str,
    task: str | None = None,
    client: Any = None,
    budget: RunBudget | None = None,
    max_turns: int = MAX_TURNS,
    on_event: Callable[[Event], None] | None = None,
) -> AgentResult:
    """Run one agent over one loan until it stops calling tools."""
    if agent not in SYSTEM_PROMPTS:
        raise KeyError(f"unknown agent {agent!r}; known: {', '.join(SYSTEM_PROMPTS)}")

    settings = get_settings()
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")

    client = client or get_client()
    budget = budget or RunBudget(
        max_tool_calls=settings.max_tool_calls_per_run,
        max_tokens=settings.max_tokens_per_run,
        max_seconds=settings.max_run_seconds,
        max_usd=settings.max_usd_per_run,
        price_in=settings.price_input_per_mtok,
        price_out=settings.price_output_per_mtok,
    )

    ctx = RunContext(
        run_id=run_id,
        agent=agent,
        loan_id=loan_id,
        confirmations=approved_confirmations(session, loan_id),
    )
    store.open_run(session, run_id=run_id, agent=agent, loan_id=loan_id)
    session.commit()

    events: list[Event] = []

    def emit(kind: str, text: str, tool: str | None = None, ok: bool = True) -> None:
        ev = Event(agent=agent, kind=kind, text=text, tool=tool, ok=ok)
        events.append(ev)
        if on_event:
            try:
                on_event(ev)
            except Exception:  # noqa: BLE001
                # A display callback must never end an agent run. The first
                # real run died here: the summarizer emitted a right-arrow, the
                # Windows console could not encode it, and the UnicodeEncodeError
                # propagated into the loop and killed a run that had worked.
                pass

    system = build_system(agent, loan.program)
    tool_schemas = tools.tool_schemas_for(agent)
    prompt = task or TASKS[agent].format(
        loan_id=loan_id, program=loan.program, purpose=loan.purpose
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    started = time.perf_counter()
    stopped = "max_turns"
    final_text = ""
    error: str | None = None
    turns = 0

    try:
        for turns in range(1, max_turns + 1):
            response = client.messages.create(
                model=settings.model_id,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tool_schemas,
                messages=messages,
                # Adaptive is the only on-mode on Opus 4.7, and omitting the
                # parameter means no thinking at all — it has to be explicit.
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            )

            usage = response.usage
            budget.input_tokens += getattr(usage, "input_tokens", 0) or 0
            budget.output_tokens += getattr(usage, "output_tokens", 0) or 0
            budget.elapsed_seconds = time.perf_counter() - started
            _record_usage(session, run_id, usage, budget)
            budget.check()

            text = _text_of(response.content)
            if text:
                emit("say", text)

            if response.stop_reason == "max_tokens":
                stopped, final_text = "max_tokens", text
                break
            if response.stop_reason != "tool_use":
                stopped, final_text = "end_turn", text
                break

            messages.append({"role": "assistant", "content": response.content})

            # All results from one assistant turn go back in ONE user message.
            # Splitting them across messages trains the model out of making
            # parallel calls, which costs turns for the rest of the run.
            results: list[dict[str, Any]] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                result = tools.dispatch(
                    block.name, dict(block.input), ctx=ctx, session=session, budget=budget
                )
                emit(
                    "denied" if result.is_error else "tool",
                    result.content[:400],
                    tool=block.name,
                    ok=not result.is_error,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

    except BudgetExceeded as exc:
        stopped, error = "budget", str(exc)
        emit("error", f"budget stop: {exc}", ok=False)
    except Exception as exc:  # noqa: BLE001 — one agent failing must not kill a pipeline
        stopped, error = "error", f"{type(exc).__name__}: {exc}"
        emit("error", error, ok=False)

    budget.elapsed_seconds = time.perf_counter() - started
    store.close_run(
        session,
        run_id,
        status={"end_turn": "closed", "budget": "budget_exceeded"}.get(stopped, stopped),
        error=error,
    )
    session.commit()
    emit("done", f"{stopped} after {turns} turn(s), {budget.tool_calls} tool call(s), "
                 f"${budget.usd:.4f}")

    return AgentResult(
        agent=agent, loan_id=loan_id, run_id=run_id, text=final_text, turns=turns,
        stopped=stopped, budget=budget, events=events, error=error,
    )


def _record_usage(session: Session, run_id: str, usage: Any, budget: RunBudget) -> None:
    """Persist token spend after every turn, not at the end.

    A run that dies on turn six should still show what it spent on turns one to
    five. Writing usage only on a clean exit means the expensive failures are
    exactly the ones with no cost recorded.
    """
    from ..models import Run

    run = session.get(Run, run_id)
    if run is None:
        return
    run.input_tokens = budget.input_tokens
    run.output_tokens = budget.output_tokens
    run.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
    run.usd = budget.usd
    session.add(run)
    session.commit()
