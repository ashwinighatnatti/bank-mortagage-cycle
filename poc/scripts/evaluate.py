"""STEP 7 — score what the agents found against what was actually planted.

    python scripts/evaluate.py                  # score the loans already scanned
    python scripts/evaluate.py --run            # scan every unscanned loan first
    python scripts/evaluate.py --json out.json  # also write the raw numbers

Scoring is free and reads only the database. `--run` spends money: roughly
$0.30-0.60 per loan for the four agents.

GROUND TRUTH IS READ HERE AND NOWHERE ELSE. `ground_truth.json` is never
seeded, never exposed by a tool and never placed in a prompt. If it leaked into
the pipeline, every number below would measure leakage rather than accuracy —
`verify_synthetic_data.py` asserts it has not.

WHAT TO LOOK AT, IN ORDER

  recall          did we find the problem at all
  type accuracy   having found it, did we name it correctly — this is where a
                  finding ends up in the wrong queue
  lane accuracy   did policy route it the way the defect deserved
  precision       how much of what we raised was real
  calibration     is a 95 more likely to be right than a 65? If the bands are
                  flat, the confidence number carries no information and
                  AUTO_THRESHOLD is routing on noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app import agents, evaluation, reporting, store        # noqa: E402
from app.db import init_db, session_scope                   # noqa: E402
from app.evaluation import Verdict                          # noqa: E402
from app.models import Document, Loan, Run                  # noqa: E402
from sqlmodel import select                                 # noqa: E402

G, R, Y, D, B, OFF = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
TRUTH = BACKEND / "data" / "ground_truth.json"

MARK = {
    Verdict.EXACT: f"{G}exact      {OFF}",
    Verdict.MISLABELLED: f"{Y}mislabelled{OFF}",
    Verdict.MISSED: f"{R}MISSED     {OFF}",
    Verdict.DUPLICATE: f"{Y}duplicate  {OFF}",
    Verdict.SPURIOUS: f"{R}spurious   {OFF}",
}


def bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return f"{D}{'·' * width}{OFF}  n/a"
    filled = round(pct / 100 * width)
    colour = G if pct >= 80 else Y if pct >= 50 else R
    return f"{colour}{'█' * filled}{OFF}{D}{'░' * (width - filled)}{OFF} {pct:5.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="scan every unscanned loan first (spends money)")
    parser.add_argument("--json", metavar="PATH", default=None)
    args = parser.parse_args()

    if not TRUTH.exists():
        print(f"no ground truth at {TRUTH}. Run scripts/generate_synthetic_data.py first.")
        return 1
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))

    init_db()
    with session_scope() as session:
        if args.run:
            unscanned = [l.id for l in session.exec(
                select(Loan).where(Loan.scanned == False).order_by(Loan.id)  # noqa: E712
            ).all()]
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            print(f"\n  scanning {len(unscanned)} loan(s) — this spends money\n")
            for loan_id in unscanned:
                print(f"  {B}{loan_id}{OFF} …", flush=True)
                out = agents.run_pipeline(session, loan_id,
                                          run_prefix=f"RUN-{stamp}-{loan_id[-4:]}")
                bad = ", ".join(f"{r.agent}({r.stopped})" for r in out.failed)
                print(f"     {out.tool_calls} tool calls · ${out.usd:.4f}"
                      + (f" · {R}{bad}{OFF}" if bad else ""))

        loans = session.exec(select(Loan).order_by(Loan.id)).all()
        scanned = [l.id for l in loans if l.scanned]
        doc_kind_of = {
            d.doc_id: d.kind for d in session.exec(select(Document)).all()
        }
        exceptions_by_loan = {
            l.id: [reporting.exception_view(e) for e in store.exceptions_for(session, l.id)]
            for l in loans
        }
        report = evaluation.build_report(truth, exceptions_by_loan, scanned, doc_kind_of)

        runs = session.exec(select(Run)).all()
        spend = sum(r.usd for r in runs)
        cache = sum(r.cache_read_tokens for r in runs)

    # ---------------------------------------------------------------- output
    print(f"\n{B}{'=' * 74}{OFF}")
    print(f"{B}  Evaluation — {len(report.loans_scored)} loan(s) scored, "
          f"{len(report.loans_skipped)} not yet scanned{OFF}")
    print(f"{B}{'=' * 74}{OFF}\n")

    print(f"  {'recall':<16}{bar(report.recall)}   "
          f"{report.exact + report.mislabelled}/{report.planted} planted defects detected")
    print(f"  {'type accuracy':<16}{bar(report.type_accuracy)}   "
          f"{report.exact} named correctly of {report.exact + report.mislabelled} detected")
    print(f"  {'lane accuracy':<16}{bar(report.lane_accuracy)}   "
          f"routed as the defect deserved")
    print(f"  {'precision':<16}{bar(report.precision)}   "
          f"{report.spurious} invented, {report.duplicates} duplicate(s)")

    print(f"\n  {B}confidence calibration{OFF}   "
          f"{D}a 95 should be right more often than a 65{OFF}")
    for band in report.calibration():
        label = f"{band['band']:>7} (n={band['n']})"
        print(f"  {label:<16}{bar(band['correct_pct'])}")

    print(f"\n  {B}by planted type{OFF}")
    print(f"  {'type':<32}{'planted':>8}{'exact':>7}{'mislab':>8}{'missed':>8}")
    for row in report.by_type():
        colour = R if row["missed"] else Y if row["mislabelled"] else G
        print(f"  {colour}{row['type']:<32}{OFF}{row['planted']:>8}{row['exact']:>7}"
              f"{row['mislabelled']:>8}{row['missed']:>8}")

    # An exact match can still be routed wrongly, and that is the more
    # interesting failure: the finding is right and it went to the wrong place.
    # Filtering on verdict alone hid every lane error behind a green tick, which
    # is how a 75% lane accuracy came with nothing listed under it.
    interesting = [f for f in report.findings
                   if f.verdict is not Verdict.EXACT or f.lane_correct is False]
    if interesting:
        print(f"\n  {B}everything that was not a clean hit{OFF}")
        for f in interesting:
            mark = (f"{Y}wrong lane {OFF}" if f.verdict is Verdict.EXACT
                    else MARK[f.verdict])
            print(f"  {mark} {f.loan_id}  "
                  f"planted={f.planted_kind or '-':<24} raised={f.raised_type or '-'}")
            if f.lane_correct is False:
                print(f"              {R}lane: expected {f.expected_lane}, "
                      f"got {f.actual_lane}{OFF}")
            if f.detail:
                print(f"              {D}{f.detail[:96]}{OFF}")

    if report.loans_skipped:
        print(f"\n  {D}not scanned, so not scored: "
              f"{', '.join(report.loans_skipped)}{OFF}")
        print(f"  {D}re-run with --run to include them{OFF}")

    print(f"\n  {B}cost{OFF}  ${spend:.4f} across {len(runs)} agent run(s) · "
          f"{cache:,} tokens read from cache")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        print(f"  wrote {args.json}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
