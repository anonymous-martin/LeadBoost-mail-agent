"""Unit tests for the PostgreSQL write-guard (tests/pg_target_guard.py) and the CI opt-in. No database."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pg_target_guard import assess_pg_target

CI_URL = "postgresql+psycopg2://mailer_agent:mailer_agent_test_pw@localhost:5432/mailer_agent_test"
DB = "mailer_agent_test"


def _a(url=CI_URL, opt_in="1", expected=DB):
    return assess_pg_target(url, opt_in=opt_in, expected_db=expected)


@pytest.mark.parametrize("url", [
    CI_URL,
    "postgresql+psycopg2://u@127.0.0.1:55433/mailer_agent_test",
    "postgresql://u@[::1]/mailer_agent_test",
    "postgresql+psycopg2:///mailer_agent_test",            # no host: local socket
])
def test_the_intended_local_or_ci_target_is_allowed(url):
    assert _a(url).verdict == "run"


def test_nothing_configured_or_no_explicit_opt_in_is_unavailable_not_run():
    assert _a(url=None).verdict == "unavailable"
    assert _a(url="").verdict == "unavailable"
    for opt_in in (None, "", "0", "true", "yes"):
        assert _a(opt_in=opt_in).verdict == "unavailable"


@pytest.mark.parametrize("url, expected", [
    ("postgresql+psycopg2://u:p@db.internal.example:5432/mailer_agent_test", DB),     # remote host
    ("postgresql+psycopg2://u:p@10.0.0.5/mailer_agent_test", DB),                      # private IP is not loopback
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_prod", "mailer_agent_prod"),    # expected lacks 'test'
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test", "other_test"),           # name != expected
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test2", DB),                    # name != expected
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test", None),                   # expected unset
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test", ""),
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test?host=prod.example.com", DB),   # host override
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test?hostaddr=203.0.113.9", DB),
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test?service=prod", DB),
    ("postgresql+psycopg2://u:p@localhost/mailer_agent_test?options=-csearch_path%3Dx", DB),
    ("sqlite:///mailer_agent_test", DB),                                               # not PostgreSQL
    ("not a url", DB),
])
def test_an_unexpected_remote_or_ambiguous_target_is_refused(url, expected):
    d = _a(url=url, expected=expected)
    assert d.verdict == "refuse", d


def test_a_refusal_reason_never_contains_the_url_or_a_credential():
    for url in ("postgresql+psycopg2://admin:SuperSecretPw@db.example.com:5432/prod",
                "postgresql+psycopg2://admin:SuperSecretPw@localhost/mailer_agent_prod"):
        d = assess_pg_target(url, opt_in="1", expected_db=DB)
        assert d.verdict == "refuse"
        assert "SuperSecretPw" not in d.reason and "admin" not in d.reason and "db.example.com" not in d.reason


def test_the_ci_postgresql_step_explicitly_opts_in_and_makes_a_missing_database_a_failure():
    ci = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.split("- name: Run PostgreSQL integration suite", 1)[1].split("\n      - name:", 1)[0]
    assert 'MAILER_PG_TEST_OPT_IN: "1"' in step
    assert "MAILER_PG_TEST_EXPECTED_DB: mailer_agent_test" in step
    assert 'REQUIRE_UNMATCHED_INBOUND_PG: "1"' in step
    assert "tests/test_unmatched_inbound_postgres.py" in step
    assert "@localhost:5432/mailer_agent_test" in step     # the job's own disposable service container
