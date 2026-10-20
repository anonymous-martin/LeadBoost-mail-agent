"""Database engine/session wiring."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from mailer_agent.config import get_settings
from mailer_agent.models import Base

settings = get_settings()
logger = logging.getLogger(__name__)

# Render (and Heroku before it) hands out connection strings prefixed
# `postgres://`, which SQLAlchemy 2.0 no longer accepts -- it requires
# `postgresql://`. Rewriting it here means the Render-provided
# DATABASE_URL env var can be used as-is, no manual edits needed.


def normalize_database_url(url: str) -> str:
    """Return ``url`` with the driver made explicit for PostgreSQL.

    * ``postgres://`` (Render/Heroku style) is rewritten to ``postgresql://``,
      which SQLAlchemy 2.x requires.
    * A bare ``postgresql://`` is rewritten to ``postgresql+psycopg2://``.
      ``requirements.txt`` installs only ``psycopg2-binary`` and allows any
      SQLAlchemy 2.x, but SQLAlchemy 2.1 resolves a bare ``postgresql://`` to
      the psycopg (v3) dialect, which is not installed -- a fresh build would
      then fail at import time on the platform-provided DATABASE_URL.
      Pinning the driver here keeps the tested driver independent of which
      SQLAlchemy 2.x a build resolves.
    URLs that already name a driver (``postgresql+...``) or another database
    (``sqlite://``) are returned unchanged.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


_database_url = normalize_database_url(settings.database_url)

_connect_args = {"check_same_thread": False} if _database_url.startswith("sqlite") else {}
engine = create_engine(_database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class SchemaNotReadyError(RuntimeError):
    """SCHEMA_INIT_MODE=verify and the database does not have what the models need."""


class SchemaInitLockTimeout(RuntimeError):
    """Another process held the schema-initialization lock for longer than the timeout."""


# One fixed, application-specific advisory-lock key (a signed 64-bit integer), derived from a name so
# it cannot be mistaken for another application's key and never changes between releases.
SCHEMA_INIT_LOCK_KEY = int.from_bytes(hashlib.sha256(b"mailer_agent:schema_init").digest()[:8], "big", signed=True)
SCHEMA_INIT_LOCK_TIMEOUT_MS = 45_000  # below gunicorn's --timeout 60; a stuck holder fails loudly, not forever


def init_db() -> None:
    """Create any MISSING tables (never alters or drops). Safe to call from several processes at once.

    ``Base.metadata.create_all`` is check-then-create, so on PostgreSQL two processes starting together
    against an empty database both see "no table" and both issue CREATE TABLE; one then fails with
    ``UniqueViolation ... pg_type_typname_nsp_index`` (the duplicate ``<table>_id_seq``/row-type error
    seen once on hosted infrastructure) or ``DuplicateTable``. The whole call therefore runs inside one
    transaction that first takes a transaction-scoped advisory lock: concurrent callers queue, and each
    later caller's existence check sees the tables the first one committed, so it does nothing. The lock
    is released automatically on commit, rollback or connection loss, so a crashed process cannot leave
    it held. SQLite (local development, most tests) is single-writer and needs no lock.
    """
    if engine.dialect.name != "postgresql":
        Base.metadata.create_all(bind=engine)
        return
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL lock_timeout = {int(SCHEMA_INIT_LOCK_TIMEOUT_MS)}")
            conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({int(SCHEMA_INIT_LOCK_KEY)})")
            Base.metadata.create_all(bind=conn)
    except OperationalError as exc:
        if "lock timeout" in str(exc).lower():
            raise SchemaInitLockTimeout(
                f"Another process held the schema-initialization lock for more than "
                f"{SCHEMA_INIT_LOCK_TIMEOUT_MS // 1000}s. Check for a stuck migration or session."
            ) from exc
        raise


def schema_problems(bind=None) -> list[str]:
    """Read-only: what the models need that the database lacks. Names only; no DDL, no data read."""
    insp = inspect(bind if bind is not None else engine)
    existing = set(insp.get_table_names())
    problems: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            problems.append(f"missing table {table.name}")
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        problems.extend(f"missing column {table.name}.{c.name}" for c in table.columns if c.name not in have)
    return problems


def verify_schema() -> None:
    """Refuse to continue unless every table and column the models need exists. Issues NO DDL.

    This checks structure only. It does not prove the release migrations were applied (indexes,
    constraints and nullability are not examined); ``scripts/migrate_release.sh --status`` reports
    those markers and remains the check for that.
    """
    problems = schema_problems()
    if problems:
        shown = "; ".join(problems[:10]) + (f"; ... and {len(problems) - 10} more" if len(problems) > 10 else "")
        raise SchemaNotReadyError(
            f"Database schema is not ready (SCHEMA_INIT_MODE=verify, {len(problems)} problem(s): {shown}). "
            "No DDL was attempted. Run scripts/migrate_release.sh (see docs/C13_DEPLOYMENT_RUNBOOK.md) "
            "before starting this service."
        )


def prepare_schema() -> None:
    """Process-start schema step for the API and the worker, selected by SCHEMA_INIT_MODE."""
    if settings.schema_init_mode == "verify":
        verify_schema()
        logger.info("Schema verified (SCHEMA_INIT_MODE=verify): no DDL executed by this process")
    else:
        init_db()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    """For use outside request handlers (scheduler jobs, scripts)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
