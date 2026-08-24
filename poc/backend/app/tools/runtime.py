"""The tool dispatcher — the single door between a model and this system.

Every tool call the model makes arrives here, and this function does the same
five things in the same order every time, whichever tool it is:

    1. count it against the run budget
    2. put it through `gate.check()`
    3. validate its arguments against the declared schema
    4. run the handler
    5. record the attempt, permitted or refused

Steps 1, 2 and 5 are not the handler's business, which is the point: a handler
cannot forget to check the gate, because it never had the opportunity. Adding a
fourteenth tool later means writing a function and a schema, not remembering a
checklist.

REFUSALS ARE RETURNED, NOT RAISED. A denied call comes back as a tool_result
with `is_error=True` and a plain-English reason. The model reads the refusal and
adapts, which is the behaviour we want, and every attempt stays visible in the
transcript and in `tool_call`. Raising instead would end the run and destroy the
evidence that something tried.

BUDGET EXHAUSTION IS THE EXCEPTION TO THAT. It propagates, because returning
"you are out of budget" to a model that can only respond by calling another tool
is how a run burns its remaining margin discovering it has none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlmodel import Session

from .. import store
from ..gate import TOOL_SPECS, Posture, RunContext, ToolDenied, confirmation_token
from ..models import ApprovalStatus, Confirmation
from ..policy import BudgetExceeded, RunBudget


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What goes back to the model as a tool_result block."""

    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolDef:
    """A handler plus the schema the model is shown.

    Both live in one object so they cannot drift: a parameter added to the
    handler without being added to the schema is a parameter the model can
    never supply, and `check_registry()` catches the reverse.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]


REGISTRY: dict[str, ToolDef] = {}


def register(name: str, description: str, input_schema: dict[str, Any]):
    """Declare a tool. The name must already exist in the gate's capability matrix."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name not in TOOL_SPECS:
            raise KeyError(
                f"tool {name!r} has a handler but no entry in gate.TOOL_SPECS. "
                "A tool with no capability matrix entry is a tool nobody is "
                "allowed to call, which is not what you meant."
            )
        REGISTRY[name] = ToolDef(name, description, input_schema, fn)
        return fn

    return wrap


def check_registry() -> None:
    """Every gated capability has a handler, and vice versa. Called by tests."""
    missing = set(TOOL_SPECS) - set(REGISTRY)
    extra = set(REGISTRY) - set(TOOL_SPECS)
    if missing or extra:
        raise RuntimeError(
            f"tool registry disagrees with the capability matrix. "
            f"no handler: {sorted(missing)}; not in matrix: {sorted(extra)}"
        )


# ---------------------------------------------------------------------------
# Schema validation
#
# The SDK sends these as strict tools, so the API enforces them server-side.
# We validate again here because "the API already checked" is only true for
# calls that came from the API — the same dispatcher runs in tests, in the
# replay harness and from the HITL endpoints, and those paths have no server
# in front of them.
# ---------------------------------------------------------------------------
def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Returns an error message, or None if the arguments satisfy the schema."""
    props = schema.get("properties", {})
    required = schema.get("required", [])

    missing = [k for k in required if k not in args or args[k] is None]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"

    unknown = [k for k in args if k not in props]
    if unknown:
        return (
            f"unknown argument(s): {', '.join(sorted(unknown))}. "
            f"Accepted: {', '.join(sorted(props))}"
        )

    for key, value in args.items():
        expected = props[key].get("type")
        if value is None or expected is None:
            continue
        if not _type_ok(expected, value):
            return f"{key!r} should be {expected}, got {type(value).__name__}"
        enum = props[key].get("enum")
        if enum and value not in enum:
            return f"{key!r} must be one of: {', '.join(map(str, enum))}"
        if expected == "integer":
            lo, hi = props[key].get("minimum"), props[key].get("maximum")
            if lo is not None and value < lo:
                return f"{key!r} must be >= {lo}"
            if hi is not None and value > hi:
                return f"{key!r} must be <= {hi}"
    return None


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


# ---------------------------------------------------------------------------
# Gated calls become a queue entry
# ---------------------------------------------------------------------------
def queue_confirmation(
    session: Session, tool: str, args: dict[str, Any], ctx: RunContext
) -> Confirmation:
    """Record that a gated call is waiting on a person.

    The gate tells the model its action "has been queued for approval". This is
    what makes that sentence true — without it the refusal would be a polite
    fiction and the order would simply never happen, which is worse than
    refusing outright because everyone believes it is pending.

    Keyed on the confirmation token, so a model that retries the identical call
    joins the existing queue entry instead of creating a second one.
    """
    token = confirmation_token(tool, args)
    existing = session.get(Confirmation, token)
    if existing is not None:
        return existing

    entry = Confirmation(
        token=token,
        run_id=ctx.run_id,
        loan_id=ctx.loan_id,
        tool=tool,
        args=args,
        requested_by=ctx.agent,
        status=ApprovalStatus.PENDING,
    )
    session.add(entry)
    session.flush()
    return entry


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    ctx: RunContext,
    session: Session,
    budget: RunBudget | None = None,
) -> ToolResult:
    """Run one tool call. Returns a result for the model; raises only on budget."""
    args = dict(args or {})
    started = time.perf_counter()
    spec = TOOL_SPECS.get(name)
    posture = str(spec.posture) if spec else "unknown"

    def record(ok: bool, denied: str | None = None, error: str | None = None) -> None:
        store.record_tool_call(
            session,
            run_id=ctx.run_id,
            loan_id=ctx.loan_id,
            agent=ctx.agent,
            tool=name,
            posture=posture,
            args=args,
            ok=ok,
            denied_reason=denied,
            error=error,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # 1 — budget. Counted before the call runs, so an over-budget run cannot
    #     squeeze in one more expensive tool on the way out.
    if budget is not None:
        budget.tool_calls += 1
        try:
            budget.check()
        except BudgetExceeded as exc:
            record(False, error=f"budget exceeded: {exc}")
            session.commit()
            raise

    # 2 — the gate
    try:
        gate_check(name, args, ctx)
    except ToolDenied as denied:
        if spec is not None and spec.posture is Posture.GATED:
            queue_confirmation(session, name, args, ctx)
        record(False, denied=denied.reason)
        session.commit()
        return ToolResult(f"DENIED: {denied.reason}", is_error=True)

    tool = REGISTRY.get(name)
    if tool is None:  # in the matrix, no handler — a wiring bug, not model error
        record(False, error="no handler registered")
        session.commit()
        return ToolResult(
            f"DENIED: {name} is not available in this deployment.", is_error=True
        )

    # 3 — arguments
    problem = validate_args(tool.input_schema, args)
    if problem:
        record(False, denied=problem)
        session.commit()
        return ToolResult(f"INVALID ARGUMENTS: {problem}", is_error=True)

    # 4 — the handler
    try:
        output = tool.handler(session=session, ctx=ctx, **args)
    except ToolDenied as denied:
        # A handler may refuse on grounds the gate cannot see — an exception
        # already closed, a repair on a HITL-lane finding. Same treatment.
        record(False, denied=denied.reason)
        session.commit()
        return ToolResult(f"DENIED: {denied.reason}", is_error=True)
    except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the run
        session.rollback()
        record(False, error=f"{type(exc).__name__}: {exc}")
        session.commit()
        return ToolResult(
            f"ERROR: {name} failed ({type(exc).__name__}). Do not retry the "
            "identical call; continue with the rest of your analysis.",
            is_error=True,
        )

    # 5 — record the success
    record(True)
    session.commit()
    return ToolResult(output if isinstance(output, str) else str(output))


def gate_check(name: str, args: dict[str, Any], ctx: RunContext) -> None:
    """Indirection so tests can assert the gate is consulted on every path."""
    from ..gate import check

    check(name, args, ctx)


# ---------------------------------------------------------------------------
# What the model is shown
# ---------------------------------------------------------------------------
# JSON Schema keywords the Messages API rejects on a tool definition:
#   "tools.5.custom: For 'integer' type, properties maximum, minimum are not supported"
#
# We keep them in the local schema — `validate_args` enforces them and the tests
# cover them — and strip them on the way out, folding the bound into the
# description so the model is still told the range. Losing the constraint
# entirely would mean a confidence of 140 reaching `decide_disposition()`, which
# raises; enforcing it locally turns that into a readable refusal instead.
_UNSUPPORTED_BY_API = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                       "multipleOf")


def _api_safe_property(prop: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in prop.items() if k not in _UNSUPPORTED_BY_API}
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if lo is not None or hi is not None:
        bound = (f"{lo} to {hi}" if lo is not None and hi is not None
                 else f"at least {lo}" if lo is not None else f"at most {hi}")
        desc = out.get("description", "")
        out["description"] = f"{desc} ({bound})".strip() if desc else bound
    return out


def tool_schemas_for(agent: str, *, strict: bool = True) -> list[dict[str, Any]]:
    """The tool definitions for one agent, in a stable order.

    Order matters for cost, not correctness: the tool list is serialised ahead
    of the system prompt, so reordering it invalidates the prompt cache for
    every subsequent request. Sorted by name, always.

    The returned schemas are deep copies. A shallow copy here would let a
    stripped keyword mutate the registry's own schema, so the local validator
    would quietly lose the same bound it is supposed to enforce.
    """
    from ..gate import tools_for_agent

    out: list[dict[str, Any]] = []
    for spec in tools_for_agent(agent):
        tool = REGISTRY.get(spec.name)
        if tool is None:
            continue
        schema = dict(tool.input_schema)
        schema["properties"] = {
            name: _api_safe_property(prop)
            for name, prop in tool.input_schema.get("properties", {}).items()
        }
        if "required" in schema:
            schema["required"] = list(schema["required"])
        if strict:
            schema["additionalProperties"] = False
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": schema,
                **({"strict": True} if strict else {}),
            }
        )
    return out
