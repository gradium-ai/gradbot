"""SQLAlchemy engine, session, and Base."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_db_url = _settings.database_url
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args: dict = {}
if _db_url.startswith("sqlite"):
    # `timeout` makes pysqlite wait up to N seconds when another connection
    # holds the lock instead of erroring immediately.
    connect_args = {"check_same_thread": False, "timeout": 30.0}

engine = create_engine(
    _db_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=connect_args,
    future=True,
)


# SQLite needs PRAGMA tweaks for any non-trivial concurrency:
#   WAL journal mode lets readers proceed while a writer is active.
#   busy_timeout makes the engine retry on lock for up to 30s.
# Postgres ignores these.
if _db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a SQLAlchemy session and closes it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for use outside of FastAPI request handlers."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Importing models registers them with Base."""
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_compat_schema()


def _ensure_compat_schema() -> None:
    """Apply small additive schema fixes for local SQLite demo databases."""
    if not _db_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "search_profiles" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("search_profiles")}
    if "city" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE search_profiles ADD COLUMN city VARCHAR(32) NOT NULL DEFAULT 'paris'")
            )
