"""One-off: add `Loan.summary`, a column that predates this schema's rows.

`db.init_db()` is deliberately create-only — `SQLModel.metadata.create_all()`
never alters an existing table, only creates missing ones. That is the right
default for a POC with a live, seeded database and an append-only audit chain
worth protecting, but it means a new nullable column on an existing table
needs a hands-run step instead of a silent migration hook. This is that step.

    python scripts/add_summary_column.py

Idempotent: checks `PRAGMA table_info(loan)` first and does nothing if the
column is already there.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text            # noqa: E402

from app.config import get_settings    # noqa: E402
from app.db import get_engine          # noqa: E402


def main() -> int:
    settings = get_settings()
    print(f"  {settings.resolved_database_url()}")

    engine = get_engine()
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(loan)"))}
        if "summary" in columns:
            print("  loan.summary already present — nothing to do")
            return 0
        conn.execute(text("ALTER TABLE loan ADD COLUMN summary TEXT"))
        print("  added loan.summary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
