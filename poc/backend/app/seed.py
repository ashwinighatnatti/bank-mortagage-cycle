"""Load the generated synthetic book into the database.

Idempotent: running it twice does not duplicate anything and does not reset
pipeline state on loans already in flight. That matters more than it sounds —
the demo will be re-seeded on a laptop minutes before it is shown, and a seeder
that silently wiped `ready`/`decision` would erase the run someone just did.

What this deliberately does NOT load: `ground_truth.json`. It never enters the
database, because anything in the database can end up in a context pack, and a
context pack containing the answer key makes every accuracy number meaningless.
Ground truth is read only by the evaluation script, straight from disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from .models import Document, Loan, Note
from .store import append_audit

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def load_book(data_dir: Path | None = None) -> dict[str, Any]:
    """Read loans.json. Raises with a useful message if step 2 was never run."""
    path = (data_dir or DATA_DIR) / "loans.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Generate the book first:\n"
            "    python scripts/generate_synthetic_data.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def seed_loans(session: Session, data_dir: Path | None = None) -> tuple[int, int]:
    """Insert loans and documents. Returns (loans_added, documents_added)."""
    book = load_book(data_dir)
    loans_added = docs_added = 0

    for row in book["loans"]:
        if session.get(Loan, row["id"]) is None:
            session.add(
                Loan(
                    id=row["id"],
                    borrowers=row["borrowers"],
                    metro=row["metro"],
                    program=row["program"],
                    purpose=row["purpose"],
                    amount=row["amount"],
                    property_value=row["property_value"],
                    fico=row["fico"],
                    ltv=row["ltv"],
                    dti=row["dti"],
                    note_rate=row["note_rate"],
                    monthly_income=row["monthly_income"],
                    piti=row["piti"],
                    other_debts=row["other_debts"],
                    conforming_limit=row["conforming_limit"],
                    is_jumbo=row["is_jumbo"],
                )
            )
            loans_added += 1
        session.flush()

        for doc in row["documents"]:
            if session.get(Document, doc["doc_id"]) is None:
                session.add(
                    Document(
                        doc_id=doc["doc_id"],
                        loan_id=row["id"],
                        kind=doc["kind"],
                        # Stored as posix so the same database file works on
                        # Windows and in the Linux container.
                        path=doc["path"].replace("\\", "/"),
                        chars=doc["chars"],
                    )
                )
                docs_added += 1
    session.flush()
    return loans_added, docs_added


# ---------------------------------------------------------------------------
# Operational memory
#
# Seeded from prior human resolutions, which is the only source `Note` accepts.
# These give `recall_notes` something true to return on the first run, so the
# memory tool is exercised in the demo rather than sitting empty.
# ---------------------------------------------------------------------------
SEED_NOTES: list[tuple[str, str]] = [
    (
        "income_variance",
        "Bonus-heavy files in tech metros: analysts have accepted a 24-month "
        "average when the variance is seasonal and the employer confirms the "
        "bonus structure in the VOE. Single-year annualisation over-states.",
    ),
    (
        "flood_determination_mismatch",
        "Zone disagreements between a vendor pull and a prior determination have "
        "resolved on re-pull in most cases. Order the re-pull before escalating.",
    ),
    (
        "unsourced_deposit",
        "A 60-day sourcing request plus a letter of explanation has cleared these "
        "without underwriter escalation when the deposit traces to a documented "
        "asset sale.",
    ),
    (
        "title_exception",
        "Judgment liens require a payoff letter or a recorded release before "
        "closing. A supervisor has never waived one on this book.",
    ),
    (
        "low_confidence_ocr",
        "Re-OCR at 300dpi and cross-reference the VOE. Escalate only if the "
        "second pass is also below threshold.",
    ),
]


def seed_notes(session: Session) -> int:
    """Insert the starting operational memory. Skips notes already present."""
    added = 0
    for exception_type, text in SEED_NOTES:
        existing = session.exec(
            select(Note).where(Note.exception_type == exception_type, Note.text == text)
        ).first()
        if existing is None:
            session.add(Note(exception_type=exception_type, text=text,
                             source="human_resolution"))
            added += 1
    session.flush()
    return added


def seed_all(session: Session, data_dir: Path | None = None) -> dict[str, int]:
    """Seed everything and record it in the audit chain.

    The audit line is the first row of the chain on a fresh database, which
    means the trail starts at "the book was loaded" rather than mid-story.
    """
    loans_added, docs_added = seed_loans(session, data_dir)
    notes_added = seed_notes(session)

    if loans_added or docs_added or notes_added:
        append_audit(
            session,
            actor="seed",
            role="system",
            kind="system",
            action="seed_database",
            case_id="SYSTEM",
            detail={"loans": loans_added, "documents": docs_added, "notes": notes_added},
        )
    session.commit()
    return {"loans": loans_added, "documents": docs_added, "notes": notes_added}
