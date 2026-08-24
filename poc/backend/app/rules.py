"""The deterministic rules engine — arithmetic and guideline checks, no model.

This is the layer that can contradict Claude. An agent reads a document and
forms a belief with a confidence; a rule recomputes the same quantity from the
extracted numbers and either agrees or does not. When it does not,
`store.revise_finding()` lowers the confidence — never raises it.

THE DESIGN DECISION THAT MATTERS HERE IS `INDETERMINATE`.

A rule with a missing input returns INDETERMINATE, never PASS. This sounds
obvious and is the single easiest thing to get wrong: if `dti_within_program`
returned PASS when `monthly_income` was never extracted, a loan with no income
documentation at all would sail through the capacity check clean. Every rule
below states its required facts up front and refuses to run without them, so
"we could not check" is structurally distinct from "we checked and it is fine".

Rules are pure functions over `Facts`. They do not touch the database, do not
call a model, and do not raise — an exception inside a rule is caught by
`evaluate()` and reported as INDETERMINATE with the error text, because one
broken rule must not abort a whole validation pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping

from .market_data import CU_SCORE_THRESHOLD, PROGRAM_LIMITS, conforming_limit
from .policy import Severity


# An extraction the agent itself barely believes must not drive arithmetic.
# Below this, the value is withheld from the rules engine and the affected rule
# reports INDETERMINATE — naming the confidence, so a human can tell "we could
# not read it" apart from "it was never on file".
EXTRACTION_CONFIDENCE_FLOOR = 0.60


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class RuleResult:
    """What one rule concluded, and what it thinks should follow.

    `suggests` and `suggested_severity` are advisory. A rule never creates an
    exception itself — the Validation Agent decides whether to raise one, and
    `policy.decide_disposition()` decides the lane. Keeping the rule advisory
    means the same engine can run in "check only" mode against ground truth
    without writing anything.
    """

    rule_id: str
    outcome: Outcome
    detail: str
    evidence: str | None = None
    suggests: str | None = None
    suggested_severity: Severity | None = None
    missing: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL


# ---------------------------------------------------------------------------
# Facts — everything a rule is allowed to see
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Facts:
    """The loan header plus whatever has actually been extracted so far.

    Deliberately a snapshot rather than a live session: a rule cannot widen its
    own inputs by querying for more, which is what keeps it reproducible in the
    evaluation pass.
    """

    loan_id: str
    program: str
    purpose: str
    metro: str
    amount: float
    property_value: float
    fico: int
    stated_ltv: float
    stated_dti: float
    monthly_income: float
    piti: float
    other_debts: float
    doc_kinds: frozenset[str] = frozenset()
    fields: Mapping[str, Any] = field(default_factory=dict)
    field_docs: Mapping[str, str] = field(default_factory=dict)
    # doc_id -> the injected-instruction markers found in it, from a Python
    # scan of the raw text. Populated by store.build_facts.
    injection_hits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # name -> confidence, for extractions withheld by the floor above. They are
    # recorded rather than dropped so a rule can say WHY it could not run.
    low_confidence: Mapping[str, float] = field(default_factory=dict)

    def num(self, name: str) -> float | None:
        """A numeric extracted field, or None if absent or unparseable."""
        return _num(self.fields.get(name))

    def has(self, *names: str) -> tuple[str, ...]:
        """Which of these fact names are missing. Empty tuple means all present."""
        return tuple(n for n in names if self.num(n) is None and not self.fields.get(n))

    def why_missing(self, name: str) -> str:
        """Describe an absent fact. Not-on-file and not-legible are different."""
        conf = self.low_confidence.get(name)
        if conf is not None:
            return (f"{name} (extracted at {conf:.2f} confidence, below the "
                    f"{EXTRACTION_CONFIDENCE_FLOOR:.2f} floor)")
        if name in self.fields:
            return f"{name} (present but not readable as a number)"
        return name


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")

# Glyphs and words a degraded scan leaves where a digit should be. A value
# carrying one of these is not a number with a blemish, it is an unknown number.
#
# This cost a real demo run. Intake correctly recorded a blurred W-2 as
# "$128,█08.48" at 0.35 confidence — exactly as instructed — and this function
# happily returned 128. The income rule divided that by twelve and reported a
# +96,547% variance, which an agent then had to spend a turn explaining. A
# fabricated number is worse than a missing one: a missing one stops the check.
_ILLEGIBLE = ("█", "▓", "▒", "░", "■", "?", "…", "illegible", "unreadable", "redacted")


def _num(value: Any) -> float | None:
    """Parse a number out of an extracted value.

    Extracted values are strings from documents: '$6,520.00', '47.0%', '742'.
    Returns None rather than guessing — an unparseable value is a missing fact,
    and a missing fact makes its rule INDETERMINATE.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value)
    lowered = text.casefold()
    if any(marker in text or marker in lowered for marker in _ILLEGIBLE):
        return None

    m = _NUM_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _indeterminate(
    rule_id: str, missing: tuple[str, ...], what: str, f: "Facts | None" = None
) -> RuleResult:
    described = [f.why_missing(n) for n in missing] if f else list(missing)

    # An input withheld because it could not be read reliably is not a mystery:
    # the remedy is a better scan. Suggesting the type here is what stopped an
    # agent inventing one from the rule id when a rule came back indeterminate.
    withheld = [n for n in missing if f and n in f.low_confidence] if f else []
    return RuleResult(
        rule_id=rule_id,
        outcome=Outcome.INDETERMINATE,
        detail=(
            f"cannot evaluate {what}: missing {', '.join(described)}. "
            "Not a pass — the check did not run."
        ),
        missing=missing,
        suggests="low_confidence_ocr" if withheld else None,
        suggested_severity=Severity.LOW if withheld else None,
    )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
def agency_eligibility(f: Facts) -> RuleResult:
    """Fannie/Freddie eligibility: loan size against the county limit, FICO floor.

    Catches the mislabelled-program case — a loan booked as Conv above the
    county conforming limit is not conforming, whatever the file says.
    """
    limits = PROGRAM_LIMITS.get(f.program)
    if limits is None:
        return _indeterminate("agency_eligibility", (f"program limits for {f.program}",),
                              "agency eligibility")

    cll = conforming_limit(f.metro)
    problems: list[str] = []

    if f.program in ("Conv", "FHA", "VA") and f.amount > cll:
        problems.append(
            f"amount ${f.amount:,.0f} exceeds the {f.metro} conforming limit ${cll:,.0f}"
        )
    if f.program == "Jumbo" and f.amount <= cll:
        problems.append(
            f"booked as Jumbo but ${f.amount:,.0f} is within the {f.metro} "
            f"conforming limit ${cll:,.0f}"
        )
    if f.fico < limits.fico_floor:
        problems.append(f"FICO {f.fico} below the {f.program} floor {limits.fico_floor}")

    if problems:
        return RuleResult(
            "agency_eligibility", Outcome.FAIL,
            "; ".join(problems),
            evidence=f"{f.program} · ${f.amount:,.0f} · FICO {f.fico} · CLL ${cll:,.0f}",
            suggests="program_eligibility",
            suggested_severity=Severity.HIGH,
        )
    return RuleResult(
        "agency_eligibility", Outcome.PASS,
        f"{limits.label}: ${f.amount:,.0f} within ${cll:,.0f}, FICO {f.fico} "
        f">= {limits.fico_floor}",
        evidence=f"CLL ${cll:,.0f} · FICO floor {limits.fico_floor}",
    )


def income_employment(f: Facts) -> RuleResult:
    """Qualifying income: annualised paystub against the prior-year W-2.

    A gap either way is a reconciliation question, not a computation — this is
    exactly the sort of finding that is never auto-dispositioned.
    """
    required_docs = {"w2", "paystub"}
    absent = required_docs - f.doc_kinds
    if absent:
        return RuleResult(
            "income_employment", Outcome.FAIL,
            f"income documentation incomplete: no {', '.join(sorted(absent))} on file",
            evidence=f"documents present: {', '.join(sorted(f.doc_kinds)) or 'none'}",
            suggests="missing_document",
            suggested_severity=Severity.MEDIUM,
        )

    missing = f.has("paystub_monthly_income", "w2_annual_wages")
    if missing:
        return _indeterminate("income_employment", missing, "income variance", f)

    paystub_monthly = f.num("paystub_monthly_income") or 0.0
    w2_monthly = (f.num("w2_annual_wages") or 0.0) / 12
    if w2_monthly <= 0:
        return _indeterminate("income_employment", ("w2_annual_wages",), "income variance", f)

    variance = (paystub_monthly - w2_monthly) / w2_monthly * 100
    evidence = f"Paystub ${paystub_monthly:,.0f}/mo · W-2 ${w2_monthly:,.0f}/mo"

    # 5% is the conventional reconciliation trigger for variable income.
    if abs(variance) >= 5.0:
        return RuleResult(
            "income_employment", Outcome.FAIL,
            f"paystub annualises {variance:+.1f}% against the W-2; qualifying "
            "income basis needs analyst reconciliation",
            evidence=evidence,
            suggests="income_variance",
            suggested_severity=Severity.HIGH if abs(variance) >= 10 else Severity.MEDIUM,
        )
    return RuleResult("income_employment", Outcome.PASS,
                      f"paystub within {variance:+.1f}% of W-2", evidence=evidence)


def dti_within_program(f: Facts) -> RuleResult:
    """Recompute back-end DTI and test it against the program cap.

    Recomputed, not read. If the file says 38% and (PITI + debts) / income says
    44%, the file is wrong and the rule says so — that disagreement is the whole
    reason this engine exists.
    """
    limits = PROGRAM_LIMITS.get(f.program)
    if limits is None:
        return _indeterminate("dti_within_program", (f"program limits for {f.program}",), "DTI")
    if f.monthly_income <= 0:
        return _indeterminate("dti_within_program", ("monthly_income",), "DTI")

    computed = (f.piti + f.other_debts) / f.monthly_income * 100
    evidence = (
        f"(PITI ${f.piti:,.0f} + debts ${f.other_debts:,.0f}) / "
        f"income ${f.monthly_income:,.0f} = {computed:.1f}% · cap {limits.dti_cap:.1f}%"
    )

    drift = abs(computed - f.stated_dti)
    if drift > 1.0:
        return RuleResult(
            "dti_within_program", Outcome.FAIL,
            f"recomputed DTI {computed:.1f}% disagrees with the stated "
            f"{f.stated_dti:.1f}% by {drift:.1f}pp",
            evidence=evidence,
            suggests="dti_breach",
            suggested_severity=Severity.HIGH,
        )
    if computed > limits.dti_cap:
        return RuleResult(
            "dti_within_program", Outcome.FAIL,
            f"back-end DTI {computed:.1f}% exceeds the {f.program} cap "
            f"{limits.dti_cap:.1f}%",
            evidence=evidence,
            suggests="dti_breach",
            suggested_severity=Severity.HIGH,
        )
    return RuleResult("dti_within_program", Outcome.PASS,
                      f"DTI {computed:.1f}% within the {f.program} cap "
                      f"{limits.dti_cap:.1f}%", evidence=evidence)


def ltv_within_program(f: Facts) -> RuleResult:
    """Recompute LTV from amount and value, then test the program cap."""
    limits = PROGRAM_LIMITS.get(f.program)
    if limits is None:
        return _indeterminate("ltv_within_program", (f"program limits for {f.program}",), "LTV")
    if f.property_value <= 0:
        return _indeterminate("ltv_within_program", ("property_value",), "LTV")

    computed = f.amount / f.property_value * 100
    evidence = (
        f"${f.amount:,.0f} / ${f.property_value:,.0f} = {computed:.1f}% · "
        f"cap {limits.ltv_cap:.1f}%"
    )
    if computed > limits.ltv_cap + 0.05:
        return RuleResult(
            "ltv_within_program", Outcome.FAIL,
            f"LTV {computed:.1f}% exceeds the {f.program} cap {limits.ltv_cap:.1f}%",
            evidence=evidence, suggests="ltv_breach", suggested_severity=Severity.HIGH,
        )
    return RuleResult("ltv_within_program", Outcome.PASS,
                      f"LTV {computed:.1f}% within cap", evidence=evidence)


def asset_sourcing(f: Facts) -> RuleResult:
    """Large-deposit sourcing on the most recent bank statement.

    Agency practice treats a single deposit above 50% of monthly qualifying
    income as needing documentation. Below that it is not evidence of anything.
    """
    if "bank_statement" not in f.doc_kinds:
        return RuleResult(
            "asset_sourcing", Outcome.FAIL,
            "no bank statement on file; assets cannot be sourced",
            evidence="documents present: " + (", ".join(sorted(f.doc_kinds)) or "none"),
            suggests="missing_document", suggested_severity=Severity.MEDIUM,
        )

    largest = f.num("largest_deposit")
    if largest is None:
        return _indeterminate("asset_sourcing", ("largest_deposit",), "deposit sourcing", f)
    if f.monthly_income <= 0:
        return _indeterminate("asset_sourcing", ("monthly_income",), "deposit sourcing", f)

    threshold = f.monthly_income * 0.5
    sourced = str(f.fields.get("largest_deposit_sourced", "")).lower() in ("true", "yes", "sourced")
    evidence = f"largest deposit ${largest:,.0f} · 50% of income ${threshold:,.0f}"

    if largest > threshold and not sourced:
        return RuleResult(
            "asset_sourcing", Outcome.FAIL,
            f"deposit of ${largest:,.0f} exceeds 50% of monthly income and has "
            "no payroll or asset trail",
            evidence=evidence, suggests="unsourced_deposit",
            suggested_severity=Severity.MEDIUM,
        )
    return RuleResult("asset_sourcing", Outcome.PASS,
                      "no unsourced deposit above the documentation threshold",
                      evidence=evidence)


def trid_fee_tolerance(f: Facts) -> RuleResult:
    """LE vs CD fee movement against the TRID tolerance buckets.

    Zero-tolerance fees may not increase at all; the 10% cumulative bucket may
    rise 10% in aggregate. Anything above either is a cure the lender owes.
    """
    missing = f.has("le_total_fees", "cd_total_fees")
    if missing:
        return _indeterminate("trid_fee_tolerance", missing, "fee tolerance", f)

    le = f.num("le_total_fees") or 0.0
    cd = f.num("cd_total_fees") or 0.0
    if le <= 0:
        return _indeterminate("trid_fee_tolerance", ("le_total_fees",), "fee tolerance", f)

    zero_increase = f.num("zero_tolerance_increase") or 0.0
    delta = cd - le
    pct = delta / le * 100
    evidence = f"LE ${le:,.0f} → CD ${cd:,.0f} ({pct:+.1f}%)"

    if zero_increase > 0:
        return RuleResult(
            "trid_fee_tolerance", Outcome.FAIL,
            f"zero-tolerance fees increased by ${zero_increase:,.0f}; a cure is owed",
            evidence=evidence, suggests="fee_tolerance_variance",
            suggested_severity=Severity.HIGH,
        )
    if pct > 10.0:
        return RuleResult(
            "trid_fee_tolerance", Outcome.FAIL,
            f"cumulative fees rose {pct:.1f}%, above the 10% bucket",
            evidence=evidence, suggests="fee_tolerance_variance",
            suggested_severity=Severity.MEDIUM,
        )
    return RuleResult("trid_fee_tolerance", Outcome.PASS,
                      f"fee movement {pct:+.1f}% within tolerance", evidence=evidence)


def flood_eligibility(f: Facts) -> RuleResult:
    """A current flood determination must exist, and its zone must agree.

    Two distinct failures with different lanes: a missing certificate can be
    auto-ordered, a zone that disagrees with the prior determination cannot be
    resolved by ordering the same thing again.
    """
    if "flood_cert" not in f.doc_kinds:
        return RuleResult(
            "flood_eligibility", Outcome.FAIL,
            "no active flood determination on file",
            evidence="flood cert: not ordered",
            suggests="flood_cert_missing", suggested_severity=Severity.MEDIUM,
        )

    cert_zone = f.fields.get("flood_zone")
    prior_zone = f.fields.get("prior_flood_zone")
    if not cert_zone:
        return _indeterminate("flood_eligibility", ("flood_zone",), "flood zone", f)
    if not prior_zone:
        return RuleResult("flood_eligibility", Outcome.PASS,
                          f"flood determination on file, zone {cert_zone}, "
                          "no prior determination to reconcile",
                          evidence=f"zone {cert_zone}")

    if str(cert_zone).strip().upper() != str(prior_zone).strip().upper():
        return RuleResult(
            "flood_eligibility", Outcome.FAIL,
            f"flood zone {cert_zone} disagrees with the prior determination {prior_zone}",
            evidence=f"Zone {cert_zone} vs {prior_zone} (stale)",
            suggests="flood_determination_mismatch", suggested_severity=Severity.LOW,
        )
    return RuleResult("flood_eligibility", Outcome.PASS,
                      f"flood zone {cert_zone} consistent", evidence=f"zone {cert_zone}")


def identity_cip(f: Facts) -> RuleResult:
    """CIP: the SSN on the documents must match the application.

    Compares the trailing four digits only — the full number is never extracted
    into a field, and `audit.redact_detail()` would strip it if it were.
    """
    doc_ssn = f.fields.get("doc_ssn_last4")
    app_ssn = f.fields.get("app_ssn_last4")
    missing = tuple(n for n, v in (("doc_ssn_last4", doc_ssn), ("app_ssn_last4", app_ssn)) if not v)
    if missing:
        return _indeterminate("identity_cip", missing, "identity match", f)

    d, a = str(doc_ssn).strip()[-4:], str(app_ssn).strip()[-4:]
    if d != a:
        return RuleResult(
            "identity_cip", Outcome.FAIL,
            "SSN trailing digits differ between document and application",
            evidence=f"Doc ****-{d} vs app ****-{a}",
            suggests="identity_mismatch", suggested_severity=Severity.CRITICAL,
        )
    return RuleResult("identity_cip", Outcome.PASS, "identity consistent across documents",
                      evidence=f"SSN ****-{d} matches")


def collateral_valuation(f: Facts) -> RuleResult:
    """Appraised value against contract price, plus the CU score.

    A value shortfall changes the deal, not just the file, which is why
    `appraisal_variance` is in NEVER_AUTO.
    """
    if "appraisal" not in f.doc_kinds:
        return RuleResult(
            "collateral_valuation", Outcome.FAIL,
            "no appraisal on file",
            evidence="appraisal: not received",
            suggests="missing_document", suggested_severity=Severity.MEDIUM,
        )

    appraised = f.num("appraised_value")
    contract = f.num("contract_price") or f.property_value
    if appraised is None or contract <= 0:
        return _indeterminate("collateral_valuation", ("appraised_value",), "valuation", f)

    variance = (appraised - contract) / contract * 100
    cu = f.num("cu_score")
    evidence = f"Contract ${contract:,.0f} · Appraisal ${appraised:,.0f}" + (
        f" · CU {cu:.1f}" if cu is not None else ""
    )

    problems: list[str] = []
    if variance <= -3.0:
        problems.append(f"appraised {abs(variance):.1f}% under contract")
    if cu is not None and cu > CU_SCORE_THRESHOLD:
        problems.append(f"CU score {cu:.1f} above the {CU_SCORE_THRESHOLD} threshold")

    if problems:
        return RuleResult(
            "collateral_valuation", Outcome.FAIL, "; ".join(problems),
            evidence=evidence, suggests="appraisal_variance",
            suggested_severity=Severity.HIGH,
        )
    return RuleResult("collateral_valuation", Outcome.PASS,
                      f"valuation supports the transaction ({variance:+.1f}%)",
                      evidence=evidence)


def document_completeness(f: Facts) -> RuleResult:
    """Every document the program requires is on file.

    Program-specific: an FHA file needs its case number, a VA file its COE. A
    generic checklist would pass a VA loan with no entitlement evidence.
    """
    base = {"urla", "id", "w2", "paystub", "bank_statement", "credit_report", "hoi"}
    per_program = {
        "FHA": {"fha_case", "appraisal"},
        "VA": {"coe", "appraisal"},
        "Conv": {"appraisal"},
        "Jumbo": {"appraisal", "title_commitment"},
    }
    required = base | per_program.get(f.program, set())
    absent = sorted(required - f.doc_kinds)
    evidence = f"{len(f.doc_kinds)} documents on file"

    if absent:
        return RuleResult(
            "document_completeness", Outcome.FAIL,
            f"missing for a {f.program} file: {', '.join(absent)}",
            evidence=evidence, suggests="missing_document",
            suggested_severity=Severity.MEDIUM if len(absent) < 3 else Severity.HIGH,
        )
    return RuleResult("document_completeness", Outcome.PASS,
                      f"all {f.program} required documents present", evidence=evidence)


def document_integrity(f: Facts) -> RuleResult:
    """Do any documents on this file contain injected instructions?

    A PASS here means "no known marker was found", never "this document is
    safe". The scanner is a tripwire over known phrasings and a careful attempt
    will step over it, so this rule is not permitted to be read as clearance —
    the same discipline that keeps INDETERMINATE distinct from a pass.
    """
    if not f.doc_kinds:
        return _indeterminate("document_integrity", ("documents",), "document integrity")

    if not f.injection_hits:
        return RuleResult(
            "document_integrity", Outcome.PASS,
            "no known injected-instruction pattern found in the documents on file",
            evidence=f"{len(f.doc_kinds)} document kinds scanned",
        )

    docs = sorted(f.injection_hits)
    markers = sorted({m for hits in f.injection_hits.values() for m in hits})
    return RuleResult(
        "document_integrity", Outcome.FAIL,
        f"{len(docs)} document(s) contain text that instructs the reader: "
        f"{', '.join(docs)}. Patterns matched: {', '.join(markers)}",
        # Names the document and how much of the scanner it tripped. The
        # offending line itself belongs on the exception, quoted by whoever
        # raises it — evidence here is a pointer, not a transcript.
        evidence="; ".join(
            f"{doc_id} tripped {len(hits)} pattern(s)"
            for doc_id, hits in sorted(f.injection_hits.items())
        ),
        suggests="prompt_injection",
        suggested_severity=Severity.HIGH,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
Rule = Callable[[Facts], RuleResult]

RULES: dict[str, Rule] = {
    "agency_eligibility": agency_eligibility,
    "income_employment": income_employment,
    "dti_within_program": dti_within_program,
    "ltv_within_program": ltv_within_program,
    "asset_sourcing": asset_sourcing,
    "trid_fee_tolerance": trid_fee_tolerance,
    "flood_eligibility": flood_eligibility,
    "identity_cip": identity_cip,
    "collateral_valuation": collateral_valuation,
    "document_completeness": document_completeness,
    "document_integrity": document_integrity,
}

# Shown in the Rules Engine panel, in this order.
RULE_LABELS: dict[str, str] = {
    "agency_eligibility": "Fannie Mae / Freddie Mac eligibility",
    "income_employment": "Income & employment (4506-C / VOE)",
    "dti_within_program": "DTI within program limits",
    "ltv_within_program": "LTV within program limits",
    "asset_sourcing": "Asset sourcing & reserves",
    "trid_fee_tolerance": "TRID disclosure timing & fee tolerance",
    "flood_eligibility": "Flood / property eligibility",
    "identity_cip": "CFPB / identity (CIP)",
    "collateral_valuation": "Collateral valuation & CU score",
    "document_completeness": "Document completeness",
    "document_integrity": "Document integrity (injected instructions)",
}


def evaluate(rule_id: str, facts: Facts) -> RuleResult:
    """Run one rule. Never raises.

    A rule that throws is reported as INDETERMINATE rather than propagating: a
    bug in `trid_fee_tolerance` must not take down a validation pass that would
    otherwise have caught an identity mismatch.
    """
    rule = RULES.get(rule_id)
    if rule is None:
        return RuleResult(rule_id, Outcome.INDETERMINATE,
                          f"no such rule: {rule_id!r}. Known rules: "
                          f"{', '.join(sorted(RULES))}")
    try:
        return rule(facts)
    except Exception as exc:  # noqa: BLE001 — deliberate, see docstring
        return RuleResult(rule_id, Outcome.INDETERMINATE,
                          f"rule raised {type(exc).__name__}: {exc}")


def evaluate_all(facts: Facts) -> list[RuleResult]:
    """Every rule, in panel order."""
    return [evaluate(rid, facts) for rid in RULE_LABELS]
