"""Disposition policy and system invariants.

This is the whole safety posture of the POC, in one file, under test.

Two principles it exists to enforce:

  1. THE MODEL DOES NOT CHOOSE THE DISPOSITION. An agent returns a finding with
     a confidence, a severity and its evidence. `decide_disposition()` — plain
     Python, no model — maps those to auto or HITL. Keeping the threshold here
     means it can be tuned against ground truth, unit-tested, and reviewed by
     someone who does not read prompts.

  2. CORRECTION IS ONE-DIRECTIONAL. Confidence may be revised down, never up,
     and a finding may move auto -> HITL, never HITL -> auto. If a second pass
     could talk a finding from 62% to 89%, the threshold would be decorative
     and the auto lane would silently widen. See `revise_confidence()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Severity(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class Lane(StrEnum):
    AUTO = "auto"
    HITL = "hitl"


@dataclass(frozen=True, slots=True)
class Disposition:
    lane: Lane
    requires_sup: bool
    reason: str


# ---------------------------------------------------------------------------
# Thresholds — per exception type, never one global number.
#
# "88% sure a document is missing" and "88% sure this income is right" are not
# the same claim, and must not share a threshold. Every value here is a
# starting point to be tuned against ground_truth.json in the evaluation pass;
# none of them is a guess we should still be running on at go-live.
# ---------------------------------------------------------------------------
AUTO_THRESHOLD: dict[str, int] = {
    "missing_document": 88,
    "expired_document": 88,
    "low_confidence_ocr": 85,
    "flood_determination_mismatch": 90,
    "flood_cert_missing": 90,
    "fee_tolerance_variance": 92,
}

# Types that are never automatic regardless of how confident the model is.
# These are judgment calls about policy, identity or collateral — the machine
# may analyse them, but a person decides.
NEVER_AUTO: frozenset[str] = frozenset(
    {
        "income_variance",
        "dti_breach",
        "ltv_breach",
        "title_exception",
        "identity_mismatch",
        "appraisal_variance",
        "undisclosed_debt",
        "unsourced_deposit",
        "aus_referral",
        # A document that tries to instruct the system is never a mechanical
        # fix. It is either an attack or a borrower who pasted something odd,
        # and telling those apart is exactly what a person is for.
        "prompt_injection",
    }
)

# Types where a human may only *propose* — a supervisor signs off before the
# fix is applied. Threshold breaches and anything Critical.
REQUIRES_SUPERVISOR: frozenset[str] = frozenset(
    {"dti_breach", "ltv_breach", "title_exception", "identity_mismatch",
     "prompt_injection"}
)

# Every type this system has a disposition rule for. `raise_exception` offers
# these as an enum so an agent cannot accidentally name a finding after the rule
# that produced it -- which happened, twice, in one run: `income_employment` and
# `trid_fee_tolerance` are rule ids, and both landed as unknown types.
#
# `other` is deliberately still allowed alongside them. Foreclosing novelty
# would be worse: a finding the vocabulary has no word for should be sayable,
# and it routes to a human either way.
KNOWN_EXCEPTION_TYPES: frozenset[str] = frozenset(AUTO_THRESHOLD) | NEVER_AUTO

# A confidence no unknown type can reach, so an unrecognised finding routes to
# a human *structurally* rather than by luck.
_UNREACHABLE = 101


def decide_disposition(
    exception_type: str, severity: Severity | str, confidence: int
) -> Disposition:
    """Map a finding onto a lane. Pure function, no I/O, no model."""
    sev = Severity(severity)

    if not 0 <= confidence <= 100:
        raise ValueError(f"confidence out of range: {confidence}")

    if sev is Severity.CRITICAL:
        return Disposition(Lane.HITL, True, "Critical severity is never automatic")

    if exception_type in NEVER_AUTO:
        return Disposition(
            Lane.HITL,
            exception_type in REQUIRES_SUPERVISOR,
            f"{exception_type} is a judgment call, never auto-dispositioned",
        )

    threshold = AUTO_THRESHOLD.get(exception_type, _UNREACHABLE)
    if exception_type not in AUTO_THRESHOLD:
        return Disposition(
            Lane.HITL, False, f"unknown exception type {exception_type!r} defaults to a human"
        )

    if confidence >= threshold:
        return Disposition(
            Lane.AUTO, False, f"confidence {confidence} >= {threshold} for {exception_type}"
        )

    return Disposition(
        Lane.HITL, False, f"confidence {confidence} < {threshold} for {exception_type}"
    )


# ---------------------------------------------------------------------------
# One-directional correction
# ---------------------------------------------------------------------------
def revise_confidence(original: int, revised: int, source: str) -> int:
    """Apply a corrected confidence. Downward only.

    Deterministic checks (an evidence quote that does not appear in the cited
    document, an arithmetic result that disagrees with the model) may lower a
    confidence. Nothing may raise one — not a second model pass, not a retry,
    not a reflection step.
    """
    if revised > original:
        raise ConfidenceRaised(
            f"{source} tried to raise confidence {original} -> {revised}. "
            "Confidence is monotonically non-increasing; the first pass is the ceiling."
        )
    return revised


def revise_disposition(current: Disposition, proposed: Disposition) -> Disposition:
    """Apply a corrected disposition. Toward the human only."""
    if current.lane is Lane.HITL and proposed.lane is Lane.AUTO:
        raise DispositionWidened(
            "a correction pass tried to move a finding from HITL to auto; "
            "corrections may only route toward a human"
        )
    return proposed


class ConfidenceRaised(Exception):
    """Raised when something attempts to increase a confidence score."""


class DispositionWidened(Exception):
    """Raised when something attempts to move a finding from HITL to auto."""


# ---------------------------------------------------------------------------
# Invariants — re-checked after every write, rolled back on violation
# ---------------------------------------------------------------------------
class InvariantViolation(Exception):
    """A post-condition failed. The transaction must roll back."""


AGENT_ACTORS: frozenset[str] = frozenset(
    {"intake", "validation", "processing", "orchestration", "supervisor_agent", "summarizer"}
)

_OPEN_STATUSES = {"predicted", "repairing", "routed", "inqueue", "pending"}
_CLOSED_STATUSES = {"resolved", "approved"}


def assert_exception_invariants(exc) -> None:
    """Invariants that must hold for a single exception after any write."""
    if exc.requires_sup and exc.status == "resolved" and exc.resolved_by in AGENT_ACTORS:
        raise InvariantViolation(
            f"{exc.id}: requires supervisor sign-off but was resolved by agent "
            f"{exc.resolved_by!r}. Must go through propose -> approve."
        )

    if exc.lane == Lane.AUTO and exc.requires_sup:
        raise InvariantViolation(
            f"{exc.id}: cannot be auto-lane and require supervisor sign-off"
        )

    if exc.status in _CLOSED_STATUSES and not exc.evidence_doc_id:
        raise InvariantViolation(
            f"{exc.id}: closed without evidence traceable to a document"
        )


def assert_loan_invariants(loan, exceptions: list) -> None:
    """Invariants that must hold for a loan after any write."""
    gating = [e for e in exceptions if e.stage <= 2]
    open_gating = [e for e in gating if e.status in _OPEN_STATUSES]

    if loan.ready and open_gating:
        raise InvariantViolation(
            f"{loan.id}: marked ready with {len(open_gating)} open gating exception(s): "
            f"{[e.id for e in open_gating]}"
        )

    if loan.decision and not loan.ready:
        raise InvariantViolation(f"{loan.id}: decisioned without ever being ready")


def is_ready(loan, exceptions: list) -> bool:
    """Readiness is DERIVED, never assigned. Re-evaluated after every action."""
    if not loan.scanned:
        return False
    gating = [e for e in exceptions if e.stage <= 2]
    if not gating:
        return True
    all_clear = all(e.status in _CLOSED_STATUSES or e.status == "idle" for e in gating)
    any_started = any(e.status != "idle" for e in gating)
    return all_clear and any_started


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
class BudgetExceeded(Exception):
    """A run hit a ceiling. Fail cleanly and audit rather than keep spending."""


@dataclass(slots=True)
class RunBudget:
    max_tool_calls: int
    max_tokens: int
    max_seconds: int
    max_usd: float

    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0

    price_in: float = 5.00   # USD per 1M input tokens, Opus 4.7
    price_out: float = 25.00  # USD per 1M output tokens

    @property
    def usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * self.price_in
            + self.output_tokens / 1_000_000 * self.price_out
        )

    def check(self) -> None:
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded(f"tool calls {self.tool_calls} > {self.max_tool_calls}")
        total_tokens = self.input_tokens + self.output_tokens
        if total_tokens > self.max_tokens:
            raise BudgetExceeded(f"tokens {total_tokens} > {self.max_tokens}")
        if self.elapsed_seconds > self.max_seconds:
            raise BudgetExceeded(
                f"elapsed {self.elapsed_seconds:.1f}s > {self.max_seconds}s"
            )
        if self.usd > self.max_usd:
            raise BudgetExceeded(f"spend ${self.usd:.4f} > ${self.max_usd:.2f}")
