"""mailer_agent.db.normalize_database_url: make the PostgreSQL driver explicit.

requirements.txt installs only psycopg2-binary and allows any SQLAlchemy 2.x.
SQLAlchemy 2.1 resolves a bare ``postgresql://`` to psycopg (v3), which is not
installed, so a fresh build that resolved 2.1 would fail at import time on the
DATABASE_URL Render provides. No database is needed to prove the rewrite.
"""

import pytest
from sqlalchemy.engine import make_url

from mailer_agent.db import normalize_database_url


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
        ("postgresql://u:p@h/d?sslmode=require", "postgresql+psycopg2://u:p@h/d?sslmode=require"),
    ],
)
def test_bare_postgres_urls_get_the_installed_driver(given, expected):
    assert normalize_database_url(given) == expected
    assert make_url(normalize_database_url(given)).get_dialect().driver == "psycopg2"


@pytest.mark.parametrize(
    "given",
    [
        "postgresql+psycopg2://u:p@h/d",
        "postgresql+psycopg://u:p@h/d",  # an explicit choice is never overridden
        "sqlite:///./mailer_agent.db",
        "sqlite://",
    ],
)
def test_explicit_drivers_and_other_databases_are_unchanged(given):
    assert normalize_database_url(given) == given


def test_normalization_is_idempotent():
    once = normalize_database_url("postgres://u:p@h/d")
    assert normalize_database_url(once) == once
