"""The four agents, and the pipeline that runs them over one loan.

ORDER IS A DATA DEPENDENCY, NOT A PREFERENCE. Validation needs the fields
Intake extracted or every rule returns INDETERMINATE. Processing needs the
findings Validation raised or it has nothing to work. The Summarizer needs both
or it describes an unexamined file as a clean one.

Each agent gets its own run id and its own budget. A validation run that burns
its ceiling does not leave processing with nothing, and the audit trail shows
which agent spent what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlmodel import Session

from .. import store
from ..models import ExceptionRecord, ExceptionStatus, Loan
from ..policy import RunBudget
from .prompts import SYSTEM_PROMPTS, TASKS
from .runner import (
    MAX_TURNS,
    AgentResult,
    Event,
    build_system,
    get_client,
    run_agent,
    set_client,
)

# The order the pipeline runs in. Not configurable — see the module docstring.
PIPELINE: tuple[str, ...] = ("intake", "validation", "processing", "summarizer")

# HITL exceptions still waiting on a human — an agent run cannot move these.
_OPEN_HITL_STATUSES = (ExceptionStatus.ROUTED, ExceptionStatus.INQUEUE, ExceptionStatus.PENDING)


@dataclass(slots=True)
class PipelineResult:
    loan_id: str
    results: list[AgentResult] = field(default_factory=list)
    ready: bool = False
    summary: str = ""

    @property
    def usd(self) -> float:
        return sum(r.budget.usd for r in self.results)

    @property
    def tool_calls(self) -> int:
        return sum(r.tool_calls for r in self.results)

    @property
    def cache_read_tokens(self) -> int:
        return 0  # persisted per run; see the Run rows

    @property
    def events(self) -> list[Event]:
        return [e for r in self.results for e in r.events]

    @property
    def failed(self) -> list[AgentResult]:
        return [r for r in self.results if r.stopped in ("error", "budget")]


def mark_scanned(session: Session, loan_id: str, actor: str, run_id: str) -> None:
    """Record that intake has been through the file.

    `policy.is_ready()` returns False for an unscanned loan regardless of its
    exceptions, so this is what distinguishes "no findings because it is clean"
    from "no findings because nobody looked".
    """
    loan = session.get(Loan, loan_id)
    if loan is None or loan.scanned:
        return
    with store.guarded_write(
        session, loan_id=loan_id, actor=actor, role="Document Intake Agent",
        kind="ai", action="mark_scanned", run_id=run_id,
    ):
        loan.scanned = True
        session.add(loan)


def persist_summary(session: Session, loan_id: str, *, text: str, actor: str, run_id: str) -> None:
    """Save the Summarizer's narrative so it survives past the live log.

    Every run overwrites the prior text — unlike `scanned`, this isn't a
    one-way flag; it should reflect the latest read of the file.
    """
    loan = session.get(Loan, loan_id)
    if loan is None:
        return
    with store.guarded_write(
        session, loan_id=loan_id, actor=actor, role="Summarizer Agent",
        kind="ai", action="persist_summary", run_id=run_id,
    ):
        loan.summary = text
        session.add(loan)


def select_agents_for_loan(loan: Loan, excs: list[ExceptionRecord]) -> tuple[str, ...]:
    """What agent work, if any, is still outstanding for this loan right now.

    Never returns "validation" once the loan has been scanned: `raise_exception`
    has no dedup guard, so re-running validation against an already-scanned loan
    can duplicate findings. `processing` and `summarizer` are safe to repeat —
    `apply_auto_repair` refuses anything that is not open and auto-lane, and the
    summarizer never writes.
    """
    if not loan.scanned:
        return PIPELINE
    if any(e.status == ExceptionStatus.PREDICTED for e in excs):
        return ("processing", "summarizer")
    return ()


def outstanding_work_message(loan: Loan, excs: list[ExceptionRecord]) -> str:
    """Explain why `select_agents_for_loan` returned nothing to run."""
    open_hitl = [e for e in excs if e.status in _OPEN_HITL_STATUSES]
    if open_hitl:
        return f"Nothing for the agents to do — {len(open_hitl)} item(s) waiting on a human queue."
    if loan.ready:
        return "This loan is ready for underwriting — no outstanding AI work."
    return "No outstanding AI work for this loan."


def run_pipeline(
    session: Session,
    loan_id: str,
    *,
    run_prefix: str,
    client: Any = None,
    agents: tuple[str, ...] = PIPELINE,
    budget_per_agent: Callable[[str], RunBudget] | None = None,
    max_turns: int = MAX_TURNS,
    on_event: Callable[[Event], None] | None = None,
) -> PipelineResult:
    """Run the agents over one loan, in order, each with its own run and budget."""
    out = PipelineResult(loan_id=loan_id)

    for agent in agents:
        result = run_agent(
            session,
            agent=agent,
            loan_id=loan_id,
            run_id=f"{run_prefix}-{agent[:4].upper()}",
            client=client,
            budget=budget_per_agent(agent) if budget_per_agent else None,
            max_turns=max_turns,
            on_event=on_event,
        )
        out.results.append(result)

        if agent == "intake" and result.stopped != "error":
            # Even a run that hit its budget looked at the file, and readiness
            # is gated on other things besides this flag.
            mark_scanned(session, loan_id, actor=agent, run_id=result.run_id)
        if agent == "summarizer":
            out.summary = result.text
            if result.stopped != "error" and result.text:
                persist_summary(session, loan_id, text=result.text, actor=agent,
                                run_id=result.run_id)

    loan = session.get(Loan, loan_id)
    out.ready = bool(loan and loan.ready)
    return out


__all__ = [
    "PIPELINE", "AgentResult", "Event", "PipelineResult", "SYSTEM_PROMPTS", "TASKS",
    "build_system", "get_client", "mark_scanned", "outstanding_work_message",
    "persist_summary", "run_agent", "run_pipeline", "select_agents_for_loan",
    "set_client",
]
