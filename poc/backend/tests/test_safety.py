"""Safety tests — the gate, the policy and the audit chain, with no model in the loop.

These run before any agent exists. The point is to prove the constraint layer
is correct on its own, so that when something autonomous is later pointed at
it, the guarantees are already established rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app import audit, gate
from app.policy import (
    AUTO_THRESHOLD,
    BudgetExceeded,
    ConfidenceRaised,
    Disposition,
    DispositionWidened,
    InvariantViolation,
    Lane,
    RunBudget,
    Severity,
    assert_exception_invariants,
    assert_loan_invariants,
    decide_disposition,
    is_ready,
    revise_confidence,
    revise_disposition,
)


# ===========================================================================
# Disposition policy
# ===========================================================================
class TestDisposition:
    def test_high_confidence_mechanical_fix_goes_auto(self):
        d = decide_disposition("missing_document", Severity.MEDIUM, 93)
        assert d.lane is Lane.AUTO
        assert not d.requires_sup

    def test_below_threshold_goes_to_a_human(self):
        d = decide_disposition("missing_document", Severity.MEDIUM, 80)
        assert d.lane is Lane.HITL

    def test_critical_is_never_automatic_however_confident(self):
        d = decide_disposition("missing_document", Severity.CRITICAL, 100)
        assert d.lane is Lane.HITL
        assert d.requires_sup

    def test_judgment_calls_never_auto_even_at_high_confidence(self):
        # The teaching case: the model is 79% sure of the DTI breach — quite
        # sure — and it still goes to a person, because weighing compensating
        # factors against a program cap is not a thing software decides.
        d = decide_disposition("dti_breach", Severity.HIGH, 79)
        assert d.lane is Lane.HITL
        assert d.requires_sup

    def test_unknown_type_defaults_to_a_human(self):
        d = decide_disposition("some_type_we_have_never_seen", Severity.LOW, 99)
        assert d.lane is Lane.HITL, "an unrecognised finding must not be auto-repaired"

    @pytest.mark.parametrize("bad", [-1, 101, 1000])
    def test_out_of_range_confidence_rejected(self, bad):
        with pytest.raises(ValueError):
            decide_disposition("missing_document", Severity.LOW, bad)

    def test_every_never_auto_type_stays_hitl_across_the_whole_range(self):
        from app.policy import NEVER_AUTO

        for t in NEVER_AUTO:
            for conf in (0, 50, 88, 99, 100):
                assert decide_disposition(t, Severity.HIGH, conf).lane is Lane.HITL

    def test_no_type_is_both_never_auto_and_thresholded(self):
        from app.policy import NEVER_AUTO

        overlap = NEVER_AUTO & AUTO_THRESHOLD.keys()
        assert not overlap, f"contradictory policy for: {overlap}"


# ===========================================================================
# One-directional correction — the rules that keep the threshold meaningful
# ===========================================================================
class TestCorrectionIsOneDirectional:
    def test_confidence_may_be_lowered(self):
        assert revise_confidence(88, 62, "evidence-quote check failed") == 62

    def test_confidence_may_not_be_raised(self):
        with pytest.raises(ConfidenceRaised):
            revise_confidence(62, 89, "reflection pass")

    def test_finding_may_be_moved_toward_a_human(self):
        auto = Disposition(Lane.AUTO, False, "high confidence")
        hitl = Disposition(Lane.HITL, False, "quote not found in cited document")
        assert revise_disposition(auto, hitl).lane is Lane.HITL

    def test_finding_may_not_be_moved_away_from_a_human(self):
        hitl = Disposition(Lane.HITL, False, "below threshold")
        auto = Disposition(Lane.AUTO, False, "on reflection, actually fine")
        with pytest.raises(DispositionWidened):
            revise_disposition(hitl, auto)


# ===========================================================================
# The gate
# ===========================================================================
def ctx(agent="validation", loan="LN-2026-0002", **kw):
    return gate.RunContext(run_id="r1", agent=agent, loan_id=loan, **kw)


class TestGate:
    def test_validation_may_raise_a_finding(self):
        gate.check("raise_exception", {"loan_id": "LN-2026-0002"}, ctx())

    def test_validation_may_not_repair_one(self):
        # Separation of duties between agents. This is also the control that
        # makes a prompt-injected document ineffective.
        with pytest.raises(gate.ToolDenied, match="does not hold this capability"):
            gate.check("apply_auto_repair", {"exception_id": "EX-1"}, ctx("validation"))

    def test_processing_may_not_raise_a_finding(self):
        with pytest.raises(gate.ToolDenied):
            gate.check("raise_exception", {"loan_id": "LN-2026-0002"}, ctx("processing"))

    def test_summarizer_holds_no_write_tools_at_all(self):
        writes = [
            n for n, s in gate.TOOL_SPECS.items() if s.posture is not gate.Posture.READ
        ]
        for tool in writes:
            with pytest.raises(gate.ToolDenied):
                gate.check(tool, {"loan_id": "LN-2026-0002"}, ctx("summarizer"))

    def test_agent_cannot_reach_a_loan_outside_its_run(self):
        with pytest.raises(gate.ToolDenied, match="scoped to"):
            gate.check("get_loan", {"loan_id": "LN-2026-0009"}, ctx(loan="LN-2026-0002"))

    def test_closed_run_refuses_writes_but_allows_reads(self):
        c = ctx(open=False)
        gate.check("get_loan", {"loan_id": "LN-2026-0002"}, c)
        with pytest.raises(gate.ToolDenied, match="closed"):
            gate.check("raise_exception", {"loan_id": "LN-2026-0002"}, c)

    def test_unknown_tool_is_refused(self):
        with pytest.raises(gate.ToolDenied, match="no such tool"):
            gate.check("exfiltrate_everything", {}, ctx())

    def test_money_spending_tool_needs_confirmation(self):
        args = {"loan_id": "LN-2026-0002", "service": "appraisal"}
        with pytest.raises(gate.ToolDenied, match="human confirmation"):
            gate.check("order_vendor_service", args, ctx("processing"))

    def test_confirmation_is_bound_to_the_exact_arguments(self):
        approved = {"loan_id": "LN-2026-0002", "service": "appraisal"}
        token = gate.confirmation_token("order_vendor_service", approved)
        c = ctx("processing", confirmations={token})

        gate.check("order_vendor_service", approved, c)  # the approved call runs

        # A different service was never approved, even though one order was.
        other = {"loan_id": "LN-2026-0002", "service": "title"}
        with pytest.raises(gate.ToolDenied):
            gate.check("order_vendor_service", other, c)

    def test_every_spec_names_at_least_one_agent(self):
        for name, spec in gate.TOOL_SPECS.items():
            assert spec.agents, f"{name} is unreachable by any agent"

    def test_tool_order_is_stable_for_prompt_caching(self):
        # The tool list renders before the system prompt; reordering it
        # invalidates the cached prefix on every subsequent request.
        assert [s.name for s in gate.tools_for_agent("validation")] == [
            s.name for s in gate.tools_for_agent("validation")
        ]
        assert [s.name for s in gate.tools_for_agent("validation")] == sorted(
            s.name for s in gate.tools_for_agent("validation")
        )


# ===========================================================================
# Invariants
# ===========================================================================
@dataclass
class FakeExc:
    id: str = "EX-001"
    stage: int = 1
    status: str = "routed"
    lane: str = "hitl"
    requires_sup: bool = False
    resolved_by: str | None = None
    evidence_doc_id: str | None = "DOC-1"


@dataclass
class FakeLoan:
    id: str = "LN-2026-0002"
    scanned: bool = True
    ready: bool = False
    decision: str | None = None


class TestInvariants:
    def test_agent_cannot_resolve_a_sign_off_case(self):
        e = FakeExc(requires_sup=True, status="resolved", resolved_by="processing")
        with pytest.raises(InvariantViolation, match="supervisor sign-off"):
            assert_exception_invariants(e)

    def test_supervisor_can_resolve_a_sign_off_case(self):
        assert_exception_invariants(
            FakeExc(requires_sup=True, status="approved", resolved_by="Marcus Webb")
        )

    def test_closed_exception_must_carry_evidence(self):
        with pytest.raises(InvariantViolation, match="evidence"):
            assert_exception_invariants(
                FakeExc(status="resolved", resolved_by="Priya Nair", evidence_doc_id=None)
            )

    def test_loan_cannot_be_ready_with_open_gating_exceptions(self):
        loan = FakeLoan(ready=True)
        with pytest.raises(InvariantViolation, match="open gating"):
            assert_loan_invariants(loan, [FakeExc(status="routed")])

    def test_readiness_is_derived_not_assigned(self):
        loan = FakeLoan()
        open_exc = [FakeExc(status="routed")]
        assert is_ready(loan, open_exc) is False

        closed = [FakeExc(status="resolved")]
        assert is_ready(loan, closed) is True

    def test_stage_three_exceptions_do_not_gate_underwriting(self):
        # TRID lives at stage 3 and blocks funding, not readiness.
        loan = FakeLoan()
        assert is_ready(loan, [FakeExc(stage=3, status="routed"), FakeExc(status="resolved")])


# ===========================================================================
# Budgets
# ===========================================================================
class TestBudget:
    def make(self):
        return RunBudget(max_tool_calls=30, max_tokens=150_000, max_seconds=120, max_usd=1.50)

    def test_within_budget_passes(self):
        b = self.make()
        b.tool_calls, b.input_tokens, b.output_tokens = 5, 20_000, 2_000
        b.check()

    def test_tool_call_ceiling(self):
        b = self.make()
        b.tool_calls = 31
        with pytest.raises(BudgetExceeded, match="tool calls"):
            b.check()

    def test_spend_ceiling_binds_before_the_token_ceiling(self):
        # Output tokens cost 5x input, so an output-heavy run can blow the
        # dollar ceiling while still well under the token ceiling. That is the
        # case the spend limit exists for.
        b = self.make()
        b.input_tokens, b.output_tokens = 10_000, 100_000   # 110k tokens, $2.55
        assert b.input_tokens + b.output_tokens < b.max_tokens
        with pytest.raises(BudgetExceeded, match="spend"):
            b.check()

    def test_cost_is_computed_from_opus_47_rates(self):
        b = self.make()
        b.input_tokens, b.output_tokens = 1_000_000, 1_000_000
        assert b.usd == pytest.approx(30.00)  # $5 in + $25 out


# ===========================================================================
# Audit chain
# ===========================================================================
class TestAuditChain:
    def build(self, n=4):
        rows, prev = [], audit.GENESIS
        at = datetime(2026, 6, 1, 9, 12, tzinfo=timezone.utc)
        for i in range(n):
            row = audit.build_entry(
                prev,
                actor="Validation Agent",
                role="AI",
                kind="ai",
                action=f"Predicted finding {i}",
                case_id="LN-2026-0002",
                run_id="r1",
                at=at,
            )
            rows.append(row)
            prev = row["hash"]
        return rows

    def test_intact_chain_verifies(self):
        ok, broken = audit.verify_chain(self.build())
        assert ok and broken is None

    def test_edited_row_is_detected(self):
        rows = self.build()
        rows[1]["action"] = "Predicted finding 1 (quietly reworded)"
        ok, broken = audit.verify_chain(rows)
        assert not ok
        assert broken == rows[1]["hash"]

    def test_deleted_row_is_detected(self):
        rows = self.build()
        del rows[2]
        ok, _ = audit.verify_chain(rows)
        assert not ok, "removing a row must break the chain"

    def test_reordered_rows_are_detected(self):
        rows = self.build()
        rows[1], rows[2] = rows[2], rows[1]
        ok, _ = audit.verify_chain(rows)
        assert not ok

    def test_canonical_json_is_order_independent(self):
        a = audit.canonical({"b": 2, "a": 1})
        b = audit.canonical({"a": 1, "b": 2})
        assert a == b, "hash is only meaningful if serialization is stable"

    def test_secrets_are_redacted_before_hashing(self):
        out = audit.redact_detail({"ssn": "123-45-6789", "loan_id": "LN-1", "api_key": "sk-x"})
        assert out["ssn"] == "<redacted>"
        assert out["api_key"] == "<redacted>"
        assert out["loan_id"] == "LN-1"

    def test_oversized_values_are_truncated(self):
        out = audit.redact_detail({"doc": "x" * 5000})
        assert len(out["doc"]) < 2100 and "truncated" in out["doc"]
