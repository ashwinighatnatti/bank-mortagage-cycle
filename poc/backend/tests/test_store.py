"""Tests for the persistence layer: disposition, invariants, audit, corrections.

These run against a real SQLite database because the guarantees under test are
database guarantees — foreign keys, the unique constraint that makes a forked
audit chain impossible, and savepoint rollback when a post-condition fails.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import audit, store
from app.models import (
    AuditEntry,
    ExceptionRecord,
    ExceptionStatus,
    ExtractedField,
    Loan,
)
from app.policy import (
    ConfidenceRaised,
    DispositionWidened,
    InvariantViolation,
    Lane,
    Severity,
)


def make_exc(loan_id: str, **overrides) -> ExceptionRecord:
    kwargs = dict(
        id="EX-001",
        loan_id=loan_id,
        stage=1,
        exception_type="flood_cert_missing",
        label="Flood certification missing",
        severity=Severity.MEDIUM,
        confidence=92,
        evidence_doc_id=f"{loan_id}-flood_cert",
        raised_by="validation",
    )
    kwargs.update(overrides)
    return ExceptionRecord.from_finding(**kwargs)


# ---------------------------------------------------------------------------
# The model does not choose the disposition
# ---------------------------------------------------------------------------
def test_from_finding_assigns_the_lane_from_policy(loan):
    exc = make_exc(loan.id, confidence=92)
    assert exc.lane == Lane.AUTO
    assert exc.requires_sup is False
    assert exc.queue is None


def test_below_threshold_routes_to_a_human(loan):
    exc = make_exc(loan.id, confidence=71)
    assert exc.lane == Lane.HITL
    assert exc.queue == "B"


def test_critical_is_never_automatic_however_confident(loan):
    exc = make_exc(loan.id, exception_type="title_exception",
                   label="Title exception", severity=Severity.CRITICAL, confidence=100)
    assert exc.lane == Lane.HITL and exc.requires_sup is True


def test_unknown_exception_type_routes_to_a_human(loan):
    """An exception type nobody wrote a threshold for must not be auto-closed."""
    exc = make_exc(loan.id, exception_type="something_nobody_anticipated",
                   label="Novel", confidence=99)
    assert exc.lane == Lane.HITL
    assert exc.queue == "C"


def test_finding_schema_has_no_lane_parameter():
    """A model emitting lane='auto' in a structured output has nowhere to put it."""
    import inspect

    params = inspect.signature(ExceptionRecord.from_finding).parameters
    assert "lane" not in params
    assert "requires_sup" not in params


# ---------------------------------------------------------------------------
# Readiness is derived
# ---------------------------------------------------------------------------
def test_open_gating_exception_blocks_readiness(session, loan):
    session.add(make_exc(loan.id, confidence=71))
    session.commit()
    store.recompute_readiness(session, loan.id)
    assert session.get(Loan, loan.id).ready is False


def test_closing_the_last_gating_exception_makes_the_loan_ready(session, loan):
    exc = make_exc(loan.id, confidence=71)
    session.add(exc)
    session.commit()

    exc.status = ExceptionStatus.RESOLVED
    exc.resolved_by = "analyst.jane"
    session.add(exc)
    store.recompute_readiness(session, loan.id)
    session.commit()
    assert session.get(Loan, loan.id).ready is True


def test_a_stage_three_exception_does_not_block_readiness(session, loan):
    """Stage 3 is closing-side. It must not hold a file out of underwriting."""
    session.add(make_exc(loan.id, id="EX-C3", stage=3,
                         exception_type="fee_tolerance_variance",
                         label="TRID fee variance", confidence=67))
    session.commit()
    store.recompute_readiness(session, loan.id)
    assert session.get(Loan, loan.id).ready is True


# ---------------------------------------------------------------------------
# guarded_write
# ---------------------------------------------------------------------------
def test_guarded_write_audits_and_recomputes(session, loan):
    with store.guarded_write(session, loan_id=loan.id, actor="validation",
                             role="Validation Agent", kind="ai",
                             action="raise_exception", run_id="RUN-1",
                             detail={"exception_type": "flood_cert_missing"}):
        session.add(make_exc(loan.id, confidence=71))

    assert session.get(Loan, loan.id).ready is False
    rows = session.exec(select(AuditEntry)).all()
    assert [r.action for r in rows] == ["raise_exception"]
    assert rows[0].prev_hash == audit.GENESIS


def test_guarded_write_rolls_back_the_state_and_the_audit_line(session, loan):
    """An action that fails its post-conditions did not happen — including in the trail."""
    exc = make_exc(loan.id, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    session.add(exc)
    session.commit()
    assert exc.requires_sup is True

    with pytest.raises(InvariantViolation):
        with store.guarded_write(session, loan_id=loan.id, actor="processing",
                                 role="Processing Agent", kind="ai",
                                 action="apply_auto_repair"):
            # An agent closing a supervisor-required exception.
            exc.status = ExceptionStatus.RESOLVED
            exc.resolved_by = "processing"
            session.add(exc)

    session.rollback()
    # A HITL finding lands ROUTED; the rollback must leave it exactly there.
    assert session.get(ExceptionRecord, "EX-001").status == ExceptionStatus.ROUTED
    assert session.exec(select(AuditEntry)).all() == []


def test_supervisor_required_exception_may_be_closed_by_a_person(session, loan):
    exc = make_exc(loan.id, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    session.add(exc)
    session.commit()

    with store.guarded_write(session, loan_id=loan.id, actor="supervisor.raj",
                             role="Supervisor", kind="human", action="approve"):
        exc.status = ExceptionStatus.APPROVED
        exc.resolved_by = "supervisor.raj"
        session.add(exc)

    assert session.get(ExceptionRecord, "EX-001").status == ExceptionStatus.APPROVED


def test_closing_without_evidence_is_refused(session, loan):
    exc = make_exc(loan.id, confidence=92, evidence_doc_id=None)
    session.add(exc)
    session.commit()

    with pytest.raises(InvariantViolation, match="without evidence"):
        with store.guarded_write(session, loan_id=loan.id, actor="processing",
                                 role="Processing Agent", kind="ai",
                                 action="apply_auto_repair"):
            exc.status = ExceptionStatus.RESOLVED
            exc.resolved_by = "processing"
            session.add(exc)
    session.rollback()


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------
def test_chain_stays_intact_across_many_writes(session, loan):
    for i in range(6):
        store.append_audit(session, actor="validation", role="Validation Agent",
                           kind="ai", action=f"step_{i}", case_id=loan.id)
    session.commit()
    ok, broken = store.verify_audit_chain(session)
    assert ok and broken is None


def test_tampering_with_a_historical_row_is_detected(session, loan):
    for i in range(4):
        store.append_audit(session, actor="a", role="r", kind="ai",
                           action=f"step_{i}", case_id=loan.id)
    session.commit()

    victim = session.exec(select(AuditEntry).order_by(AuditEntry.id)).all()[1]
    victim.action = "step_1_altered"
    session.add(victim)
    session.commit()

    ok, broken = store.verify_audit_chain(session)
    assert not ok and broken == victim.hash


def test_the_chain_cannot_fork(session, loan):
    """UNIQUE(prev_hash): two rows may not claim the same predecessor."""
    first = store.append_audit(session, actor="a", role="r", kind="ai",
                               action="one", case_id=loan.id)
    session.commit()

    forged = audit.build_entry(first.prev_hash, actor="b", role="r", kind="ai",
                               action="two", case_id=loan.id)
    session.add(AuditEntry(**forged))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_sensitive_values_are_redacted_before_they_enter_the_hash(session, loan):
    store.append_audit(session, actor="intake", role="Intake Agent", kind="ai",
                       action="record_extraction", case_id=loan.id,
                       detail={"ssn": "123-45-6789", "field": "borrower_name"})
    session.commit()

    row = session.exec(select(AuditEntry)).first()
    assert row.detail["ssn"] == "<redacted>"
    assert "6789" not in audit.canonical(row.detail)
    ok, _ = store.verify_audit_chain(session)
    assert ok


# ---------------------------------------------------------------------------
# Corrections are one-directional
# ---------------------------------------------------------------------------
def test_confidence_may_be_lowered(session, loan):
    session.add(make_exc(loan.id, confidence=92))
    session.commit()

    exc = store.revise_finding(session, "EX-001", revised_confidence=74,
                               reason="quoted evidence not found in the cited document",
                               source="evidence_check")
    session.commit()
    assert exc.confidence == 74
    assert exc.confidence_revised_from == 92
    assert exc.lane == Lane.HITL
    assert exc.queue == "B"


def test_confidence_may_not_be_raised(session, loan):
    session.add(make_exc(loan.id, confidence=71))
    session.commit()
    with pytest.raises(ConfidenceRaised):
        store.revise_finding(session, "EX-001", revised_confidence=95,
                             reason="second pass was more sure", source="reflection")


def test_a_correction_may_not_move_a_finding_back_to_auto(session, loan):
    """Even at an unchanged confidence, HITL is a one-way door.

    The realistic route back to auto is reclassification, not a higher score: a
    finding sitting in HITL because its *type* is never automatic gets
    relabelled as a type that can be, at a confidence already above that type's
    threshold. Nothing here raises a confidence, and it must still be refused.
    """
    exc = make_exc(loan.id, exception_type="income_variance", label="Income variance",
                   severity=Severity.HIGH, confidence=95)
    session.add(exc)
    session.commit()
    assert exc.lane == Lane.HITL  # income_variance is never auto, whatever the score

    exc.exception_type = "missing_document"  # threshold 88, and we are at 95
    session.add(exc)
    session.flush()

    with pytest.raises(DispositionWidened):
        store.revise_finding(session, "EX-001", revised_confidence=95,
                             reason="reclassified as a missing document",
                             source="reclassifier")


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------
def test_evidence_must_point_at_a_real_document(session, loan):
    """Without PRAGMA foreign_keys=ON this silently succeeds."""
    session.add(make_exc(loan.id, evidence_doc_id="LN-TEST-0001-does-not-exist"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# Tool-call bookkeeping
# ---------------------------------------------------------------------------
def test_a_denied_call_is_recorded_and_counted(session, loan):
    store.open_run(session, run_id="RUN-9", agent="validation", loan_id=loan.id)
    store.record_tool_call(session, run_id="RUN-9", loan_id=loan.id,
                           agent="validation", tool="apply_auto_repair",
                           posture="write", args={"exception_id": "EX-001"},
                           ok=False, denied_reason="validation does not hold this capability")
    session.commit()

    from app.models import Run, ToolCall

    call = session.exec(select(ToolCall)).one()
    assert call.ok is False and "capability" in call.denied_reason
    assert session.get(Run, "RUN-9").tool_calls == 1


def test_tool_arguments_are_redacted_when_recorded(session, loan):
    store.open_run(session, run_id="RUN-10", agent="intake", loan_id=loan.id)
    store.record_tool_call(session, run_id="RUN-10", loan_id=loan.id, agent="intake",
                           tool="record_extraction", posture="write",
                           args={"ssn": "123-45-6789", "name": "borrower_ssn"})
    session.commit()

    from app.models import ToolCall

    call = session.exec(select(ToolCall)).one()
    assert call.args["ssn"] == "<redacted>"


# ---------------------------------------------------------------------------
# Facts snapshot
# ---------------------------------------------------------------------------
def test_build_facts_reflects_the_loan_and_its_documents(session, loan):
    f = store.build_facts(session, loan.id)
    assert f.program == "Conv"
    assert "w2" in f.doc_kinds and "flood_cert" in f.doc_kinds
    assert f.fields == {}


def test_build_facts_prefers_the_higher_confidence_extraction(session, loan):
    session.add(ExtractedField(loan_id=loan.id, doc_id=f"{loan.id}-w2",
                               name="monthly_income", value="5940", confidence=0.71))
    session.add(ExtractedField(loan_id=loan.id, doc_id=f"{loan.id}-paystub",
                               name="monthly_income", value="6520", confidence=0.95))
    session.commit()

    f = store.build_facts(session, loan.id)
    assert f.num("monthly_income") == 6520.0
    assert f.field_docs["monthly_income"] == f"{loan.id}-paystub"


# ---------------------------------------------------------------------------
# Routing is mechanical
# ---------------------------------------------------------------------------
def test_a_hitl_finding_lands_in_a_queue_not_in_limbo(session, loan):
    """A finding routed to a human must be visible to that human immediately.

    Before this was mechanical, a HITL finding sat at PREDICTED and never
    appeared in `open_hitl()` — routed by policy, queued by nobody.
    """
    session.add(make_exc(loan.id, confidence=71))
    session.commit()

    exc = session.get(ExceptionRecord, "EX-001")
    assert exc.status == ExceptionStatus.ROUTED
    assert [e.id for e in store.open_hitl(session)] == ["EX-001"]
    assert [e.id for e in store.open_hitl(session, queue="B")] == ["EX-001"]


def test_an_auto_finding_is_not_queued(session, loan):
    session.add(make_exc(loan.id, confidence=92))
    session.commit()

    exc = session.get(ExceptionRecord, "EX-001")
    assert exc.status == ExceptionStatus.PREDICTED
    assert exc.queue is None
    assert store.open_hitl(session) == []


def test_a_correction_out_of_the_auto_lane_also_puts_it_in_a_queue(session, loan):
    """Lowering a confidence must not leave a finding on no path at all."""
    session.add(make_exc(loan.id, confidence=92))
    session.commit()

    store.revise_finding(session, "EX-001", revised_confidence=74,
                         reason="quote not found", source="evidence_check")
    session.commit()

    assert [e.id for e in store.open_hitl(session)] == ["EX-001"]


def test_build_facts_withholds_an_extraction_below_the_confidence_floor(session, loan):
    """A value the extractor barely believed must not drive arithmetic."""
    from app.rules import EXTRACTION_CONFIDENCE_FLOOR

    session.add(ExtractedField(loan_id=loan.id, doc_id=f"{loan.id}-w2",
                               name="w2_annual_wages", value="$128,\u258808.48",
                               confidence=0.35))
    session.add(ExtractedField(loan_id=loan.id, doc_id=f"{loan.id}-paystub",
                               name="paystub_monthly_income", value="$10,309.04",
                               confidence=0.95))
    session.commit()

    f = store.build_facts(session, loan.id)
    assert "w2_annual_wages" not in f.fields
    assert f.low_confidence["w2_annual_wages"] == 0.35
    assert f.fields["paystub_monthly_income"] == "$10,309.04"
    assert EXTRACTION_CONFIDENCE_FLOOR == 0.60


def test_a_withheld_field_is_reported_as_unreadable_not_absent(session, loan):
    session.add(ExtractedField(loan_id=loan.id, doc_id=f"{loan.id}-w2",
                               name="w2_annual_wages", value="$1", confidence=0.2))
    session.commit()

    f = store.build_facts(session, loan.id)
    assert "0.20 confidence" in f.why_missing("w2_annual_wages")
    assert f.why_missing("never_extracted") == "never_extracted"
