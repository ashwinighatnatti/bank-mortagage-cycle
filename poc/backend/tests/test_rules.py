"""Tests for the deterministic rules engine.

The tests that matter most here are the INDETERMINATE ones. A rules engine that
returns PASS on missing input is worse than no rules engine, because it
manufactures false assurance: the file reads clean precisely because nothing
was checked. Each rule gets an explicit "input absent" test asserting the
outcome is INDETERMINATE and not PASS.
"""

from __future__ import annotations

import pytest

from app.policy import Severity
from app.rules import (
    RULE_LABELS,
    RULES,
    Facts,
    Outcome,
    evaluate,
    evaluate_all,
)

BASE = dict(
    loan_id="LN-TEST-0001",
    program="Conv",
    purpose="Purchase",
    metro="Austin, TX",
    amount=412_000.0,
    property_value=515_000.0,
    fico=742,
    stated_ltv=80.0,
    stated_dti=38.0,
    monthly_income=10_309.04,
    piti=3_277.44,
    other_debts=640.0,
    doc_kinds=frozenset({"urla", "id", "w2", "paystub", "bank_statement",
                         "credit_report", "hoi", "appraisal", "flood_cert"}),
)


def facts(**overrides) -> Facts:
    data = {**BASE, **overrides}
    data.setdefault("fields", {})
    return Facts(**data)


# ---------------------------------------------------------------------------
# The property that protects everything else
# ---------------------------------------------------------------------------
def test_no_rule_passes_on_empty_facts():
    """With nothing extracted, no rule may report PASS on an extracted quantity.

    Header-only rules (eligibility, DTI, LTV, completeness) legitimately can
    pass — their inputs are on the loan record. Every rule that needs an
    extracted field must be INDETERMINATE, never PASS.
    """
    needs_extraction = {
        "income_employment", "asset_sourcing", "trid_fee_tolerance",
        "identity_cip", "collateral_valuation",
    }
    results = {r.rule_id: r for r in evaluate_all(facts(fields={}))}
    for rule_id in needs_extraction:
        assert results[rule_id].outcome is not Outcome.PASS, (
            f"{rule_id} reported {results[rule_id].outcome} with no extracted "
            "fields — a check that did not run must not read as a check that passed"
        )


def test_indeterminate_names_what_is_missing():
    r = evaluate("income_employment", facts(fields={"paystub_monthly_income": "6520"}))
    assert r.outcome is Outcome.INDETERMINATE
    assert "w2_annual_wages" in r.missing
    assert "Not a pass" in r.detail


def test_every_registered_rule_has_a_label():
    """The Rules Engine panel renders RULE_LABELS; a rule missing from it is invisible."""
    assert set(RULES) == set(RULE_LABELS)


def test_unknown_rule_is_indeterminate_not_an_error():
    r = evaluate("does_not_exist", facts())
    assert r.outcome is Outcome.INDETERMINATE
    assert "no such rule" in r.detail


def test_a_raising_rule_does_not_abort_the_pass(monkeypatch):
    """One broken rule must not stop the others from running."""
    def boom(_f):
        raise ZeroDivisionError("synthetic")

    monkeypatch.setitem(RULES, "identity_cip", boom)
    results = evaluate_all(facts())
    broken = next(r for r in results if r.rule_id == "identity_cip")
    assert broken.outcome is Outcome.INDETERMINATE
    assert "ZeroDivisionError" in broken.detail
    assert len(results) == len(RULE_LABELS)


# ---------------------------------------------------------------------------
# Agency eligibility
# ---------------------------------------------------------------------------
def test_conforming_loan_passes_eligibility():
    assert evaluate("agency_eligibility", facts()).outcome is Outcome.PASS


def test_conventional_above_the_county_limit_fails():
    r = evaluate("agency_eligibility", facts(amount=900_000.0))
    assert r.failed and "conforming limit" in r.detail


def test_jumbo_below_the_county_limit_fails():
    """The mislabelled-program case the reference design gets wrong at 2026 limits."""
    r = evaluate("agency_eligibility", facts(program="Jumbo", amount=625_000.0))
    assert r.failed and "within the" in r.detail


def test_fico_below_program_floor_fails():
    r = evaluate("agency_eligibility", facts(program="Jumbo", amount=900_000.0, fico=680))
    assert r.failed and "FICO 680" in r.detail


# ---------------------------------------------------------------------------
# DTI — recomputed, not read
# ---------------------------------------------------------------------------
def test_dti_recomputes_and_agrees_with_a_consistent_header():
    r = evaluate("dti_within_program", facts())
    assert r.outcome is Outcome.PASS and "38.0%" in r.detail


def test_dti_catches_a_header_that_disagrees_with_its_own_arithmetic():
    """A stated DTI the numbers do not support is a finding in itself."""
    r = evaluate("dti_within_program", facts(stated_dti=31.0))
    assert r.failed
    assert "disagrees with the stated" in r.detail
    assert r.suggests == "dti_breach"


def test_dti_over_program_cap_fails():
    r = evaluate("dti_within_program",
                 facts(program="FHA", piti=4_200.0, other_debts=645.0,
                       monthly_income=10_309.04, stated_dti=47.0))
    assert r.failed and "exceeds the FHA cap" in r.detail


def test_dti_with_no_income_is_indeterminate():
    r = evaluate("dti_within_program", facts(monthly_income=0.0))
    assert r.outcome is Outcome.INDETERMINATE


# ---------------------------------------------------------------------------
# LTV
# ---------------------------------------------------------------------------
def test_ltv_within_cap_passes():
    assert evaluate("ltv_within_program", facts()).outcome is Outcome.PASS


def test_ltv_over_jumbo_cap_fails():
    r = evaluate("ltv_within_program",
                 facts(program="Jumbo", amount=900_000.0, property_value=1_000_000.0))
    assert r.failed and r.suggests == "ltv_breach"


def test_va_at_one_hundred_ltv_passes():
    """VA allows 100%. A generic 95% cap would wrongly fail every VA purchase."""
    r = evaluate("ltv_within_program",
                 facts(program="VA", amount=245_000.0, property_value=245_000.0))
    assert r.outcome is Outcome.PASS


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------
def test_income_variance_within_tolerance_passes():
    r = evaluate("income_employment",
                 facts(fields={"paystub_monthly_income": "$6,000",
                               "w2_annual_wages": "$71,400"}))
    assert r.outcome is Outcome.PASS


def test_income_variance_above_tolerance_fails():
    r = evaluate("income_employment",
                 facts(fields={"paystub_monthly_income": "$6,520",
                               "w2_annual_wages": "$71,280"}))
    assert r.failed and r.suggests == "income_variance"


def test_missing_income_documents_fails_rather_than_indeterminate():
    """No W-2 on file is a known defect, not an unknown — it is a FAIL."""
    r = evaluate("income_employment", facts(doc_kinds=frozenset({"urla", "paystub"})))
    assert r.failed and r.suggests == "missing_document"


# ---------------------------------------------------------------------------
# Assets, TRID, flood, identity, collateral
# ---------------------------------------------------------------------------
def test_large_unsourced_deposit_fails():
    r = evaluate("asset_sourcing", facts(fields={"largest_deposit": "$61,000"}))
    assert r.failed and r.suggests == "unsourced_deposit"


def test_large_deposit_that_is_sourced_passes():
    r = evaluate("asset_sourcing",
                 facts(fields={"largest_deposit": "$61,000",
                               "largest_deposit_sourced": "true"}))
    assert r.outcome is Outcome.PASS


def test_small_deposit_passes():
    r = evaluate("asset_sourcing", facts(fields={"largest_deposit": "$1,200"}))
    assert r.outcome is Outcome.PASS


def test_zero_tolerance_fee_increase_fails_even_when_total_movement_is_small():
    """A $45 rise in a zero-tolerance fee is a cure regardless of the 10% bucket."""
    r = evaluate("trid_fee_tolerance",
                 facts(fields={"le_total_fees": "1250", "cd_total_fees": "1295",
                               "zero_tolerance_increase": "45"}))
    assert r.failed and "cure is owed" in r.detail


def test_cumulative_fee_movement_within_ten_percent_passes():
    r = evaluate("trid_fee_tolerance",
                 facts(fields={"le_total_fees": "1250", "cd_total_fees": "1295"}))
    assert r.outcome is Outcome.PASS


def test_missing_flood_cert_and_zone_mismatch_are_different_findings():
    """They route differently, so they must not collapse into one exception type."""
    missing = evaluate("flood_eligibility", facts(doc_kinds=frozenset({"urla"})))
    mismatch = evaluate("flood_eligibility",
                        facts(fields={"flood_zone": "X", "prior_flood_zone": "AE"}))
    assert missing.suggests == "flood_cert_missing"
    assert mismatch.suggests == "flood_determination_mismatch"
    assert missing.suggests != mismatch.suggests


def test_ssn_mismatch_is_critical():
    r = evaluate("identity_cip",
                 facts(fields={"doc_ssn_last4": "1182", "app_ssn_last4": "1128"}))
    assert r.failed and r.suggested_severity is Severity.CRITICAL


def test_identity_evidence_never_contains_a_full_ssn():
    r = evaluate("identity_cip",
                 facts(fields={"doc_ssn_last4": "1182", "app_ssn_last4": "1128"}))
    assert "1182" in (r.evidence or "")
    assert len([c for c in (r.evidence or "") if c.isdigit()]) <= 8


def test_appraisal_shortfall_fails():
    r = evaluate("collateral_valuation",
                 facts(fields={"appraised_value": "588000", "contract_price": "625000"}))
    assert r.failed and "under contract" in r.detail


def test_high_cu_score_fails_even_when_value_supports():
    r = evaluate("collateral_valuation",
                 facts(fields={"appraised_value": "515000", "contract_price": "515000",
                               "cu_score": "3.1"}))
    assert r.failed and "CU score" in r.detail


def test_fha_file_without_a_case_number_is_incomplete():
    r = evaluate("document_completeness", facts(program="FHA"))
    assert r.failed and "fha_case" in r.detail


def test_va_file_without_a_coe_is_incomplete():
    """A generic checklist would pass this; entitlement evidence is VA-specific."""
    r = evaluate("document_completeness", facts(program="VA"))
    assert r.failed and "coe" in r.detail


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("$6,520.00", 6520.0), ("47.0%", 47.0), ("742", 742.0), (1234, 1234.0),
     ("  $1,249,125  ", 1249125.0)],
)
def test_currency_and_percent_strings_parse(raw, expected):
    f = facts(fields={"largest_deposit": raw})
    assert f.num("largest_deposit") == expected


@pytest.mark.parametrize("raw", ["", "n/a", "illegible", None, "—"])
def test_unparseable_values_are_missing_not_zero(raw):
    """Parsing a blurred OCR value as 0 would silently pass a sourcing check."""
    f = facts(fields={"largest_deposit": raw})
    assert f.num("largest_deposit") is None


# ---------------------------------------------------------------------------
# Illegible values and low-confidence extractions
#
# Both of these fabricated a number from a blurred W-2 in a real demo run, and
# the income rule reported a +96,547% variance on the strength of it.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["$128,\u258808.48", "1\u25933,708.48", "$12?,400", "illegible", "$123,45\u2588"],
)
def test_a_partially_illegible_value_is_not_a_number(raw):
    """Parsing the legible half of a redacted figure invents the other half."""
    f = facts(fields={"w2_annual_wages": raw})
    assert f.num("w2_annual_wages") is None


def test_an_illegible_w2_makes_the_income_rule_indeterminate_not_absurd():
    r = evaluate("income_employment", facts(fields={
        "paystub_monthly_income": "$10,309.04",
        "w2_annual_wages": "$128,\u258808.48",
    }))
    assert r.outcome is Outcome.INDETERMINATE
    assert "w2_annual_wages" in r.missing
    assert "not readable as a number" in r.detail


def test_a_withheld_extraction_says_why_rather_than_looking_absent():
    """"Not on file" and "on file but unreadable" need different remedies."""
    f = facts(
        fields={"paystub_monthly_income": "$10,309.04"},
        low_confidence={"w2_annual_wages": 0.35},
    )
    r = evaluate("income_employment", f)
    assert r.outcome is Outcome.INDETERMINATE
    assert "0.35 confidence" in r.detail
    assert "below the 0.60 floor" in r.detail


def test_a_confidently_extracted_value_still_works():
    r = evaluate("income_employment", facts(fields={
        "paystub_monthly_income": "$6,000",
        "w2_annual_wages": "$71,400",
    }))
    assert r.outcome is Outcome.PASS


def test_an_unreadable_input_suggests_the_ocr_type_rather_than_nothing():
    """Without a suggestion the agent invented a type from the rule id."""
    r = evaluate("income_employment", facts(
        fields={"paystub_monthly_income": "$10,309.04"},
        low_confidence={"w2_annual_wages": 0.35},
    ))
    assert r.outcome is Outcome.INDETERMINATE
    assert r.suggests == "low_confidence_ocr"
    assert r.suggested_severity is Severity.LOW


def test_an_input_that_was_never_extracted_suggests_nothing():
    """Absent and unreadable are different, and only one has an obvious remedy."""
    r = evaluate("income_employment", facts(fields={"paystub_monthly_income": "$6,000"}))
    assert r.outcome is Outcome.INDETERMINATE
    assert r.suggests is None
