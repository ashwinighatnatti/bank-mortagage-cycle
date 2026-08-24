"""What people do — the human counterpart to `tools/handlers.py`.

Every function here takes a `User`, checks `rbac.require()` before it writes,
and goes through `store.guarded_write()` with `kind="human"`. That last part is
the governance story: the audit trail distinguishes AI actions from human ones
at every row, so "what did the machine decide and what did a person decide" is
a query, not an archaeology project.

The propose/approve split lives here. An analyst working a `requires_sup`
exception does not resolve it — they move it to PENDING and create an Approval.
A supervisor closes it. `policy.assert_exception_invariants()` independently
refuses any close of a supervisor-required exception by an agent, so this
module and the invariant check agree without either trusting the other.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from . import store
from .models import (
    Approval,
    ApprovalStatus,
    Confirmation,
    Decision,
    ExceptionRecord,
    ExceptionStatus,
    Loan,
    Note,
)
from .rbac import Action, Denied, Role, User, require, require_queue

# Verbatim from the reference design. The label is what appears in the audit
# trail and on the exception card, so it must not drift.
ACTION_LABELS: dict[str, str] = {
    "verify": "Verified & Approved",
    "recalc": "Recalculated",
    "request": "Document requested (Outreach)",
    "override": "Overridden",
}


def _exception(session: Session, exception_id: str) -> ExceptionRecord:
    exc = session.get(ExceptionRecord, exception_id)
    if exc is None:
        raise LookupError(f"no such exception: {exception_id}")
    return exc


def claim_exception(session: Session, user: User, exception_id: str) -> ExceptionRecord:
    """Analyst picks a routed case up: routed -> inqueue."""
    require(user, Action.WORK_EXCEPTION)
    exc = _exception(session, exception_id)
    require_queue(user, exc.queue)

    if exc.status != ExceptionStatus.ROUTED:
        raise Denied(Action.WORK_EXCEPTION, user.role,
                     f"{exception_id} is {exc.status}, not waiting to be picked up")

    with store.guarded_write(
        session, loan_id=exc.loan_id, actor=user.username, role=str(user.role),
        kind="human", action="claim_exception",
        detail={"exception_id": exception_id},
    ):
        exc.status = ExceptionStatus.INQUEUE
        session.add(exc)
    return exc


def act_on_exception(
    session: Session, user: User, exception_id: str, action: str, note: str = ""
) -> ExceptionRecord:
    """Work a HITL exception. Resolves it, or proposes a fix for sign-off.

    Which of the two happens is not the analyst's choice — `requires_sup` was
    set by policy when the finding was raised, and it decides.
    """
    require(user, Action.WORK_EXCEPTION)
    if action not in ACTION_LABELS:
        raise Denied(Action.WORK_EXCEPTION, user.role,
                     f"unknown action {action!r}; expected one of "
                     f"{', '.join(ACTION_LABELS)}")

    exc = _exception(session, exception_id)
    require_queue(user, exc.queue)

    if not exc.is_open:
        raise Denied(Action.WORK_EXCEPTION, user.role,
                     f"{exception_id} is already {exc.status}")
    if exc.lane != "hitl":
        raise Denied(Action.WORK_EXCEPTION, user.role,
                     f"{exception_id} is in the auto lane and is not yours to work")

    # PENDING is open but already spoken for. Without this, acting again on a
    # sign-off case creates a SECOND proposal for the same exception: the
    # supervisor sees three near-identical cards, approving one closes the
    # exception, and the others are left pointing at something already decided.
    # A second proposal is not a new fact, it is a duplicate.
    if exc.status == ExceptionStatus.PENDING:
        existing = session.exec(
            select(Approval).where(
                Approval.exception_id == exc.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        raise Denied(
            Action.WORK_EXCEPTION, user.role,
            f"{exception_id} already has a proposal awaiting sign-off"
            + (f" ({existing.id}, from {existing.proposed_by})" if existing else "")
            + ". A supervisor approves or rejects it; a rejection returns the "
            "case to its queue and you can propose again then.",
        )

    label = ACTION_LABELS[action]
    needs_signoff = exc.requires_sup

    with store.guarded_write(
        session, loan_id=exc.loan_id, actor=user.username, role=str(user.role),
        kind="human",
        action="propose_resolution" if needs_signoff else "resolve_exception",
        detail={"exception_id": exception_id, "action": action,
                "label": label, "note": note},
    ):
        if needs_signoff:
            exc.status = ExceptionStatus.PENDING
            exc.resolution_note = f"{label}. {note}".strip()
            session.add(exc)
            session.add(Approval(
                id=_next_approval_id(session),
                exception_id=exc.id,
                loan_id=exc.loan_id,
                exception_type=exc.exception_type,
                proposed_by=user.username,
                proposed_action=label,
                ai_recommendation=exc.recommendation,
                queue=exc.queue,
                status=ApprovalStatus.PENDING,
                note=note or None,
            ))
        else:
            exc.status = ExceptionStatus.RESOLVED
            exc.resolved_by = user.username
            exc.resolved_at = datetime.now(timezone.utc)
            exc.resolution_note = f"{label}. {note}".strip()
            session.add(exc)
            # A resolution by a person is the only thing this system accepts as
            # operational memory. See the Note docstring.
            session.add(Note(
                exception_type=exc.exception_type,
                text=f"{label} by {user.name}. {note}".strip(),
                source="human_resolution",
                loan_id=exc.loan_id,
            ))
    return exc


def decide_approval(
    session: Session, user: User, approval_id: str, decision: str, note: str = ""
) -> Approval:
    """Supervisor signs off, or sends it back.

    A rejection returns the exception to `routed` rather than closing it — the
    problem is still there, it is just not fixed the way that was proposed.
    """
    require(user, Action.APPROVE)
    if decision not in ("approved", "rejected"):
        raise Denied(Action.APPROVE, user.role,
                     f"decision must be approved or rejected, got {decision!r}")

    approval = session.get(Approval, approval_id)
    if approval is None:
        raise LookupError(f"no such approval: {approval_id}")
    if approval.status != ApprovalStatus.PENDING:
        raise Denied(Action.APPROVE, user.role,
                     f"{approval_id} was already {approval.status}")
    if approval.proposed_by == user.username:
        # A supervisor may work any queue, so this is reachable: propose as
        # yourself, then try to sign off your own proposal.
        raise Denied(Action.APPROVE, user.role,
                     "you proposed this; sign-off is a second pair of eyes")

    exc = _exception(session, approval.exception_id)
    if not exc.is_open:
        # A stale proposal pointing at a case somebody already closed. Acting on
        # it would silently re-open and re-close the exception under a different
        # approval id, and the trail would show two people deciding it once each.
        raise Denied(
            Action.APPROVE, user.role,
            f"{exc.id} is already {exc.status}; {approval_id} refers to a case "
            "that has been decided",
        )

    with store.guarded_write(
        session, loan_id=approval.loan_id, actor=user.username, role=str(user.role),
        kind="human", action=f"approval_{decision}",
        detail={"approval_id": approval_id, "exception_id": exc.id, "note": note},
    ):
        approval.status = (ApprovalStatus.APPROVED if decision == "approved"
                           else ApprovalStatus.REJECTED)
        approval.decided_by = user.username
        approval.decided_at = datetime.now(timezone.utc)
        approval.note = note or approval.note
        session.add(approval)

        if decision == "approved":
            exc.status = ExceptionStatus.APPROVED
            exc.resolved_by = user.username
            exc.resolved_at = datetime.now(timezone.utc)
        else:
            exc.status = ExceptionStatus.ROUTED
            exc.resolution_note = f"Rejected: {note}".strip()
        session.add(exc)
    return approval


def decide_confirmation(
    session: Session, user: User, token: str, decision: str
) -> Confirmation:
    """Authorise or refuse a gated tool call.

    Approving writes `status = approved`. The next agent run loads approved
    tokens into its `RunContext`, and the identical call then passes the gate.
    Nothing here executes the call — a person authorises, an agent acts.
    """
    require(user, Action.CONFIRM_GATED)
    if decision not in ("approved", "rejected"):
        raise Denied(Action.CONFIRM_GATED, user.role,
                     f"decision must be approved or rejected, got {decision!r}")

    conf = session.get(Confirmation, token)
    if conf is None:
        raise LookupError("no such confirmation")
    if conf.status != ApprovalStatus.PENDING:
        raise Denied(Action.CONFIRM_GATED, user.role,
                     f"this request was already {conf.status}")

    with store.guarded_write(
        session, loan_id=conf.loan_id, actor=user.username, role=str(user.role),
        kind="human", action=f"confirmation_{decision}",
        detail={"tool": conf.tool, "args": conf.args},
    ):
        conf.status = (ApprovalStatus.APPROVED if decision == "approved"
                       else ApprovalStatus.REJECTED)
        conf.confirmed_by = user.username
        conf.confirmed_at = datetime.now(timezone.utc)
        session.add(conf)
    return conf


def decide_loan(
    session: Session, user: User, loan_id: str, decision: str, note: str = ""
) -> Loan:
    """The underwriting decision. Underwriters only, and only on a ready file."""
    require(user, Action.DECIDE_LOAN)
    try:
        Decision(decision)
    except ValueError:
        raise Denied(Action.DECIDE_LOAN, user.role,
                     f"unknown decision {decision!r}; expected one of "
                     f"{', '.join(d.value for d in Decision)}") from None

    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")
    if not loan.ready:
        # `assert_loan_invariants` would also catch this, but as a rollback
        # after the fact. Refusing here gives the underwriter a reason instead
        # of an internal error.
        raise Denied(Action.DECIDE_LOAN, user.role,
                     f"{loan_id} is not ready for underwriting; open gating "
                     "exceptions must be cleared first")
    if loan.decision:
        raise Denied(Action.DECIDE_LOAN, user.role,
                     f"{loan_id} was already decided: {loan.decision}")

    with store.guarded_write(
        session, loan_id=loan_id, actor=user.username, role=str(user.role),
        kind="human", action="decide_loan",
        detail={"decision": decision, "note": note},
    ):
        loan.decision = decision
        loan.decision_note = note or None
        loan.decided_by = user.username
        loan.decided_at = datetime.now(timezone.utc)
        loan.delivered = decision in (Decision.APPROVE, Decision.APPROVE_WITH_CONDITIONS)
        session.add(loan)
    return loan


def _next_approval_id(session: Session) -> str:
    n = session.exec(select(func.count()).select_from(Approval)).one()
    return f"AP-{n + 1:03d}"
