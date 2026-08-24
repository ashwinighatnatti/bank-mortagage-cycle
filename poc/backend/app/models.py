"""The SQLModel schema — the state the twelve tools read and write.

Three things this schema is built to make structurally true, rather than true
by convention:

  1. DISPOSITION IS DERIVED, NOT STORED BY THE MODEL. `lane` and `requires_sup`
     are written only by `ExceptionRecord.from_finding()`, which calls
     `policy.decide_disposition()`. No tool handler sets them directly, so a
     model cannot talk its way into the auto lane by emitting `lane="auto"` in
     a structured output — the field is not part of the finding schema at all.

  2. EVERY CLOSED EXCEPTION CARRIES ITS EVIDENCE. `evidence_doc_id` is checked
     by `policy.assert_exception_invariants()` on close. A resolution that
     cannot name the document it came from is rejected, which is the difference
     between an audit trail and a log file.

  3. READINESS IS COMPUTED. `Loan.ready` exists as a column because the UI needs
     to query it, but it is only ever assigned from `policy.is_ready()`. See
     `store.recompute_readiness()`.

Field names here are load-bearing: `policy.assert_exception_invariants()`,
`assert_loan_invariants()` and `is_ready()` all read these attributes by name.
Renaming a column means changing policy.py in the same commit.

NO ORM RELATIONSHIPS, DELIBERATELY. Every link is a plain foreign-key column
and every read is an explicit query in store.py, so nothing lazy-loads a
borrower file into a context pack because a template touched an attribute. The
cost is that SQLAlchemy cannot infer insert order from a bare ForeignKey — it
flushes objects in the order they were added — so a caller creating a parent
and its children in one unit of work must `session.flush()` between them. See
`seed.seed_loans()`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from .policy import Lane, Severity, decide_disposition


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Status vocabulary
#
# A StrEnum whose *values* match the strings policy.py already tests against
# (_OPEN_STATUSES / _CLOSED_STATUSES). The enum exists so a typo becomes an
# AttributeError at import time rather than an exception that silently never
# closes because "resolvd" is in neither set.
# ---------------------------------------------------------------------------
class ExceptionStatus(StrEnum):
    IDLE = "idle"            # not yet examined
    PREDICTED = "predicted"  # a finding exists
    REPAIRING = "repairing"  # auto-repair in flight
    ROUTED = "routed"        # sent to a HITL queue
    INQUEUE = "inqueue"      # an analyst has picked it up
    PENDING = "pending"      # analyst proposed; awaiting supervisor
    RESOLVED = "resolved"    # closed by repair or analyst action
    APPROVED = "approved"    # closed by supervisor sign-off


class Decision(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_CONDITIONS = "approve-conditions"
    SUSPEND = "suspend"
    DENY = "deny"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Loan and its documents
# ---------------------------------------------------------------------------
class Loan(SQLModel, table=True):
    __tablename__ = "loan"

    id: str = Field(primary_key=True)
    borrowers: str
    metro: str
    program: str          # Conv | FHA | VA | Jumbo
    purpose: str          # Purchase | Refi
    amount: int
    property_value: int
    fico: int
    ltv: float
    dti: float
    note_rate: float
    monthly_income: float
    piti: float
    other_debts: float
    conforming_limit: int
    is_jumbo: bool = False

    # --- pipeline state — all derived, see store.py ------------------------
    scanned: bool = False
    ready: bool = False
    decision: Optional[str] = None
    decision_note: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    delivered: bool = False
    summary: Optional[str] = None


class Document(SQLModel, table=True):
    __tablename__ = "document"

    doc_id: str = Field(primary_key=True)
    loan_id: str = Field(foreign_key="loan.id", index=True)
    kind: str
    path: str
    chars: int


class ExtractedField(SQLModel, table=True):
    """One field the Intake Agent pulled out of one document.

    `confidence` here is EXTRACTION confidence on a 0-1 scale, not the 0-100
    disposition confidence on an exception. They are different scales with
    different consumers and must never be compared. A low extraction confidence
    is what *produces* a low_confidence_ocr finding; it is not itself a finding.
    """

    __tablename__ = "extracted_field"
    __table_args__ = (
        UniqueConstraint("loan_id", "name", "doc_id", name="uq_field_per_doc"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    loan_id: str = Field(foreign_key="loan.id", index=True)
    doc_id: str = Field(foreign_key="document.doc_id")
    name: str
    value: str
    confidence: float                       # 0.0 - 1.0
    source_span: Optional[str] = None       # where in the document it came from
    extracted_at: datetime = Field(default_factory=utcnow)
    run_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions — the centre of the whole system
# ---------------------------------------------------------------------------
class ExceptionRecord(SQLModel, table=True):
    """A finding, plus the disposition policy assigned to it.

    Note what an agent may NOT write: `lane`, `requires_sup`. Those come from
    `decide_disposition()` inside `from_finding()`. The model supplies the
    observation; Python supplies the consequence.
    """

    __tablename__ = "exception_record"
    __table_args__ = (Index("ix_exc_loan_stage", "loan_id", "stage"),)

    id: str = Field(primary_key=True)
    loan_id: str = Field(foreign_key="loan.id", index=True)
    stage: int                              # 0 intake · 1 processing · 2 AUS/risk · 3 closing
    exception_type: str
    label: str                              # human-readable, for the UI
    severity: str
    confidence: int                         # 0-100, monotonically non-increasing

    lane: str                               # POLICY-ASSIGNED
    requires_sup: bool                      # POLICY-ASSIGNED
    disposition_reason: str                 # POLICY-ASSIGNED, shown in the UI

    status: str = ExceptionStatus.PREDICTED
    queue: Optional[str] = None             # A | B | C, HITL only

    recommendation: Optional[str] = None
    rationale: Optional[str] = None
    evidence_quote: Optional[str] = None
    evidence_doc_id: Optional[str] = Field(default=None, foreign_key="document.doc_id")

    raised_by: Optional[str] = None
    raised_at: datetime = Field(default_factory=utcnow)
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None
    run_id: Optional[str] = None

    # Set when a deterministic check contradicted the model. Kept for the
    # calibration report — a system that never revises is not being checked.
    confidence_revised_from: Optional[int] = None
    revision_reason: Optional[str] = None

    @classmethod
    def from_finding(
        cls,
        *,
        id: str,
        loan_id: str,
        stage: int,
        exception_type: str,
        label: str,
        severity: Severity | str,
        confidence: int,
        recommendation: str | None = None,
        rationale: str | None = None,
        evidence_quote: str | None = None,
        evidence_doc_id: str | None = None,
        raised_by: str | None = None,
        run_id: str | None = None,
    ) -> "ExceptionRecord":
        """The only supported way to create an exception.

        Routing every creation through here is what makes "the model does not
        choose the disposition" a property of the schema rather than a habit of
        whoever wrote the tool handler.
        """
        disp = decide_disposition(exception_type, severity, confidence)
        # Routing is a consequence, not a decision. Once policy has put a
        # finding in the HITL lane and `queue_for()` has named the queue, there
        # is nothing left for anyone to choose — so the finding lands ROUTED
        # rather than sitting at PREDICTED waiting for an agent to route it.
        # Leaving that step to an agent would let it decline to take one, and a
        # finding nobody routed is a finding nobody sees.
        status = (
            ExceptionStatus.PREDICTED if disp.lane is Lane.AUTO else ExceptionStatus.ROUTED
        )
        return cls(
            id=id,
            loan_id=loan_id,
            stage=stage,
            exception_type=exception_type,
            label=label,
            severity=str(Severity(severity)),
            confidence=confidence,
            lane=str(disp.lane),
            requires_sup=disp.requires_sup,
            disposition_reason=disp.reason,
            status=status,
            queue=None if disp.lane is Lane.AUTO else queue_for(exception_type),
            recommendation=recommendation,
            rationale=rationale,
            evidence_quote=evidence_quote,
            evidence_doc_id=evidence_doc_id,
            raised_by=raised_by,
            run_id=run_id,
        )

    @property
    def is_open(self) -> bool:
        return self.status not in (ExceptionStatus.RESOLVED, ExceptionStatus.APPROVED)

    @property
    def is_gating(self) -> bool:
        """Stages 0-2 block underwriting readiness. Stage 3 is closing-side."""
        return self.stage <= 2


def queue_for(exception_type: str) -> str:
    """Which analyst queue a HITL exception lands in.

    A deterministic routing rule, not a model call. Queue A is income and
    capacity, B is collateral and title, C is documents and identity. Anything
    unrecognised goes to C, where a generalist triages it — an unknown type has
    to land somewhere a human definitely looks, not nowhere.
    """
    if exception_type in {"income_variance", "dti_breach", "aus_referral",
                          "undisclosed_debt", "unsourced_deposit"}:
        return "A"
    if exception_type in {"appraisal_variance", "ltv_breach", "title_exception",
                          "flood_determination_mismatch", "flood_cert_missing"}:
        return "B"
    # Documents and identity -- which is where a tampered document belongs.
    if exception_type == "prompt_injection":
        return "C"
    return "C"


# ---------------------------------------------------------------------------
# Supervisor approvals
# ---------------------------------------------------------------------------
class Approval(SQLModel, table=True):
    """An analyst proposal awaiting a supervisor.

    Exists because a `requires_sup` exception may not be closed by whoever
    proposed the fix — propose and approve are separate acts by separate
    actors, and `assert_exception_invariants()` enforces that the closer was
    not an agent.
    """

    __tablename__ = "approval"

    id: str = Field(primary_key=True)
    exception_id: str = Field(foreign_key="exception_record.id", index=True)
    loan_id: str = Field(foreign_key="loan.id", index=True)
    exception_type: str
    proposed_by: str
    proposed_action: str
    ai_recommendation: Optional[str] = None
    queue: Optional[str] = None
    status: str = ApprovalStatus.PENDING
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Runs, tool calls, confirmations
# ---------------------------------------------------------------------------
class Run(SQLModel, table=True):
    """One agent invocation, with its budget consumption."""

    __tablename__ = "run"

    run_id: str = Field(primary_key=True)
    agent: str
    loan_id: str = Field(foreign_key="loan.id", index=True)
    status: str = "open"                    # open | closed | failed | budget_exceeded
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    usd: float = 0.0
    error: Optional[str] = None


class ToolCall(SQLModel, table=True):
    """Every attempt, including the denied ones.

    Denials are the interesting rows. A Validation Agent repeatedly trying to
    call `apply_auto_repair` is either a prompt bug or an injection attempt,
    and neither is visible if refusals go unrecorded.
    """

    __tablename__ = "tool_call"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="run.run_id", index=True)
    loan_id: str
    agent: str
    tool: str
    posture: str
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ok: bool = True
    denied_reason: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    at: datetime = Field(default_factory=utcnow)


class Confirmation(SQLModel, table=True):
    """A human approval of one specific gated call.

    The primary key is `gate.confirmation_token()` — tool name plus exact
    arguments — so confirming an appraisal order for LN-2026-0003 does not
    confirm a title order, or the same order on a different loan.
    """

    __tablename__ = "confirmation"

    token: str = Field(primary_key=True)
    run_id: str
    loan_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    requested_by: str
    requested_at: datetime = Field(default_factory=utcnow)
    status: str = ApprovalStatus.PENDING
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Audit and operational memory
# ---------------------------------------------------------------------------
class AuditEntry(SQLModel, table=True):
    """One row of the hash chain. Append-only by discipline and by API.

    Column names match the payload keys `audit.build_entry()` produces, so a
    row round-trips through `audit.verify_chain()` without a translation layer
    that could quietly drop a field from the hashed set.
    """

    __tablename__ = "audit_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    at: str                                 # ISO-8601, exactly as hashed
    actor: str
    role: str
    kind: str                               # ai | human | system
    action: str
    case_id: str = Field(index=True)
    run_id: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # UNIQUE on prev_hash makes a forked chain structurally impossible: each
    # hash may be the predecessor of at most one row. Two concurrent writers
    # that both read the same tip get an IntegrityError on the second insert
    # instead of quietly producing two valid-looking branches — which
    # `verify_chain` would report as tampering long after the fact.
    prev_hash: str = Field(unique=True)
    hash: str = Field(index=True, unique=True)


class Note(SQLModel, table=True):
    """Operational memory, scoped by exception type. Read by `recall_notes`.

    Deliberately NOT free-form model memory: a note is written by a human
    resolution or a verified outcome, so memory cannot be poisoned by a model
    asserting something and later reading its own assertion back as an
    established fact.
    """

    __tablename__ = "note"

    id: Optional[int] = Field(default=None, primary_key=True)
    exception_type: str = Field(index=True)
    text: str
    source: str                             # human_resolution | verified_outcome
    loan_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


ALL_TABLES = (
    Loan, Document, ExtractedField, ExceptionRecord, Approval,
    Run, ToolCall, Confirmation, AuditEntry, Note,
)
