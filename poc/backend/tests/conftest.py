"""Test fixtures: a real SQLite database, in memory, per test.

Deliberately not a mock. The things step 3 must get right — foreign keys,
the unique constraint on the audit chain, savepoint rollback on an invariant
violation — are all database behaviour. A mocked session would pass every one
of these tests while the real thing corrupted its audit trail.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import db as db_module
from app import store
from app.models import Document, Loan


@pytest.fixture()
def engine():
    """One in-memory database shared across connections for the test's life.

    StaticPool keeps every connection pointed at the same memory database;
    without it each connection gets its own empty one and nothing persists
    between two statements.
    """
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    previous = db_module._engine
    db_module.set_engine(eng)
    try:
        yield eng
    finally:
        db_module.set_engine(previous)
        eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def loan(session) -> Loan:
    """One conforming Austin purchase whose arithmetic is self-consistent.

    PITI 3277.44 + debts 640 over income 10309.04 is 38.0%, and 412000/515000
    is 80.0% — so a rule that recomputes either quantity agrees with the header
    unless a test perturbs it on purpose.
    """
    ln = Loan(
        id="LN-TEST-0001",
        borrowers="Test Borrower",
        metro="Austin, TX",
        program="Conv",
        purpose="Purchase",
        amount=412_000,
        property_value=515_000,
        fico=742,
        ltv=80.0,
        dti=38.0,
        note_rate=6.67,
        monthly_income=10_309.04,
        piti=3_277.44,
        other_debts=640.0,
        conforming_limit=832_750,
        is_jumbo=False,
        scanned=True,
    )
    session.add(ln)
    # Flush the parent before the children: this schema declares no ORM
    # relationships, so SQLAlchemy flushes in the order objects were added
    # rather than in foreign-key order, and the documents would insert first.
    session.flush()
    for kind in ("urla", "id", "w2", "paystub", "bank_statement",
                 "credit_report", "hoi", "appraisal", "flood_cert"):
        session.add(Document(doc_id=f"LN-TEST-0001-{kind}", loan_id=ln.id,
                             kind=kind, path=f"documents/LN-TEST-0001/{kind}.txt",
                             chars=200))
    session.commit()
    return ln


@pytest.fixture()
def docs_on_disk(tmp_path, monkeypatch, session, loan):
    """Write real text for the fixture loan's documents and point the tools at it.

    `read_document` and the evidence-quote check both read from disk, so a
    fixture that only creates Document rows tests neither. Returns the root so
    a test can plant its own content.
    """
    from app import documents

    root = tmp_path / "data"
    for doc in store.documents_for(session, loan.id):
        path = root / doc.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"DOCUMENT TYPE: {doc.kind.upper()}\n"
            f"Loan: {doc.loan_id}\n"
            "Gross Monthly Income:   $10,309.04\n"
            "Back-End DTI:           38.0%\n",
            encoding="utf-8",
        )
    # One module owns the document root, so the tool layer and the integrity
    # scanner cannot end up reading different files.
    monkeypatch.setattr(documents, "DATA_DIR", root)
    return root


@pytest.fixture()
def ctx(session, loan):
    """An open Validation Agent run scoped to the fixture loan."""
    from app.gate import RunContext

    store.open_run(session, run_id="RUN-T1", agent="validation", loan_id=loan.id)
    session.commit()
    return RunContext(run_id="RUN-T1", agent="validation", loan_id=loan.id)
