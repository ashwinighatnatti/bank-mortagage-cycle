"""The tool gate. Every tool call passes through `check()` before it runs.

This is the primary security control, not the UI and not the prompt. If a
prompt-injected document persuades the Validation Agent to repair an exception
instead of raising one, the gate denies it — that agent does not hold that
capability, and no amount of persuasive text in a PDF changes the matrix below.

Denials are returned to the model as a tool_result with is_error=True and a
plain-English reason, never dropped silently. The model reads the refusal and
adapts, which is the behaviour we want and also makes every attempt visible in
the transcript and the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Posture(StrEnum):
    """What a tool is capable of doing. Drives gating, logging and UI colour."""

    READ = "read"    # cannot change anything
    WRITE = "write"  # mutates our own state
    GATED = "gated"  # costs money or contacts a human — needs confirmation


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    posture: Posture
    agents: frozenset[str]
    description: str


# ---------------------------------------------------------------------------
# The capability matrix.
#
# Read it as: "which agents may call this tool". Deliberately narrow —
# Validation may raise a finding but never repair one; Processing may repair
# but never raise; the Summarizer holds read-only tools and nothing else.
# Separation of duties between agents, for the same reason it exists between
# people.
# ---------------------------------------------------------------------------
TOOL_SPECS: dict[str, ToolSpec] = {
    # --- read -------------------------------------------------------------
    "list_documents": ToolSpec(
        "list_documents", Posture.READ,
        frozenset({"intake", "validation", "processing", "summarizer"}),
        "Documents on file for a loan, with type and page count.",
    ),
    "read_document": ToolSpec(
        "read_document", Posture.READ,
        frozenset({"intake", "validation", "summarizer"}),
        "Full text of one document, delimited as untrusted data.",
    ),
    "get_loan": ToolSpec(
        "get_loan", Posture.READ,
        frozenset({"intake", "validation", "processing", "summarizer"}),
        "Loan header: borrowers, program, amount, FICO, DTI, LTV.",
    ),
    "get_extracted_fields": ToolSpec(
        "get_extracted_fields", Posture.READ,
        frozenset({"validation", "processing", "summarizer"}),
        "Previously extracted fields with their confidences.",
    ),
    "evaluate_rule": ToolSpec(
        "evaluate_rule", Posture.READ, frozenset({"validation"}),
        "Run one deterministic rule; returns pass / fail / indeterminate.",
    ),
    "lookup_guideline": ToolSpec(
        "lookup_guideline", Posture.READ, frozenset({"validation", "summarizer"}),
        "Retrieve the guideline passage for a program and topic.",
    ),
    "recall_notes": ToolSpec(
        "recall_notes", Posture.READ, frozenset({"validation", "processing"}),
        "Operational memory relevant to this exception type.",
    ),
    # Added after the first real agent run. The Processing Agent held
    # apply_auto_repair, which needs an exception id, and nothing that could
    # produce one -- so it correctly reported that it could not act and did
    # nothing. A capability with no way to discover its own subject is not a
    # capability. Validation holds it too, to see what has already been raised
    # rather than raising a duplicate.
    "list_exceptions": ToolSpec(
        "list_exceptions", Posture.READ,
        frozenset({"validation", "processing", "summarizer"}),
        "Exceptions on this loan with their lane, queue and status.",
    ),
    # --- write ------------------------------------------------------------
    "record_extraction": ToolSpec(
        "record_extraction", Posture.WRITE, frozenset({"intake"}),
        "Persist an extracted field with confidence and source location.",
    ),
    "raise_exception": ToolSpec(
        "raise_exception", Posture.WRITE, frozenset({"validation"}),
        "Emit a finding. The Validation Agent's primary output.",
    ),
    "apply_auto_repair": ToolSpec(
        "apply_auto_repair", Posture.WRITE, frozenset({"processing"}),
        "Execute a repair. Refused unless policy placed the exception in the auto lane.",
    ),
    # --- gated ------------------------------------------------------------
    "order_vendor_service": ToolSpec(
        "order_vendor_service", Posture.GATED, frozenset({"processing"}),
        "Order title / appraisal / flood / credit. Spends money.",
    ),
    "request_borrower_document": ToolSpec(
        "request_borrower_document", Posture.GATED, frozenset({"processing"}),
        "Contact a borrower. Irreversible.",
    ),
}


class ToolDenied(Exception):
    """A tool call was refused by policy. Returned to the model, not raised to the user."""

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool} denied: {reason}")


@dataclass(slots=True)
class RunContext:
    """Everything the gate needs to decide. Carried per agent run."""

    run_id: str
    agent: str
    loan_id: str
    confirmations: set[str] = field(default_factory=set)
    open: bool = True


def check(tool_name: str, kwargs: dict, ctx: RunContext) -> None:
    """Raise ToolDenied if this call is not permitted. Otherwise return."""

    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        raise ToolDenied(tool_name, "no such tool is registered")

    # 1 — capability: may this agent call this tool at all?
    if ctx.agent not in spec.agents:
        raise ToolDenied(
            tool_name,
            f"the {ctx.agent} agent does not hold this capability "
            f"(held by: {', '.join(sorted(spec.agents))})",
        )

    # 2 — a closed run may not mutate anything
    if spec.posture is not Posture.READ and not ctx.open:
        raise ToolDenied(tool_name, f"run {ctx.run_id} is closed; writes are refused")

    # 3 — scope: an agent may only touch the loan its run is scoped to
    target = kwargs.get("loan_id")
    if target and target != ctx.loan_id:
        raise ToolDenied(
            tool_name,
            f"run is scoped to {ctx.loan_id}; refusing to act on {target}",
        )

    # 4 — gated tools need an explicit human confirmation for this exact call
    if spec.posture is Posture.GATED:
        token = confirmation_token(tool_name, kwargs)
        if token not in ctx.confirmations:
            raise ToolDenied(
                tool_name,
                "this action spends money or contacts a borrower and requires "
                "human confirmation. It has been queued for approval — continue "
                "with the rest of your analysis and do not retry.",
            )


def confirmation_token(tool_name: str, kwargs: dict) -> str:
    """Stable id for one specific gated call.

    Confirmation is bound to the exact arguments, so approving one appraisal
    order does not approve a second, different one.
    """
    parts = [tool_name] + [f"{k}={kwargs[k]!r}" for k in sorted(kwargs)]
    return "|".join(parts)


def tools_for_agent(agent: str) -> list[ToolSpec]:
    """The tool surface a given agent sees. Keep the order stable — the tool
    list is rendered before the system prompt, so reordering it invalidates
    the prompt cache for every subsequent request."""
    return [s for _, s in sorted(TOOL_SPECS.items()) if agent in s.agents]
