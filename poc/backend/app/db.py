"""Engine, session and schema creation.

One SQLite detail that is easy to get wrong and expensive to discover late:
**SQLite does not enforce foreign keys unless you ask it to, per connection.**
Without the pragma below, `evidence_doc_id` could point at a document that does
not exist and nothing would complain until a human opened the exception and
found no evidence behind it. The pragma is installed on the connect event so
every pooled connection gets it, not just the first.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  — import registers every table on SQLModel.metadata
from .config import get_settings

_engine: Engine | None = None


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Enforce FKs and use WAL. Applied to every new connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL lets the UI read while an agent run writes. Without it, a long
        # scan blocks every dashboard poll for its duration.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Wait rather than fail immediately when another writer holds the lock.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine() -> Engine:
    """Process-wide engine, built on first use from settings."""
    global _engine
    if _engine is None:
        url = get_settings().resolved_database_url()
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path not in (":memory:", ""):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            url,
            echo=False,
            # FastAPI hands a session to whichever worker thread serves the
            # request, so the connection legitimately crosses threads.
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
    return _engine


def set_engine(engine: Engine) -> None:
    """Override the engine — used by tests to point at an in-memory database."""
    global _engine
    _engine = engine


def init_db(engine: Engine | None = None) -> Engine:
    """Create any missing tables. Safe to call repeatedly.

    Deliberately create-only. There is no migration story in a POC, and a
    `drop_all` hiding in a startup path is how a demo loses its audit trail
    thirty seconds before it is shown to someone.
    """
    engine = engine or get_engine()
    SQLModel.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception."""
    with Session(engine or get_engine()) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Commit is the caller's responsibility per request."""
    with Session(get_engine()) as session:
        yield session
