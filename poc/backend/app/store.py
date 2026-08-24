"""The guarded write path — the only place state changes.

Everything a tool handler wants to do to the database goes through here, for
three reasons:

  1. INVARIANTS ARE RE-CHECKED AFTER EVERY WRITE. `guarded_write()` runs the
     assertions in policy.py inside the transaction, so a violation rolls the
     write back rather than leaving the database in a state the rest of the
     system believes is impossible.

  2. NOTHING MUTATES WITHOUT AN AUDIT LINE. `guarded_write()` takes the audit
     arguments as required parameters. There is no code path that writes state
     and forgets to record who did it — not because everyone remembers, but
     because the signature will not let you.

  3. READINESS IS RECOMPUTED, NEVER ASSIGNED. Any write that touches an
     exception re-derives `Loan.ready` from `policy.is_ready()`.

Corrections are applied through `revise_finding()`, which routes through
`policy.revise_confidence()` and `revise_disposition()` — the one-directional
rules. Nothing else may write `confidence` or `lane` on an existing row.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import audit, documents, policy, rules
from .models import (
    AuditEntry,
    Document,
    ExceptionRecord,
    ExceptionStatus,
    ExtractedField,
    Loan,
    Note,
    Run,
    ToolCall,
)
from .policy import Disposition, Lane

# The chain tip is read then written. SQLite serialises writers across
# processes; this lock serialises them within one, so the common case never
# reaches the IntegrityError retry below.
_chain_lock = threading.Lock()

_MAX_CHAIN_RETRIES = 5


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def chain_tip(session: Session) -> str:
    """Hash of the newest audit row, or GENESIS if the chain is empty."""
    row = session.exec(
        select(AuditEntry).order_by(AuditEntry.id.desc()).limit(1)  # type: ignore[union-attr]
    ).first()
    return row.hash if row else audit.GENESIS


def append_audit(
    session: Session,
    *,
    actor: str,
    role: str,
    kind: audit.Kind,
    action: str,
    case_id: str,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> AuditEntry:
    """Append one row to the hash chain.

    `detail` is redacted before hashing, never after: once a value is inside a
    hash it cannot be removed without breaking every row that follows, so a
    leaked SSN in an audit payload would be permanent by construction.
    """
    payload_detail = audit.redact_detail(detail or {})

    for attempt in range(_MAX_CHAIN_RETRIES):
        with _chain_lock:
            entry_dict = audit.build_entry(
                chain_tip(session),
                actor=actor,
                role=role,
                kind=kind,
                action=action,
                case_id=case_id,
                run_id=run_id,
                detail=payload_detail,
                at=at,
            )
            # A savepoint, not session.rollback(): append_audit runs inside
            # guarded_write's own savepoint, and a full rollback here would
            # discard the caller's state changes as collateral damage on what
            # is meant to be a retryable contention error.
            entry = AuditEntry(**entry_dict)
            savepoint = session.begin_nested()
            try:
                session.add(entry)
                session.flush()
                savepoint.commit()
                return entry
            except IntegrityError:
                # Another writer claimed this tip. Drop our row and re-read.
                savepoint.rollback()
                if attempt == _MAX_CHAIN_RETRIES - 1:
                    raise
    raise RuntimeError("unreachable")


def verify_audit_chain(session: Session) -> tuple[bool, str | None]:
    """Walk the persisted chain in insertion order."""
    rows = session.exec(select(AuditEntry).order_by(AuditEntry.id)).all()  # type: ignore[arg-type]
    return audit.verify_chain([r.model_dump() for r in rows])


# ---------------------------------------------------------------------------
# Readiness — derived, never assigned
# ---------------------------------------------------------------------------
def exceptions_for(session: Session, loan_id: str) -> list[ExceptionRecord]:
    return list(
        session.exec(
            select(ExceptionRecord)
            .where(ExceptionRecord.loan_id == loan_id)
            .order_by(ExceptionRecord.stage, ExceptionRecord.id)  # type: ignore[arg-type]
        ).all()
    )


def recompute_readiness(session: Session, loan_id: str) -> Loan:
    """Re-derive `Loan.ready` from the current exception set.

    Called after every exception write. `ready` is a cached projection of
    `policy.is_ready()` so the UI can filter on it in SQL; it is never the
    source of truth and is never set by a caller.
    """
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")
    excs = exceptions_for(session, loan_id)
    loan.ready = policy.is_ready(loan, excs)
    session.add(loan)
    return loan


def assert_all_invariants(session: Session, loan_id: str) -> None:
    """Every invariant that touches this loan. Raises InvariantViolation."""
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")
    excs = exceptions_for(session, loan_id)
    for exc in excs:
        policy.assert_exception_invariants(exc)
    policy.assert_loan_invariants(loan, excs)


# ---------------------------------------------------------------------------
# The guarded write
# ---------------------------------------------------------------------------
@contextmanager
def guarded_write(
    session: Session,
    *,
    loan_id: str,
    actor: str,
    role: str,
    kind: audit.Kind,
    action: str,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> Iterator[Session]:
    """Mutate state inside this block. On exit: recompute, assert, audit, commit.

    On any invariant violation the whole block rolls back — including the audit
    line, which is correct: an action that did not survive its post-conditions
    did not happen, and recording that it did would make the trail describe a
    state the database is not in.
    """
    savepoint = session.begin_nested()
    try:
        yield session
        session.flush()
        recompute_readiness(session, loan_id)
        session.flush()
        assert_all_invariants(session, loan_id)
        append_audit(
            session,
            actor=actor,
            role=role,
            kind=kind,
            action=action,
            case_id=loan_id,
            run_id=run_id,
            detail=detail,
        )
        savepoint.commit()
    except Exception:
        if savepoint.is_active:
            savepoint.rollback()
        raise
    session.commit()


# ---------------------------------------------------------------------------
# Corrections — one-directional, enforced
# ---------------------------------------------------------------------------
def revise_finding(
    session: Session,
    exception_id: str,
    *,
    revised_confidence: int,
    reason: str,
    source: str,
) -> ExceptionRecord:
    """Lower a confidence and re-derive its disposition.

    The only supported way to change `confidence` or `lane` on an existing
    exception. Both directions are checked: confidence may not rise
    (`revise_confidence`), and the lane may not move toward auto
    (`revise_disposition`). Both raise rather than clamp — a silent clamp hides
    the bug that produced it.
    """
    exc = session.get(ExceptionRecord, exception_id)
    if exc is None:
        raise LookupError(f"no such exception: {exception_id}")

    original = exc.confidence
    new_conf = policy.revise_confidence(original, revised_confidence, source)

    current = Disposition(Lane(exc.lane), exc.requires_sup, exc.disposition_reason)
    proposed = policy.decide_disposition(exc.exception_type, exc.severity, new_conf)
    applied = policy.revise_disposition(current, proposed)

    exc.confidence_revised_from = original
    exc.revision_reason = f"{source}: {reason}"
    exc.confidence = new_conf
    exc.lane = str(applied.lane)
    exc.requires_sup = applied.requires_sup
    exc.disposition_reason = applied.reason
    if applied.lane is Lane.HITL:
        from .models import ExceptionStatus, queue_for

        if exc.queue is None:
            exc.queue = queue_for(exc.exception_type)
        # A finding pushed out of the auto lane by a correction must also land
        # in a queue. Lowering its confidence and leaving it at PREDICTED would
        # take it off the auto path without putting it on any other one.
        if exc.status == ExceptionStatus.PREDICTED:
            exc.status = ExceptionStatus.ROUTED
    session.add(exc)
    return exc


# ---------------------------------------------------------------------------
# Run and tool-call bookkeeping
# ---------------------------------------------------------------------------
def open_run(session: Session, *, run_id: str, agent: str, loan_id: str) -> Run:
    run = Run(run_id=run_id, agent=agent, loan_id=loan_id, status="open")
    session.add(run)
    session.flush()
    return run


def close_run(
    session: Session, run_id: str, *, status: str = "closed", error: str | None = None
) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(f"no such run: {run_id}")
    run.status = status
    run.error = error
    run.ended_at = datetime.now(timezone.utc)
    session.add(run)
    return run


def record_tool_call(
    session: Session,
    *,
    run_id: str,
    loan_id: str,
    agent: str,
    tool: str,
    posture: str,
    args: dict[str, Any],
    ok: bool = True,
    denied_reason: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> ToolCall:
    """Record an attempt — permitted or refused.

    Refusals are recorded with the same weight as successes. A run full of
    denials is the signal that an agent is being pushed at capabilities it does
    not hold, and it is only a signal if it is written down.
    """
    call = ToolCall(
        run_id=run_id,
        loan_id=loan_id,
        agent=agent,
        tool=tool,
        posture=posture,
        args=audit.redact_detail(args),
        ok=ok,
        denied_reason=denied_reason,
        error=error,
        duration_ms=duration_ms,
    )
    session.add(call)
    run = session.get(Run, run_id)
    if run is not None:
        run.tool_calls += 1
        session.add(run)
    return call


# ---------------------------------------------------------------------------
# Reads used by the tool layer
# ---------------------------------------------------------------------------
def documents_for(session: Session, loan_id: str) -> Sequence[Document]:
    return session.exec(
        select(Document).where(Document.loan_id == loan_id).order_by(Document.doc_id)  # type: ignore[arg-type]
    ).all()


def notes_for(session: Session, exception_type: str, limit: int = 5) -> Sequence[Note]:
    return session.exec(
        select(Note)
        .where(Note.exception_type == exception_type)
        .order_by(Note.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    ).all()


def build_facts(session: Session, loan_id: str) -> rules.Facts:
    """Snapshot the loan into the input a rule is allowed to see.

    Where two documents produced the same field name, the higher-confidence
    extraction wins. Ties keep the first by doc_id, so the snapshot is stable
    across runs — a rules engine that returns different answers on the same
    data cannot be evaluated.
    """
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")

    docs = documents_for(session, loan_id)
    extracted = session.exec(
        select(ExtractedField)
        .where(ExtractedField.loan_id == loan_id)
        .order_by(ExtractedField.doc_id)  # type: ignore[arg-type]
    ).all()

    best: dict[str, ExtractedField] = {}
    for f in extracted:
        current = best.get(f.name)
        if current is None or f.confidence > current.confidence:
            best[f.name] = f

    # Withhold anything the extractor itself barely believed. A 0.35-confidence
    # reading of a blurred W-2 is not a fact, and letting one through once
    # produced a "+96,547% income variance" that an agent had to spend a turn
    # explaining. Withheld names are recorded so the rule can report the
    # difference between "not on file" and "on file, not readable".
    trusted = {n: f for n, f in best.items()
               if f.confidence >= rules.EXTRACTION_CONFIDENCE_FLOOR}
    withheld = {n: f.confidence for n, f in best.items()
                if f.confidence < rules.EXTRACTION_CONFIDENCE_FLOOR}

    return rules.Facts(
        loan_id=loan.id,
        program=loan.program,
        purpose=loan.purpose,
        metro=loan.metro,
        amount=float(loan.amount),
        property_value=float(loan.property_value),
        fico=loan.fico,
        stated_ltv=loan.ltv,
        stated_dti=loan.dti,
        monthly_income=loan.monthly_income,
        piti=loan.piti,
        other_debts=loan.other_debts,
        doc_kinds=frozenset(d.kind for d in docs),
        fields={name: f.value for name, f in trusted.items()},
        field_docs={name: f.doc_id for name, f in trusted.items()},
        low_confidence=withheld,
        # Reads the document text from disk. A deliberate exception to "this
        # snapshot is database-only": the integrity scan has to see the bytes,
        # and doing it here keeps every rule a pure function of its Facts.
        injection_hits={
            doc_id: tuple(h.marker for h in hits)
            for doc_id, hits in documents.scan_documents(
                (d.doc_id, d.path) for d in docs
            ).items()
        },
    )


def open_hitl(session: Session, queue: str | None = None) -> Sequence[ExceptionRecord]:
    stmt = select(ExceptionRecord).where(
        ExceptionRecord.lane == str(Lane.HITL),
        ExceptionRecord.status.in_(  # type: ignore[union-attr]
            [ExceptionStatus.ROUTED, ExceptionStatus.INQUEUE, ExceptionStatus.PENDING]
        ),
    )
    if queue:
        stmt = stmt.where(ExceptionRecord.queue == queue)
    return session.exec(stmt.order_by(ExceptionRecord.loan_id)).all()  # type: ignore[arg-type]
