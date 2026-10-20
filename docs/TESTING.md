# Testing

What each test layer proves, what it needs, the exact commands, and what has not been tested. Commands were checked against `pytest.ini`, `.github/workflows/ci.yml` and the test files at the commit this was written against. Evidence labels are defined in the [readiness report](FINAL_PRODUCTION_READINESS.md).

Configuration facts that apply everywhere: `pytest.ini` sets `pythonpath = .` and `addopts = -m "not integration"`, so a bare `pytest` runs only the deterministic layer. The `integration`, `postgres` and `imap_e2e` markers are opt-in with `-o addopts=""` plus explicit file paths. There is no `pyproject.toml`.

## 1. Layers

| Layer | Needs | Safe against | What it proves |
|---|---|---|---|
| Deterministic unit and contract | Python 3.12 and `requirements.txt` only (SQLite, fake LLM) | Any machine | Request contracts, state-machine logic, claim and recovery semantics on SQLite, tenant boundaries, error classification, prompt and grounding rules, the Render Blueprint's shape |
| PostgreSQL integration | A disposable PostgreSQL via `POSTGRES_TEST_URL` | A throwaway database only; tests create and truncate tables | `SKIP LOCKED` claims, row locking, unique constraints, fenced writes and idempotency under real concurrency |
| Migration wrapper | Disposable PostgreSQL | Creates and drops its own `mailer_migrate_test_*` database | The real `scripts/migrate_release.sh` and migration scripts, including their guards |
| Disposable SMTP | `aiosmtpd` (in `requirements.txt`), loopback socket | Any machine | `SENT`, `FAILED` and `UNKNOWN` classification and the MIME headers actually on the wire |
| Disposable IMAP and C12 chain | Dovecot (`dovecot-imapd`), root, PostgreSQL | A throwaway machine or CI only | Per-mailbox IMAP polling, and the chain from accepted dispatch through generation and send to an inbound reply, using a local SMTP sink |
| Live provider and mailbox checks | `GROQ_API_KEY`, real mailboxes | Only with credentials you intend to use | Real LLM behavior and real SMTP/IMAP providers; never part of the default run |
| Operator-reported local end-to-end | LeadBoost, Mailer and a test mailbox on the operator's machine | The operator's environment | The cross-system workflow worked once, locally; not repeatable from this repository |
| Hosted acceptance | The deployed API and worker | Controlled, with sending explicitly enabled | Not yet performed |

## 2. Commands

Set up as in the [README](../README.md#quick-start).

**Deterministic suite (this is what CI runs first):**

```bash
python -m pytest -q
python -m pytest -q tests/test_pr2_api_surface.py tests/test_render_blueprint.py   # targeted example
```

**PostgreSQL integration** (exactly the files CI lists; start a disposable PostgreSQL 16 first):

```bash
export POSTGRES_TEST_URL=postgresql+psycopg2://<user>:<password>@localhost:5432/<disposable_db>
python -m pytest -o addopts="" \
  tests/test_external_dispatch_postgres.py \
  tests/test_leadboost_generated_outreach_postgres.py \
  tests/test_postgresql_concurrency.py \
  tests/test_m3_inbound_postgres.py \
  tests/test_leadboost_reconciliation_postgres.py \
  tests/test_leadboost_conversation_postgres.py -v
```

**Migration wrapper against disposable PostgreSQL:**

```bash
REQUIRE_MIGRATION_WRAPPER_PG=1 python -m pytest -o addopts="" tests/test_migrate_release.py -v
```

`REQUIRE_MIGRATION_WRAPPER_PG=1` turns an unreachable database into a failure instead of a skip. The same file also covers `scripts/migrate_preflight.py verify` (the read-only dry run of `SCHEMA_INIT_MODE=verify`).

**Concurrent schema initialization** (real simultaneous processes against empty and wrapper-migrated disposable databases; the server must be able to `CREATE DATABASE`, and use a UTF-8 database for the other PostgreSQL suites):

```bash
REQUIRE_SCHEMA_INIT_PG=1 python -m pytest -o addopts="" tests/test_schema_init.py -v
```

`REQUIRE_SCHEMA_INIT_PG=1` turns an unreachable database into a failure instead of a skip. The always-run SQLite half of this file (modes, no-DDL guarantee, lifespan and worker wiring) is part of the ordinary deterministic suite.

**Local SMTP protocol integration:**

```bash
python -m pytest -o addopts="" tests/test_smtp_local_integration.py -v
```

**Disposable Dovecot IMAP and C12 full chain** (needs root and PostgreSQL; run on a throwaway machine):

```bash
sudo -E env "PATH=$PATH" \
  "DATABASE_URL=$POSTGRES_TEST_URL" "POSTGRES_TEST_URL=$POSTGRES_TEST_URL" \
  "C12_ALLOW_DESTRUCTIVE_TEST_DB=1" \
  python -m pytest -o addopts="" tests/test_m3_imap_e2e.py tests/test_c12_chain_smtp_imap.py -v
```

`C12_ALLOW_DESTRUCTIVE_TEST_DB=1` is an explicit acknowledgement that the test will wipe the database named by `POSTGRES_TEST_URL`. Never point it at a database you care about.

**Live provider smoke test** (requires `GROQ_API_KEY`; spends tokens; not a no-credential command):

```bash
python -m pytest -o addopts="" tests/evaluation/test_live_smoke.py -v
```

Files under `tests/evaluation/test_live_*.py` and `tests/test_semantic_regression.py` also depend on a live LLM. `tests/test_mailbox_postgres.py` is marked `postgres` but is not in any CI job's file list (see section 4).

## 3. Map of tests to guarantees

| Guarantee | Representative files |
|---|---|
| Dispatch state machine, claims, recovery | `test_external_dispatch_claiming.py`, `test_dispatch_recovery_semantics.py`, `test_external_dispatch_crash_matrix.py`, `test_external_dispatch_worker.py`, `test_external_dispatch_postgres.py` |
| Worker shutdown | `test_external_dispatch_scheduler_shutdown.py` |
| Idempotency | `test_leadboost_integration.py`, `test_leadboost_generated_outreach.py`, `test_leadboost_generated_outreach_postgres.py` |
| Tenant boundaries | `test_leadboost_native_isolation.py`, `test_c15_1_auth_fail_closed.py`, `test_c15_1b_webhook_tenant_scope.py`, `test_tenant_correlation.py`, `test_pr3_system_operator_boundary.py` |
| Mailbox API and encryption (deterministic, run by CI) | `test_mailbox_foundation.py`, `test_l1_mailbox_transport_update.py` |
| Mailbox persistence and encryption on PostgreSQL (**not run by any CI job**) | `test_mailbox_postgres.py` |
| SMTP outcomes | `test_smtp_local_integration.py`, `test_smtp_explicit_refusals_not_retried.py`, `test_sender_message_id.py`, `test_sender_quit_containment.py`, `test_sender_cleanup_precedence.py` |
| IMAP and inbound | `test_imap_reader.py`, `test_m3_mailbox_inbound.py`, `test_m3_imap_e2e.py`, `test_m3_inbound_postgres.py` |
| Migrations and release safeguards | `test_migrate_release.py`, `test_m3_message_dedupe_schema.py` |
| Deployment configuration | `test_render_blueprint.py`, `test_check_config.py`, `test_l1_render_org_keys.py` |
| Synthetic end-to-end | `test_c12_chain_smtp_imap.py`, `e2e/test_full_lifecycle.py` |

## 4. CI

`.github/workflows/ci.yml` runs on pushes to `main` and `grounding-state-fix` and on pull requests to `main`, with four jobs:

1. **Quality and deterministic tests**: `compileall`, report-only ruff, `pip check`, report-only `pip-audit` and `bandit`, then `python -m pytest -q --cov=mailer_agent` with coverage and JUnit artifacts.
2. **PostgreSQL integration** (PostgreSQL 16 service): the six files above, then the migration wrapper test with `REQUIRE_MIGRATION_WRAPPER_PG=1`.
3. **Local SMTP protocol integration**.
4. **M3 real IMAP end-to-end and C12 full chain**: installs Dovecot, then runs the two files with PostgreSQL and `C12_ALLOW_DESTRUCTIVE_TEST_DB=1`.

Lint, `pip-audit` and `bandit` are report-only (`--exit-zero` / `|| true`); they do not fail the build.

The most recent confirmed run: workflow `CI`, run `38626064792` on the Render-connected repository, commit `ce0cbfa`, conclusion `success` (`CI VERIFIED`, observed through the GitHub API while preparing this documentation). A matching run on the canonical repository was not retrievable at that time, and per-job results were not retrievable; the commit reached the Render-connected repository only through the CI-gated mirror workflow, which is consistent with the canonical run passing.

Gap: `tests/test_mailbox_postgres.py` exists and is marked `postgres` but is not listed in the PostgreSQL job, so CI does not run it. Either add it to the job or document why not; this documentation pass does not change workflows.

## 5. What is and is not demonstrated

| Claim | Status |
|---|---|
| Deterministic, PostgreSQL, wrapper, SMTP and Dovecot/C12 suites pass at `ce0cbfa` | `CI VERIFIED` (workflow conclusion; see section 4) |
| Complete LeadBoost-plus-Mailer workflow (discovery, scraping, scoring, lead generation, sending, follow-ups, SMTP and IMAP) worked | `LOCAL E2E VERIFIED`, operator-reported, scoped to the operator's local setup; the test mailbox or sink is not recorded here. Discovery, scraping and scoring are LeadBoost responsibilities |
| Mailer API `/health` returned 200 on Render; `/metrics` returned 404 | `HOSTED OBSERVED` |
| Railway worker started APScheduler and scheduled dispatch and generation jobs ran | `HOSTED OBSERVED` (worker logs) |
| Live Groq provider behavior | Not part of CI. `NOT VERIFIED` for the current hosted worker |
| Outbound SMTP from the Railway worker (project is on a Limited Trial; permitted networking not verified) | `NOT VERIFIED` |
| `tests/test_mailbox_postgres.py` | Not run by CI; no result is claimed |
| Hosted request, dispatch lifecycle and receipt on the deployed system | `NOT VERIFIED`: no hosted end-to-end delivery evidence exists |
| Throughput, latency, availability | Not measured anywhere in the repository |

## 6. Writing safe tests

- Never use a real mailbox, recipient or production database. Use a loopback SMTP sink and disposable Dovecot.
- Keep `LIVE_SENDING_ENABLED=false` in any environment that is not a disposable sink.
- Prove an invariant with a failing test before or alongside a fix.
- The legacy manual real-Gmail walkthrough in [TESTING_QUICKSTART.md](../TESTING_QUICKSTART.md) predates the mailbox model: it exercises the native deployment-wide SMTP identity, not the LeadBoost integration path.
