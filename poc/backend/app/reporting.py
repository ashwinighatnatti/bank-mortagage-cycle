"""Read models for the UI — KPIs, the underwriting hub, the rules panel.

Everything here is derived on read. Nothing is stored, nothing is cached, and
no endpoint may write. That keeps a dashboard number from ever disagreeing with
the rows it claims to summarise, which is the usual failure mode once someone
adds a counter column "for performance".

KPI definitions are verbatim from the reference design so the numbers mean the
same thing they did in the prototype.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from . import rules, store
from .market_data import CU_SCORE_THRESHOLD, PROGRAM_LIMITS
from .models import Approval, ApprovalStatus, ExceptionRecord, ExceptionStatus, Loan

_OPEN_HITL = (ExceptionStatus.ROUTED, ExceptionStatus.INQUEUE, ExceptionStatus.PENDING)
_CLOSED = (ExceptionStatus.RESOLVED, ExceptionStatus.APPROVED)

STAGES = [
    "Application & Intake",
    "Loan Processing",
    "Underwriting",
    "Closing & Funding",
    "Post-Closing QC",
]


def kpis(session: Session) -> dict[str, Any]:
    loans = list(session.exec(select(Loan)).all())
    excs = list(session.exec(select(ExceptionRecord)).all())

    auto = [e for e in excs if e.lane == "auto"]
    auto_repaired = [e for e in auto if e.status in _CLOSED]
    resolved = [e for e in excs if e.status in _CLOSED]
    open_hitl = [e for e in excs if e.status in _OPEN_HITL]
    scanned = [l for l in loans if l.scanned]
    ready = [l for l in loans if l.ready]
    decided = [l for l in loans if l.decision]
    delivered = [l for l in loans if l.delivered]

    # Straight-through: a scanned loan every exception of which was
    # auto-dispositioned. A loan with no exceptions counts — nothing needed a
    # human, which is exactly what STP means.
    by_loan: dict[str, list[ExceptionRecord]] = {}
    for e in excs:
        by_loan.setdefault(e.loan_id, []).append(e)
    stp_loans = [
        l for l in scanned
        if all(e.lane == "auto" for e in by_loan.get(l.id, []))
    ]

    return {
        "loans": len(loans),
        "scanned": len(scanned),
        "predicted": len([e for e in excs if e.status != ExceptionStatus.IDLE]),
        "exceptions": len(excs),
        "auto_total": len(auto),
        "auto_repaired": len(auto_repaired),
        "auto_pct": round(len(auto_repaired) / len(auto) * 100) if auto else 0,
        "resolved": len(resolved),
        "open_hitl": len(open_hitl),
        "pending_approvals": len([
            a for a in session.exec(select(Approval)).all()
            if a.status == ApprovalStatus.PENDING
        ]),
        "ready": len(ready),
        "decided": len(decided),
        "delivered": len(delivered),
        "stp_pct": round(len(stp_loans) / len(scanned) * 100) if scanned else 0,
        # Cosmetic, and honest about it: a 43-day baseline compressing toward 29
        # as work clears. Kept because the reference shows it and a customer
        # asks; the baseline is the only part that claims to be real.
        "cycle_days": 43 - min(14, round((len(resolved) + len(delivered) * 2) / 2)),
        "queue_load": {
            q: len([e for e in open_hitl if e.queue == q]) for q in ("A", "B", "C")
        },
        "by_severity": {
            s: len([e for e in excs if e.severity == s])
            for s in ("Low", "Medium", "High", "Critical")
        },
        "by_stage": [
            {"stage": i, "name": name,
             "loans": len([l for l in loans if _stage_of(l, by_loan.get(l.id, [])) == i])}
            for i, name in enumerate(STAGES)
        ],
    }


def _stage_of(loan: Loan, excs: list[ExceptionRecord]) -> int:
    """Where a loan sits in the funnel. Derived, like everything else here."""
    if loan.delivered:
        return 4
    if loan.decision:
        return 3
    if loan.ready:
        return 2
    if loan.scanned:
        return 1
    return 0


def loan_summary(session: Session, loan: Loan) -> dict[str, Any]:
    excs = store.exceptions_for(session, loan.id)
    return {
        "id": loan.id,
        "borrowers": loan.borrowers,
        "metro": loan.metro,
        "program": loan.program,
        "purpose": loan.purpose,
        "amount": loan.amount,
        "fico": loan.fico,
        "dti": loan.dti,
        "ltv": loan.ltv,
        "is_jumbo": loan.is_jumbo,
        "scanned": loan.scanned,
        "ready": loan.ready,
        "decision": loan.decision,
        "delivered": loan.delivered,
        "stage": _stage_of(loan, excs),
        "stage_name": STAGES[_stage_of(loan, excs)],
        "exceptions": len(excs),
        "open_hitl": len([e for e in excs if e.status in _OPEN_HITL]),
        "summary": loan.summary,
    }


def exception_view(exc: ExceptionRecord) -> dict[str, Any]:
    return {
        "id": exc.id,
        "loan_id": exc.loan_id,
        "stage": exc.stage,
        "stage_name": STAGES[exc.stage] if 0 <= exc.stage < len(STAGES) else "",
        "type": exc.exception_type,
        "label": exc.label,
        "severity": exc.severity,
        "confidence": exc.confidence,
        "lane": exc.lane,
        "queue": exc.queue,
        "requires_sup": exc.requires_sup,
        "status": exc.status,
        "disposition_reason": exc.disposition_reason,
        "recommendation": exc.recommendation,
        "rationale": exc.rationale,
        "evidence": exc.evidence_quote,
        "evidence_doc_id": exc.evidence_doc_id,
        "raised_by": exc.raised_by,
        "raised_at": exc.raised_at,
        "resolved_by": exc.resolved_by,
        "resolution_note": exc.resolution_note,
        "confidence_revised_from": exc.confidence_revised_from,
        "revision_reason": exc.revision_reason,
    }


def rules_panel(session: Session, loan_id: str) -> list[dict[str, Any]]:
    """Every rule, run live against the loan's current facts.

    Three outcomes reach the UI, not two. A rule that could not run is shown as
    such — collapsing INDETERMINATE into a pass or a fail is exactly the lie
    this system is built to avoid, and it would be an easy one to tell here
    because a two-state chip is prettier.
    """
    facts = store.build_facts(session, loan_id)
    return [
        {
            "rule_id": r.rule_id,
            "label": rules.RULE_LABELS.get(r.rule_id, r.rule_id),
            "outcome": str(r.outcome),
            "detail": r.detail,
            "evidence": r.evidence,
            "missing": list(r.missing),
        }
        for r in rules.evaluate_all(facts)
    ]


def hub(session: Session, loan_id: str) -> dict[str, Any]:
    """The Underwriters' Digital Hub payload for one loan."""
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise LookupError(f"no such loan: {loan_id}")
    excs = store.exceptions_for(session, loan_id)
    limits = PROGRAM_LIMITS[loan.program]

    gating = [e for e in excs if e.stage <= 2]
    conditions = [
        {"text": f"Clear: {e.label}", "done": e.status in _CLOSED, "exception_id": e.id}
        for e in gating
    ] + [
        # Two standing conditions, verbatim from the reference. They are never
        # auto-checked — a person confirms them at closing.
        {"text": "Verify income documentation (W-2 + paystub)", "done": False,
         "exception_id": None},
        {"text": "Evidence of hazard insurance at closing", "done": False,
         "exception_id": None},
    ]

    aus_engine = "TOTAL" if loan.program == "FHA" else "DU"
    aus_result = "Approve/Eligible"
    for e in excs:
        if e.exception_type == "aus_referral":
            aus_engine = "LPA" if "LPA" in (e.label or "") else aus_engine
            aus_result = "Refer/Eligible" if aus_engine != "LPA" else "Caution"

    return {
        "loan": loan_summary(session, loan),
        "guidelines": {
            "program": loan.program,
            "label": limits.label,
            "dti": {"value": loan.dti, "cap": limits.dti_cap,
                    "pass": loan.dti <= limits.dti_cap},
            "ltv": {"value": loan.ltv, "cap": limits.ltv_cap,
                    "pass": loan.ltv <= limits.ltv_cap},
            "fico": {"value": loan.fico, "floor": limits.fico_floor,
                     "pass": loan.fico >= limits.fico_floor},
            "cu_threshold": CU_SCORE_THRESHOLD,
        },
        "aus": {"engine": aus_engine, "result": aus_result},
        "conditions": conditions,
        "rules": rules_panel(session, loan_id),
        "exceptions": [exception_view(e) for e in excs],
        "decisions": ["approve", "approve-conditions", "suspend", "deny"],
    }
