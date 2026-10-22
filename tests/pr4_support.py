"""Shared helpers for the PR4 operator-observability tests.

Every request goes through the REAL ``app`` and the REAL operator authentication dependency; only
``get_db`` is replaced (an isolated SQLite file) and the clock the routes read is pinned. Nothing
contacts SMTP/IMAP, an LLM provider or a real database.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps, system
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import Base, get_db

OP_KEY = "operator-" + "p" * 28
OP_HEADER = "X-System-Operator-Key"
KEY_A = "key-a-" + "a" * 20

# A fixed instant; every age in the tests is computed against it.
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def ago(seconds: float, *, naive: bool = False) -> datetime:
    value = NOW - timedelta(seconds=seconds)
    return value.replace(tzinfo=None) if naive else value


def install_operator_app(monkeypatch, sm):
    """Point the REAL app at ``sm`` (get_db only), configure a real operator key, pin the route clock.
    Returns (client, headers, get). The caller removes the get_db override."""
    def _get_db():
        s = sm()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(deps, "_KEY_MAP", {KEY_A: "org-a"})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)
    monkeypatch.setattr(get_settings(), "system_operator_api_key", SecretStr(OP_KEY))
    monkeypatch.setattr(system, "utcnow", lambda: NOW)
    client = TestClient(app)
    headers = {OP_HEADER: OP_KEY}
    return client, headers, (lambda path, **kw: client.get(path, headers=headers, **kw))


@pytest.fixture()
def operator_env(tmp_path, monkeypatch):
    """Isolated SQLite DB + real app + a configured operator key + a pinned clock."""
    eng = create_engine(f"sqlite:///{tmp_path / 'pr4.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    sm = sessionmaker(bind=eng)
    client, headers, get = install_operator_app(monkeypatch, sm)
    try:
        yield SimpleNamespace(sm=sm, engine=eng, client=client, headers=headers, get=get)
    finally:
        app.dependency_overrides.pop(get_db, None)
        eng.dispose()


@contextlib.contextmanager
def capture_sql(engine):
    """All SQL statements the engine executes inside the block (statement text only)."""
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _before)
