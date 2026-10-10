"""Database engine/session wiring."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mailer_agent.config import get_settings
from mailer_agent.models import Base

settings = get_settings()

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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


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
