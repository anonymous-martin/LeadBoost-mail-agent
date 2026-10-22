"""Tests for scripts/migrate_release.sh (+ scripts/migrate_preflight.py).

Two groups:

* Guard-refusal tests need no database at all. They run in the ordinary
  deterministic suite and prove that a missing/empty/SQLite DATABASE_URL, a
  missing backup acknowledgement, and a mis-declared "disposable" target are
  refused before anything is imported, connected to, or changed.

* Real-PostgreSQL tests run the ACTUAL wrapper and the ACTUAL migration scripts
  against a database this test creates and drops itself
  (``mailer_migrate_test_<random>`` on the loopback server named by
  POSTGRES_TEST_URL). They never touch any other database. When
  REQUIRE_MIGRATION_WRAPPER_PG=1 (set by the CI job) a missing or unreachable
  PostgreSQL is a hard FAILURE, never a skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "migrate_release.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required to run scripts/migrate_release.sh")

_SCRUBBED = (
    "DATABASE_URL",
    "MIGRATE_CONFIRM_BACKUP",
    "MIGRATE_DISPOSABLE_TEST_DB",
)


def _run(args: list[str] = (), **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    full["PYTHON"] = sys.executable
    full.update(env)
    return subprocess.run(
        [BASH, str(WRAPPER), *args], cwd=ROOT, env=full, check=False, capture_output=True, text=True, timeout=300
    )


def _out(p: subprocess.CompletedProcess) -> str:
    return p.stdout + p.stderr


# --------------------------------------------------------------------------
# Guard refusals -- no database involved
# --------------------------------------------------------------------------

@pytest.fixture()
def no_sqlite_side_effect():
    path = ROOT / "mailer_agent.db"
    existed = path.exists()
    yield
    if not existed:
        assert not path.exists(), "a refused run must never fall back to the local SQLite file"


@pytest.mark.parametrize("flag", [[], ["--check-only"], ["--status"]])
def test_missing_database_url_is_refused(flag, no_sqlite_side_effect):
    p = _run(flag)
    assert p.returncode == 2, _out(p)
    assert "DATABASE_URL is not set" in _out(p)


def test_empty_database_url_is_refused(no_sqlite_side_effect):
    p = _run(DATABASE_URL="   ")
    assert p.returncode == 2, _out(p)


@pytest.mark.parametrize("url", ["sqlite:///./mailer_agent.db", "sqlite://", "mysql://u:p@h/d", "not a url"])
def test_non_postgres_urls_are_refused(url, no_sqlite_side_effect):
    p = _run(DATABASE_URL=url)
    assert p.returncode == 2, _out(p)
    assert "not a PostgreSQL URL" in _out(p)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:SECRETPW@db.example.com:5432/mailer_migrate_test_x",  # not loopback
        "postgresql://u:SECRETPW@127.0.0.1:5432/mailer_agent",  # real-looking name
        "postgresql://u:SECRETPW@localhost:5432/not_mailer_migrate_test_x",  # prefix must be at start
    ],
)
def test_disposable_declaration_is_refused_unless_loopback_and_test_named(url):
    p = _run(DATABASE_URL=url, MIGRATE_DISPOSABLE_TEST_DB="yes")
    assert p.returncode == 2, _out(p)
    assert "does not qualify" in _out(p)
    assert "SECRETPW" not in _out(p)


def test_apply_without_backup_acknowledgement_is_refused_before_connecting():
    # 127.0.0.1:1 would be "unreachable" (exit 3) if the guard did not fire first.
    p = _run(DATABASE_URL="postgresql://u:SECRETPW@127.0.0.1:1/some_production_db")
    assert p.returncode == 2, _out(p)
    assert "backup acknowledgement" in _out(p)
    assert "I_HAVE_A_VERIFIED_BACKUP_OF_some_production_db" in _out(p)
    assert "SECRETPW" not in _out(p)


def test_wrong_backup_acknowledgement_is_refused():
    p = _run(
        DATABASE_URL="postgresql://u:SECRETPW@127.0.0.1:1/some_production_db",
        MIGRATE_CONFIRM_BACKUP="I_HAVE_A_VERIFIED_BACKUP_OF_another_db",
    )
    assert p.returncode == 2, _out(p)


def test_no_downgrade_option_exists():
    for arg in ("--downgrade", "--status --downgrade"):
        p = _run(arg.split())
        assert p.returncode == 64, _out(p)
        assert "no downgrade option" in _out(p) or "usage" in _out(p)


def test_unreachable_target_reports_type_only_and_no_credentials():
    p = _run(["--check-only"], DATABASE_URL="postgresql://u:SECRETPW@127.0.0.1:1/mailer_migrate_test_x")
    assert p.returncode == 3, _out(p)
    assert "127.0.0.1:1/mailer_migrate_test_x" in _out(p)
    assert "SECRETPW" not in _out(p)


# --------------------------------------------------------------------------
# Real PostgreSQL: the actual wrapper + actual scripts on a database the test owns
# --------------------------------------------------------------------------

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
_REQUIRED = os.environ.get("REQUIRE_MIGRATION_WRAPPER_PG") == "1"


def _admin_engine():
    if not POSTGRES_TEST_URL:
        msg = "POSTGRES_TEST_URL is not set"
    else:
        eng = create_engine(POSTGRES_TEST_URL, isolation_level="AUTOCOMMIT")
        try:
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            return eng
        except Exception as exc:  # noqa: BLE001
            msg = f"PostgreSQL named by POSTGRES_TEST_URL is unreachable ({type(exc).__name__})"
    if _REQUIRED:
        pytest.fail(f"REQUIRE_MIGRATION_WRAPPER_PG=1 but {msg}")
    pytest.skip(msg)


class _Disposable:
    def __init__(self, admin):
        self.name = f"mailer_migrate_test_{uuid.uuid4().hex[:12]}"
        self._admin = admin
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.name}"'))
        base = make_url(POSTGRES_TEST_URL)
        self.sa_url = base.set(database=self.name)
        # Bare postgresql:// on purpose: that is the form Render hands out, and the
        # application must pin the driver itself (db.normalize_database_url).
        self.dsn = self.sa_url.set(drivername="postgresql").render_as_string(hide_password=False)
        self.engine = create_engine(self.sa_url)

    def run(self, args=(), **env):
        return _run(list(args), DATABASE_URL=self.dsn, MIGRATE_DISPOSABLE_TEST_DB="yes", **env)

    def tables(self) -> set[str]:
        with self.engine.connect() as c:
            return set(inspect(c).get_table_names())

    def drop(self):
        self.engine.dispose()
        with self._admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))


@pytest.fixture()
def disposable_db():
    admin = _admin_engine()
    db = _Disposable(admin)
    try:
        yield db
    finally:
        db.drop()
        admin.dispose()


@pytest.mark.integration
@pytest.mark.postgres
def test_full_canonical_sequence_on_empty_database_then_idempotent_rerun(disposable_db):
    assert disposable_db.tables() == set()

    first = disposable_db.run()
    assert first.returncode == 0, _out(first)
    assert "(11/12) migrations/009_messages_mailbox_scoped_dedupe.py" in _out(first)
    assert "(12/12) migrations/010_worker_runtime_status.py" in _out(first)
    assert "ABSENT" not in _out(first), "every schema marker must be present after the run"

    tables = disposable_db.tables()
    assert {"campaigns", "contacts", "messages", "external_dispatches", "mailboxes"} <= tables
    # PR4: operator observability tables and the 010 markers
    assert {"worker_runtime_status", "worker_cycle_status"} <= tables
    assert "010  worker_runtime_status table" in _out(first)
    assert "ix_messages_inbound_created_at" in _out(first)
    with disposable_db.engine.connect() as c:
        names = {i["name"] for i in inspect(c).get_indexes("messages")}
        names |= {u["name"] for u in inspect(c).get_unique_constraints("messages")}
    # Present only because the migrations ran -- create_all alone does not create it.
    assert "uq_messages_inbound_message_id" in names

    second = disposable_db.run()
    assert second.returncode == 0, _out(second)
    assert "ABSENT" not in _out(second)


@pytest.mark.integration
@pytest.mark.postgres
def test_refusal_without_acknowledgement_changes_nothing(disposable_db):
    # Same disposable database, but WITHOUT the disposable declaration and WITHOUT the ack.
    p = _run(DATABASE_URL=disposable_db.dsn)
    assert p.returncode == 2, _out(p)
    assert disposable_db.tables() == set()


@pytest.mark.integration
@pytest.mark.postgres
def test_explicit_acknowledgement_is_accepted_for_the_named_database(disposable_db):
    p = _run(
        DATABASE_URL=disposable_db.dsn,
        MIGRATE_CONFIRM_BACKUP=f"I_HAVE_A_VERIFIED_BACKUP_OF_{disposable_db.name}",
    )
    assert p.returncode == 0, _out(p)
    assert "mailboxes" in disposable_db.tables()


@pytest.mark.integration
@pytest.mark.postgres
def test_check_only_and_status_are_read_only(disposable_db):
    for flag in (["--check-only"], ["--status"]):
        p = disposable_db.run(flag)
        assert p.returncode == 0, _out(p)
        assert "ABSENT" in _out(p)
        assert disposable_db.tables() == set(), f"{flag} must not create anything"


def _legacy_shaped_db_with_dispatch(db: _Disposable, state: str):
    """A pre-M2-A shape: dispatch/message have no mailbox_id and there is no mailboxes table."""
    from sqlalchemy.orm import sessionmaker

    from mailer_agent.models import Base
    from tests.dispatch_support import seed_dispatch

    Base.metadata.create_all(bind=db.engine)
    with sessionmaker(bind=db.engine)() as s:
        d = seed_dispatch(s, state=state, mailbox=False)
        dispatch_id = d.id
    with db.engine.begin() as c:
        c.execute(text("ALTER TABLE external_dispatches DROP COLUMN mailbox_id"))
        c.execute(text("ALTER TABLE messages DROP COLUMN mailbox_id"))
        c.execute(text("DROP TABLE mailboxes CASCADE"))
    return dispatch_id


def _dispatch_state(db: _Disposable, dispatch_id: int):
    with db.engine.connect() as c:
        return c.execute(text("SELECT state FROM external_dispatches WHERE id = :i"), {"i": dispatch_id}).scalar()


@pytest.mark.integration
@pytest.mark.postgres
def test_migration_007_precheck_blocks_queued_dispatch_and_changes_nothing(disposable_db):
    dispatch_id = _legacy_shaped_db_with_dispatch(disposable_db, "queued")
    p = disposable_db.run()
    assert p.returncode == 4, _out(p)
    assert "mailbox_id does not exist yet" in _out(p)
    assert "mailboxes" not in disposable_db.tables(), "no migration step may have run"
    assert _dispatch_state(disposable_db, dispatch_id) == "queued", "the dispatch must be untouched"


@pytest.mark.integration
@pytest.mark.postgres
def test_migration_007_precheck_does_not_block_when_only_terminal_dispatches_exist(disposable_db):
    dispatch_id = _legacy_shaped_db_with_dispatch(disposable_db, "sent")
    p = disposable_db.run()
    assert p.returncode == 0, _out(p)
    assert "mailboxes" in disposable_db.tables()
    assert _dispatch_state(disposable_db, dispatch_id) == "sent", "existing delivery state must be preserved"


# --------------------------------------------------------------------------
# `migrate_preflight.py verify`: read-only dry run of the SCHEMA_INIT_MODE=verify start-up check
# --------------------------------------------------------------------------
PREFLIGHT = ROOT / "scripts" / "migrate_preflight.py"


def _preflight(*args: str, **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    full.update(env)
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), *args], cwd=ROOT, env=full, check=False, capture_output=True, text=True, timeout=300
    )


def test_verify_subcommand_is_guarded_like_every_other_subcommand(no_sqlite_side_effect):
    p = _preflight("verify")  # no DATABASE_URL at all
    assert p.returncode == 2 and "REFUSED" in _out(p)
    p = _preflight("verify", DATABASE_URL="sqlite:///./x.db")
    assert p.returncode == 2 and "REFUSED" in _out(p)


@pytest.mark.integration
@pytest.mark.postgres
def test_verify_reports_an_empty_database_as_not_ready_and_creates_nothing(disposable_db):
    p = _preflight("verify", DATABASE_URL=disposable_db.dsn, MIGRATE_DISPOSABLE_TEST_DB="yes")
    assert p.returncode == 5, _out(p)
    assert "[MISSING] missing table external_dispatches" in _out(p)
    assert "would refuse to start" in _out(p)
    assert disposable_db.tables() == set(), "verify must never create anything"
    assert "password" not in _out(p).lower() and disposable_db.dsn not in _out(p)


@pytest.mark.integration
@pytest.mark.postgres
def test_verify_passes_after_the_real_canonical_migration_sequence(disposable_db):
    assert disposable_db.run().returncode == 0
    before = disposable_db.tables()
    p = _preflight("verify", DATABASE_URL=disposable_db.dsn, MIGRATE_DISPOSABLE_TEST_DB="yes")
    assert p.returncode == 0, _out(p)
    assert "would start" in _out(p) and "[MISSING]" not in _out(p)
    assert disposable_db.tables() == before


@pytest.mark.integration
@pytest.mark.postgres
def test_verify_names_the_columns_a_legacy_shaped_database_lacks(disposable_db):
    _legacy_shaped_db_with_dispatch(disposable_db, "sent")
    p = _preflight("verify", DATABASE_URL=disposable_db.dsn, MIGRATE_DISPOSABLE_TEST_DB="yes")
    assert p.returncode == 5, _out(p)
    assert "missing column external_dispatches.mailbox_id" in _out(p)
    assert "missing column messages.mailbox_id" in _out(p)
    assert "missing table mailboxes" in _out(p)
