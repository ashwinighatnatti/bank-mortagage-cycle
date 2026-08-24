"""Tests for the scorer.

The scorer is the thing that tells you whether the rest of the system works, so
it gets its own tests — a scorer that flatters is worse than none, because the
number it prints is the one people stop looking past.
"""

from __future__ import annotations

from app.evaluation import Verdict, build_report, score_loan

DOC_KINDS = {
    "LN-1-w2": "w2",
    "LN-1-paystub": "paystub",
    "LN-1-urla": "urla",
    "LN-1-appraisal": "appraisal",
}


def planted(kind: str, doc: str, lane: str = "hitl", detail: str = "") -> dict:
    return {"kind": kind, "doc": doc, "lane_hint": lane, "detail": detail,
            "expected_severity": "High"}


def raised(exc_id: str, type_: str, doc: str, conf: int = 90,
           lane: str = "hitl", sup: bool = False) -> dict:
    return {"id": exc_id, "type": type_, "evidence_doc_id": doc, "confidence": conf,
            "lane": lane, "requires_sup": sup, "label": type_}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def test_the_same_type_is_an_exact_hit():
    out = score_loan("LN-1", [planted("dti_breach", "urla")],
                     [raised("EX-1", "dti_breach", "LN-1-urla")], DOC_KINDS)
    assert [f.verdict for f in out] == [Verdict.EXACT]


def test_the_right_document_with_the_wrong_type_is_mislabelled_not_missed():
    """The failure mode that sends a real finding to the wrong queue.

    Reporting this as a miss AND a false positive would double-count one error
    and hide what actually went wrong.
    """
    out = score_loan("LN-1", [planted("low_confidence_ocr", "w2")],
                     [raised("EX-1", "income_variance", "LN-1-w2")], DOC_KINDS)
    assert [f.verdict for f in out] == [Verdict.MISLABELLED]
    assert out[0].planted_kind == "low_confidence_ocr"
    assert out[0].raised_type == "income_variance"


def test_a_defect_nobody_raised_is_a_miss():
    out = score_loan("LN-1", [planted("dti_breach", "urla")], [], DOC_KINDS)
    assert [f.verdict for f in out] == [Verdict.MISSED]
    assert out[0].exception_id is None


def test_a_finding_with_nothing_planted_is_spurious():
    out = score_loan("LN-1", [], [raised("EX-1", "ltv_breach", "LN-1-urla")], DOC_KINDS)
    assert [f.verdict for f in out] == [Verdict.SPURIOUS]


def test_a_second_finding_of_a_matched_defect_is_a_duplicate_not_a_false_positive():
    """Found twice and invented are different problems with different remedies."""
    out = score_loan(
        "LN-1", [planted("dti_breach", "urla")],
        [raised("EX-1", "dti_breach", "LN-1-urla"),
         raised("EX-2", "dti_breach", "LN-1-urla")],
        DOC_KINDS,
    )
    assert sorted(str(f.verdict) for f in out) == ["duplicate", "exact"]


def test_an_exact_match_is_claimed_before_a_document_match():
    """Ordering matters: a weaker match must not consume a defect first."""
    out = score_loan(
        "LN-1",
        [planted("low_confidence_ocr", "w2"), planted("income_variance", "paystub")],
        [raised("EX-1", "income_variance", "LN-1-w2"),      # wrong doc, right type
         raised("EX-2", "low_confidence_ocr", "LN-1-w2")],  # right type
        DOC_KINDS,
    )
    verdicts = {f.planted_kind: f.verdict for f in out if f.planted_kind}
    assert verdicts["low_confidence_ocr"] is Verdict.EXACT
    assert verdicts["income_variance"] is Verdict.EXACT


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------
def test_lane_is_scored_against_what_the_defect_deserved():
    out = score_loan(
        "LN-1", [planted("dti_breach", "urla", lane="hitl_supervisor")],
        [raised("EX-1", "dti_breach", "LN-1-urla", lane="hitl", sup=True)], DOC_KINDS,
    )
    assert out[0].lane_correct is True


def test_a_defect_that_should_have_auto_repaired_but_went_to_a_human_is_flagged():
    out = score_loan(
        "LN-1", [planted("low_confidence_ocr", "w2", lane="auto")],
        [raised("EX-1", "low_confidence_ocr", "LN-1-w2", lane="hitl")], DOC_KINDS,
    )
    assert out[0].verdict is Verdict.EXACT       # found and named correctly
    assert out[0].lane_correct is False          # but routed wrong


def test_a_spurious_finding_has_no_lane_verdict():
    """There is no expectation to compare it against."""
    out = score_loan("LN-1", [], [raised("EX-1", "ltv_breach", "LN-1-urla")], DOC_KINDS)
    assert out[0].lane_correct is None


# ---------------------------------------------------------------------------
# Report arithmetic
# ---------------------------------------------------------------------------
TRUTH = {
    "loans": [
        {"loan_id": "LN-1", "planted_defects": [
            planted("dti_breach", "urla", "hitl_supervisor"),
            planted("low_confidence_ocr", "w2", "auto"),
        ]},
        {"loan_id": "LN-2", "planted_defects": [planted("ltv_breach", "urla")]},
    ]
}


def test_an_unscanned_loan_is_skipped_not_counted_as_a_miss():
    """Otherwise recall reports a failure for work nobody attempted."""
    report = build_report(
        TRUTH,
        {"LN-1": [raised("EX-1", "dti_breach", "LN-1-urla", lane="hitl", sup=True),
                  raised("EX-2", "low_confidence_ocr", "LN-1-w2", lane="auto")]},
        scanned=["LN-1"], doc_kind_of=DOC_KINDS,
    )
    assert report.loans_scored == ["LN-1"]
    assert report.loans_skipped == ["LN-2"]
    assert report.planted == 2          # not 3
    assert report.recall == 100.0
    assert report.missed == 0


def test_recall_counts_a_mislabelled_defect_as_detected():
    report = build_report(
        {"loans": [{"loan_id": "LN-1", "planted_defects": [
            planted("low_confidence_ocr", "w2", "auto")]}]},
        {"LN-1": [raised("EX-1", "income_variance", "LN-1-w2")]},
        scanned=["LN-1"], doc_kind_of=DOC_KINDS,
    )
    assert report.recall == 100.0        # it did reach a human
    assert report.type_accuracy == 0.0   # but under the wrong name


def test_precision_counts_duplicates_as_real_and_reports_them_separately():
    report = build_report(
        {"loans": [{"loan_id": "LN-1", "planted_defects": [planted("dti_breach", "urla")]}]},
        {"LN-1": [raised("EX-1", "dti_breach", "LN-1-urla"),
                  raised("EX-2", "dti_breach", "LN-1-urla"),
                  raised("EX-3", "ltv_breach", "LN-1-appraisal")]},
        scanned=["LN-1"], doc_kind_of=DOC_KINDS,
    )
    assert report.duplicates == 1
    assert report.spurious == 1
    assert report.precision == round(2 / 3 * 100, 1)


def test_calibration_bands_report_the_hit_rate_per_confidence_range():
    report = build_report(
        {"loans": [{"loan_id": "LN-1", "planted_defects": [planted("dti_breach", "urla")]}]},
        {"LN-1": [raised("EX-1", "dti_breach", "LN-1-urla", conf=95),
                  raised("EX-2", "ltv_breach", "LN-1-appraisal", conf=95)]},
        scanned=["LN-1"], doc_kind_of=DOC_KINDS,
    )
    top = next(b for b in report.calibration() if b["band"] == "90-100")
    assert top["n"] == 2 and top["correct_pct"] == 50.0
    empty = next(b for b in report.calibration() if b["band"] == "60-79")
    assert empty["n"] == 0 and empty["correct_pct"] is None


def test_a_report_with_nothing_in_it_does_not_divide_by_zero():
    report = build_report({"loans": []}, {}, scanned=[], doc_kind_of={})
    assert (report.recall, report.precision, report.type_accuracy,
            report.lane_accuracy) == (0.0, 0.0, 0.0, 0.0)


def test_the_report_serialises_for_tracking_over_time():
    report = build_report(
        TRUTH,
        {"LN-1": [raised("EX-1", "dti_breach", "LN-1-urla", lane="hitl", sup=True)]},
        scanned=["LN-1"], doc_kind_of=DOC_KINDS,
    )
    blob = report.to_dict()
    assert blob["recall_pct"] == 50.0
    assert blob["loans_skipped"] == ["LN-2"]
    assert len(blob["findings"]) == 2
