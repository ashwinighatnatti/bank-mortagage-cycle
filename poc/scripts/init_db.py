"""STEP 3 — create the database and load the synthetic book.

    python scripts/init_db.py            # create + seed, idempotent
    python scripts/init_db.py --check    # report state, verify the chain, no writes

Safe to re-run. Nothing here drops a table: the audit chain is the one artefact
in this POC that cannot be regenerated, and a --reset flag next to a seeder is
how it gets destroyed by muscle memory. To start over, delete the .db file
deliberately.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import get_settings          # noqa: E402
from app.db import init_db, session_scope    # noqa: E402
from app.models import (                     # noqa: E402
    AuditEntry, Document, ExceptionRecord, Loan, Note,
)
from app.rules import RULE_LABELS, Outcome, evaluate_all  # noqa: E402
from app.seed import seed_all                # noqa: E402
from app.store import build_facts, verify_audit_chain     # noqa: E402
from sqlmodel import func, select            # noqa: E402


def count(session, model) -> int:
    return session.exec(select(func.count()).select_from(model)).one()


def report(session) -> None:
    print("\n  Database contents")
    for model, label in (
        (Loan, "loans"), (Document, "documents"), (ExceptionRecord, "exceptions"),
        (Note, "notes"), (AuditEntry, "audit rows"),
    ):
        print(f"    {label:<14} {count(session, model):>4}")

    ok, broken = verify_audit_chain(session)
    print(f"\n  Audit chain     {'intact' if ok else f'BROKEN at {broken}'}")


def rules_preview(session) -> None:
    """Run every rule against every loan on the raw header, before extraction.

    Expect a wall of INDETERMINATE. That is the point: nothing has been
    extracted yet, so almost nothing is checkable, and the engine says so
    instead of reporting a clean file.
    """
    loans = session.exec(select(Loan).order_by(Loan.id)).all()
    tally = {o: 0 for o in Outcome}
    fails: list[str] = []

    for loan in loans:
        for result in evaluate_all(build_facts(session, loan.id)):
            tally[result.outcome] += 1
            if result.failed:
                fails.append(f"    {loan.id}  {RULE_LABELS[result.rule_id]}: {result.detail}")

    print(f"\n  Rules engine    {len(loans)} loans x {len(RULE_LABELS)} rules")
    print(f"    pass {tally[Outcome.PASS]:>3}   fail {tally[Outcome.FAIL]:>3}   "
          f"indeterminate {tally[Outcome.INDETERMINATE]:>3}")
    if fails:
        print("\n  Failing on the loan header alone (no extraction yet):")
        for line in fails[:12]:
            print(line)
        if len(fails) > 12:
            print(f"    ... and {len(fails) - 12} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report state and verify the chain without writing")
    args = parser.parse_args()

    settings = get_settings()
    # The resolved URL, not the raw setting: a relative DATABASE_URL is
    # anchored at poc/, and printing the raw value would show a path that
    # means something different depending on where you ran this from.
    print(f"  {settings.resolved_database_url()}")

    init_db()
    with session_scope() as session:
        if not args.check:
            added = seed_all(session)
            print(f"\n  Seeded          {added['loans']} loans, "
                  f"{added['documents']} documents, {added['notes']} notes"
                  + ("  (already present)" if not any(added.values()) else ""))
        report(session)
        rules_preview(session)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
