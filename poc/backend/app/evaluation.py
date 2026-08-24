"""Scoring agent output against ground truth.

WHAT THIS IS FOR. Every bug found in this build so far was found by a person
clicking through the UI and noticing something odd — a fabricated income
variance, a rule id used as an exception type, an auto-lane defect that never
reached the auto lane. All three were visible in the data the whole time. This
module is the thing that looks.

GROUND TRUTH NEVER TOUCHES THE RUNNING SYSTEM. `ground_truth.json` is read from
disk by the evaluation script and passed in here. It is not seeded, not exposed
by a tool, and not importable from anything an agent can reach. If it ever
became a fact the pipeline could see, every number below would become a
measurement of leakage rather than of accuracy.

THREE OUTCOMES, NOT TWO — again. A planted defect can be found, missed, or
*found and mislabelled*. Collapsing the third into either of the others hides
the most actionable failure mode there is: the agent saw the problem, described
it correctly, and filed it under a type that routes it to the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence


class Verdict(StrEnum):
    EXACT = "exact"              # right defect, right type
    MISLABELLED = "mislabelled"  # right defect, wrong type
    MISSED = "missed"            # planted, never raised
    DUPLICATE = "duplicate"      # a second finding of an already-matched defect
    SPURIOUS = "spurious"        # raised, nothing planted

# DUPLICATE is separate from SPURIOUS on purpose. Both are unmatched findings,
# and lumping them together would report an over-flagging problem the system
# does not have. A duplicate means the agent found the same real defect twice —
# the remedy is to check `list_exceptions` before raising. A spurious finding
# means it invented a problem — the remedy is somewhere else entirely.


# The lane a planted defect was expected to end up in, in the vocabulary the
# generator uses, mapped onto what the system actually records.
def lane_of(exc: Mapping[str, Any]) -> str:
    if exc["lane"] == "auto":
        return "auto"
    return "hitl_supervisor" if exc["requires_sup"] else "hitl"


@dataclass(frozen=True, slots=True)
class Finding:
    """One scored pairing of a planted defect with what the agents produced."""

    loan_id: str
    verdict: Verdict
    planted_kind: str | None = None
    raised_type: str | None = None
    exception_id: str | None = None
    confidence: int | None = None
    expected_lane: str | None = None
    actual_lane: str | None = None
    detail: str = ""

    @property
    def detected(self) -> bool:
        """Found at all — exact or mislabelled. The defect did not get past us."""
        return self.verdict in (Verdict.EXACT, Verdict.MISLABELLED)

    @property
    def lane_correct(self) -> bool | None:
        if self.expected_lane is None or self.actual_lane is None:
            return None
        return self.expected_lane == self.actual_lane


@dataclass(slots=True)
class Report:
    loans_scored: list[str] = field(default_factory=list)
    loans_skipped: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    # --- counts ------------------------------------------------------------
    @property
    def planted(self) -> int:
        return len([f for f in self.findings if f.planted_kind is not None])

    @property
    def raised(self) -> int:
        return len([f for f in self.findings if f.exception_id is not None])

    @property
    def exact(self) -> int:
        return len([f for f in self.findings if f.verdict is Verdict.EXACT])

    @property
    def mislabelled(self) -> int:
        return len([f for f in self.findings if f.verdict is Verdict.MISLABELLED])

    @property
    def missed(self) -> int:
        return len([f for f in self.findings if f.verdict is Verdict.MISSED])

    @property
    def spurious(self) -> int:
        return len([f for f in self.findings if f.verdict is Verdict.SPURIOUS])

    @property
    def duplicates(self) -> int:
        return len([f for f in self.findings if f.verdict is Verdict.DUPLICATE])

    # --- rates -------------------------------------------------------------
    @property
    def recall(self) -> float:
        """Of what was planted, how much did we detect at all?

        Mislabelled counts as detected. A defect described correctly under the
        wrong type still reached a human; it reached the wrong queue.
        """
        return _pct(self.exact + self.mislabelled, self.planted)

    @property
    def type_accuracy(self) -> float:
        """Of what we detected, how much did we name correctly?"""
        return _pct(self.exact, self.exact + self.mislabelled)

    @property
    def precision(self) -> float:
        """Of what we raised, how much described a real defect?

        Duplicates count as correct here: they describe something genuinely
        planted. They are wasteful, not wrong, and they get their own line.

        A false positive is not free: it consumes an analyst, and a system that
        cries wolf gets its findings ignored — including the true ones.
        """
        return _pct(self.exact + self.mislabelled + self.duplicates, self.raised)

    @property
    def lane_accuracy(self) -> float:
        scored = [f for f in self.findings if f.lane_correct is not None]
        return _pct(len([f for f in scored if f.lane_correct]), len(scored))

    def calibration(self) -> list[dict[str, Any]]:
        """Is a confident finding more likely to be right than a hesitant one?

        If the true-positive rate is flat across the bands, the confidence
        number carries no information — and `AUTO_THRESHOLD`, which routes on
        exactly that number, is deciding on noise.
        """
        bands = ((90, 101, "90-100"), (80, 90, "80-89"), (60, 80, "60-79"), (0, 60, "<60"))
        out: list[dict[str, Any]] = []
        for low, high, label in bands:
            in_band = [f for f in self.findings
                       if f.confidence is not None and low <= f.confidence < high]
            if not in_band:
                out.append({"band": label, "n": 0, "correct_pct": None})
                continue
            correct = [f for f in in_band if f.verdict is Verdict.EXACT]
            out.append({
                "band": label,
                "n": len(in_band),
                "correct_pct": _pct(len(correct), len(in_band)),
            })
        return out

    def by_type(self) -> list[dict[str, Any]]:
        """Per planted type — which defects this system is bad at."""
        kinds = sorted({f.planted_kind for f in self.findings if f.planted_kind})
        rows = []
        for kind in kinds:
            group = [f for f in self.findings if f.planted_kind == kind]
            rows.append({
                "type": kind,
                "planted": len(group),
                "exact": len([f for f in group if f.verdict is Verdict.EXACT]),
                "mislabelled": len([f for f in group if f.verdict is Verdict.MISLABELLED]),
                "missed": len([f for f in group if f.verdict is Verdict.MISSED]),
            })
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "loans_scored": self.loans_scored,
            "loans_skipped": self.loans_skipped,
            "planted": self.planted,
            "raised": self.raised,
            "exact": self.exact,
            "mislabelled": self.mislabelled,
            "missed": self.missed,
            "spurious": self.spurious,
            "duplicates": self.duplicates,
            "recall_pct": self.recall,
            "type_accuracy_pct": self.type_accuracy,
            "precision_pct": self.precision,
            "lane_accuracy_pct": self.lane_accuracy,
            "calibration": self.calibration(),
            "by_type": self.by_type(),
            "findings": [
                {
                    "loan_id": f.loan_id, "verdict": str(f.verdict),
                    "planted_kind": f.planted_kind, "raised_type": f.raised_type,
                    "exception_id": f.exception_id, "confidence": f.confidence,
                    "expected_lane": f.expected_lane, "actual_lane": f.actual_lane,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def score_loan(
    loan_id: str,
    planted: Sequence[Mapping[str, Any]],
    raised: Sequence[Mapping[str, Any]],
    doc_kind_of: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Pair planted defects with raised exceptions for one loan.

    Two passes, and the order matters. Exact type matches are claimed first, so
    a defect is never consumed by a weaker match while its own exception is
    still available. Only then does the document-based pass run, which is what
    catches "saw it, named it wrong".
    """
    doc_kind_of = doc_kind_of or {}
    unclaimed = list(raised)
    findings: list[Finding] = []

    # Pass 1 — same type.
    remaining_defects = []
    for defect in planted:
        match = next((e for e in unclaimed if e["type"] == defect["kind"]), None)
        if match is None:
            remaining_defects.append(defect)
            continue
        unclaimed.remove(match)
        findings.append(_pair(loan_id, defect, match, Verdict.EXACT))

    # Pass 2 — same document, different type. The agent looked in the right
    # place and drew the wrong label.
    for defect in remaining_defects:
        match = next(
            (e for e in unclaimed
             if e.get("evidence_doc_id")
             and doc_kind_of.get(e["evidence_doc_id"]) == defect["doc"]),
            None,
        )
        if match is None:
            findings.append(Finding(
                loan_id=loan_id, verdict=Verdict.MISSED,
                planted_kind=defect["kind"], expected_lane=defect["lane_hint"],
                detail=defect.get("detail", ""),
            ))
            continue
        unclaimed.remove(match)
        findings.append(_pair(loan_id, defect, match, Verdict.MISLABELLED))

    # Anything left is either a second sighting of a defect already matched, or
    # a finding with nothing behind it.
    matched_kinds = {f.planted_kind for f in findings if f.detected}
    for exc in unclaimed:
        duplicate = exc["type"] in matched_kinds
        findings.append(Finding(
            loan_id=loan_id,
            verdict=Verdict.DUPLICATE if duplicate else Verdict.SPURIOUS,
            planted_kind=None,
            raised_type=exc["type"], exception_id=exc["id"],
            confidence=exc["confidence"], actual_lane=lane_of(exc),
            detail=exc.get("label", ""),
        ))
    return findings


def _pair(loan_id: str, defect: Mapping[str, Any], exc: Mapping[str, Any],
          verdict: Verdict) -> Finding:
    return Finding(
        loan_id=loan_id,
        verdict=verdict,
        planted_kind=defect["kind"],
        raised_type=exc["type"],
        exception_id=exc["id"],
        confidence=exc["confidence"],
        expected_lane=defect["lane_hint"],
        actual_lane=lane_of(exc),
        detail=defect.get("detail", ""),
    )


def build_report(
    truth: Mapping[str, Any],
    exceptions_by_loan: Mapping[str, Sequence[Mapping[str, Any]]],
    scanned: Iterable[str],
    doc_kind_of: Mapping[str, str] | None = None,
) -> Report:
    """Score every scanned loan. Unscanned loans are skipped, not counted as misses.

    Scoring a loan nobody looked at would report a recall failure for work that
    was never attempted, which would make the number meaningless the moment the
    book is larger than the demo run.
    """
    scanned_set = set(scanned)
    report = Report()

    for entry in truth["loans"]:
        loan_id = entry["loan_id"]
        if loan_id not in scanned_set:
            report.loans_skipped.append(loan_id)
            continue
        report.loans_scored.append(loan_id)
        report.findings.extend(score_loan(
            loan_id,
            entry["planted_defects"],
            exceptions_by_loan.get(loan_id, []),
            doc_kind_of,
        ))
    return report
