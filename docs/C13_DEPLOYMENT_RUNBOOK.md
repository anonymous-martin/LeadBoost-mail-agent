# C13 -- Mailer deployment wiring and operations runbook

Scope: how the **already deployed** Mailer API, Mailer worker and Mailer-owned
PostgreSQL are configured, migrated, paused, rolled back and checked. It
changes no delivery logic. Nothing here was executed against a live deployment;
see "Evidence" at the end for what is proven and how.

Frozen invariants this runbook must never conflict with: `ExternalDispatch` is the
durable queue; claims use `FOR UPDATE SKIP LOCKED` + lease/fencing; Txn B -> SMTP
(outside any DB transaction) -> Txn C; Message-ID persisted before SMTP; an
expired `SENDING` becomes `UNKNOWN`, never `QUEUED`; ambiguous outcomes stay
`UNKNOWN` and are never automatically resent.

## 1. Process model

| Process | Start command | Scheduler | Instances |
|---|---|---|---|
| API (`render.yaml`: `mailer-agent-api`, web) | Gunicorn + Uvicorn workers (`-w 2`) | **None.** Requires `RUN_SCHEDULER_IN_PROCESS=false` | scale freely |
| Worker (`mailer-agent-worker`, background worker) | `python worker.py` | **Always starts one** (reply poll, follow-up, dispatch and recovery jobs, `max_instances=1` per job) | **Exactly one** |
| Database (`mailer-agent-db`) | managed PostgreSQL | -- | one |

* `RUN_SCHEDULER_IN_PROCESS` defaults to **true** in code. If it is missing on the
  API service, each Gunicorn worker starts its own scheduler beside the worker
  service. `scripts/check_config.py --role api` fails on this.
* A second worker instance does not corrupt dispatch (claims use `SKIP LOCKED`),
  but it doubles the polling jobs and is not the intended topology. Keep one.
* The API and worker must run the **same revision** against the **same database**.
  Deploy them together; do not leave one on an older revision.
* Never substitute the C12 one-shot cycle driver for `worker.py`.

## 2. Environment contract

Run `python scripts/check_config.py --role api|worker` in each service's shell.
It prints presence/shape only, never values.

| Variable | API | Worker | Secret | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | yes | yes | yes | Same database for both. **Defaults to local SQLite if unset** (no startup guard). A bare `postgresql://` is pinned to `psycopg2` by `db.normalize_database_url` |
| `MAILBOX_ENCRYPTION_KEY` | yes | yes | yes | Fernet key; **identical on both, stable forever** (section 4) |
| `ORG_KEY_MAP` | yes | no | yes | `{"<api_key>":"<mailer_org_id>"}`; API only |
| `API_KEY` | optional | no | yes | Legacy. If set it is **always** an accepted key mapped to org `default` on every route. Leave unset when `ORG_KEY_MAP` is used |
| `ORG_KEYS_<org>` | optional | no | yes | Third key source, merged below `ORG_KEY_MAP` |
| `LEADBOOST_INTEGRATION_SENDER_EMAIL` | yes | no | no | Deployment-wide sender persona (C15 limitation). Empty -> first generated-outreach dispatch per org returns 503 |
| `GROQ_API_KEY` | yes | yes | yes | Generation |
| `LIVE_SENDING_ENABLED` | read (`/health`) | **decides dispatch** | no | Separate copy per service -- see section 7 |
| `AUTO_REPLY_ENABLED` | yes | yes | no | Separate copy per service. Keep `false` unless authorised |
| `RUN_SCHEDULER_IN_PROCESS` | **must be `false`** | n/a | no | See section 1 |
| `EXTERNAL_DISPATCH_SHUTDOWN_DRAIN_SECONDS` | no | yes | no | Default 25 |
| `EXTERNAL_DISPATCH_POLL_SECONDS`, `_RECOVERY_POLL_SECONDS`, `_MAX_PER_CYCLE`, `_LEASE_SECONDS`, `_GENERATION_LEASE_SECONDS` | no | yes | no | Tunables; defaults 15 / 60 / 5 / 900 / 300. Do not change as part of a deploy |

SMTP/IMAP for the LeadBoost flow are **per-mailbox** (stored encrypted via
`POST /mailboxes`), not process environment variables. LeadBoost does not yet
provision IMAP settings; the staging operator PATCH is a C14 procedure. Exactly
one ACTIVE mailbox per organization for the integrated flow.

## 3. API-key contract with LeadBoost

The two sides use **inverse** shapes. Neither value should ever be pasted into a
ticket, PR, log or chat.

| Side | Variable | Shape |
|---|---|---|
| LeadBoost | `MAILING_AGENT_ORG_API_KEYS` | `{"<leadboost_org_id>": "<api_key>"}` |
| Mailer | `ORG_KEY_MAP` | `{"<api_key>": "<mailer_org_id>"}` |

Both sides must agree per organization: the key LeadBoost sends for org X must
map, on the Mailer, to the org id the Mailer holds mailboxes for. The Mailer
derives the organization **from the key**, never from a request body.

If **no** keys are effective (empty/invalid `ORG_KEY_MAP`, no `API_KEY`, no
`ORG_KEYS_*`), the human-operated routes (campaigns, contacts, messages,
webhooks, system) are **unauthenticated**; only the integration and mailbox
routes fail closed (503). An invalid-JSON `ORG_KEY_MAP` is logged and ignored;
valid JSON that is not an object crashes the API at import. `check_config.py`
reports all three. Fixing this in code is C15.

Safe verification after a key change (read-only, no send): from a trusted shell,
`GET https://<mailer-host>/mailboxes` with `X-API-Key: <key>` should return 200
for a valid key and 401/403 for a wrong key, and `GET /system/health` (authenticated,
organisation-scoped, queries the database) should return 200. Both prove TLS +
key mapping + database reachability **for the API only**; they say nothing about
the worker. Rotation/revocation procedure is C15/C16.

## 4. `MAILBOX_ENCRYPTION_KEY`

* Encrypts the SMTP/IMAP passwords stored by `POST /mailboxes`. Without the exact
  key the stored credentials are permanently unreadable and mailboxes must be
  re-provisioned.
* Same value on API **and** worker; set it once and treat it as immutable.
* Keep an offline copy in your secrets manager, separate from the database
  backup. A database backup is useless for mailbox credentials without it, and
  the key alone is useless without the database.
* A rollback of code must not change it. Rotation (re-encrypting stored
  credentials) is not implemented; do not "rotate" by replacing the value.

## 5. Database and migrations

Mailer does not use Alembic. The canonical, ordered, idempotent scripts and the
guarded operator procedure are in `migrations/README.md`
("Release procedure"). Summary:

1. Back up; verify the backup restores.
2. Suspend the worker (section 7).
3. `DATABASE_URL=... scripts/migrate_release.sh --status`, then `--check-only`.
4. `MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<db> scripts/migrate_release.sh`.
5. Deploy API + worker together; resume the worker.

Rules: migrations never run from API/worker startup; never downgrade a database
that holds customer data (006 downgrade destroys mailbox credentials; 007-009
drop columns/indexes); never run scripts out of order or individually on a
production database. **Existing database:** the live schema state is unknown to
this repository -- run `--status` first and treat any `ABSENT` marker as "needs
migrating". A database that only ever ran the application's own `create_all`
lacks the inbound-dedupe unique index and the work-queue indexes (measured in
C13), so "the app starts" does not mean "migrated".

Migration 007: drain non-terminal dispatches first (the tool blocks otherwise).

## 6. Health semantics (do not over-read them)

| Endpoint | Auth | What it proves | What it does **not** prove |
|---|---|---|---|
| Mailer `GET /health` | none | The API process is up and serving. Static `ok`; also echoes the two sending flags | Database, worker, SMTP, IMAP, readiness |
| Mailer `GET /system/health` | key | Auth + key mapping + database queries work, for that org | Worker liveness, SMTP/IMAP |
| Worker | -- | **No liveness signal exists.** Detect failure from service status/restarts and logs | -- |

Render's health check on `/health` therefore only gates routing to a live
process. `/health` unauthenticated exposure and a real worker liveness signal
are C15. **Do not claim the worker is monitored** until one exists.

## 7. Pause / resume sending

**Do not pause by setting `LIVE_SENDING_ENABLED=false` on the worker.** The
worker does not hold dispatches in that mode: a claimed dispatch is failed
pre-SMTP with `live_sending_disabled` (`mailer_agent/mail/external_dispatch_worker.py`),
i.e. queued work is destroyed and callers must resubmit with new idempotency
keys. The flag is also a separate copy on each service; changing one does not
change the other.

Safe pause:

1. Suspend the **worker** service in the provider (an operator action).
   SIGTERM -> `graceful_shutdown`: stop new claims, drain in-flight sends up to
   `EXTERNAL_DISPATCH_SHUTDOWN_DRAIN_SECONDS`, move dispatches this process still
   owns `SENDING -> UNKNOWN`, release contact claims. Work not yet sent stays
   `QUEUED`.
2. The API keeps accepting dispatches; they queue durably. Tell LeadBoost
   operators delivery is delayed, not failed.
3. Resume the worker. It claims `QUEUED` work; `UNKNOWN` items are **not**
   retried.

`maxShutdownDelaySeconds: 60` is set on the worker in `render.yaml`. If the live
worker was configured in the dashboard, apply the equivalent shutdown-delay
setting there; the repository file does not change a live service by itself.

## 8. `UNKNOWN` outcomes

`UNKNOWN` means the system cannot prove whether the message left. Never reset it
to `QUEUED`, never re-send automatically, never edit rows to "fix" it. An operator
checks the mailbox's Sent folder / SMTP provider log for the persisted Message-ID
and decides; the product remedy policy is a C15 item. LeadBoost `/mailer-state`
is read-only and does not heal.

## 9. Release and rollback

**Identify the deployed version:** the provider's Deploys view for each service
shows its commit; API and worker must match. Compare with `git log`.

**Tell failures apart:** API/worker fail at start -> look for config errors in the
service log (`check_config.py` first). Downstream failures (SMTP/IMAP) surface as
per-dispatch `FAILED`/`UNKNOWN` states, not as service restarts.

**Code rollback** (operator, explicit approval): roll back API and worker to the
same earlier commit. It does **not** roll back the schema. Migrations here are
additive/idempotent, but whether older code tolerates the newer schema (notably
009's replacement of the global `message_id_header` unique constraint by partial
indexes) is **not verified**; roll code back only to a revision that matches the
current schema, otherwise restore the database from the pre-migration backup
(which loses data written since). A rebuild re-resolves dependencies: the driver
pin in `db.py` exists because SQLAlchemy 2.1 resolves a bare `postgresql://` to a
driver this project does not install.

**Keep stable across restarts/rollbacks:** `MAILBOX_ENCRYPTION_KEY`, the
`ORG_KEY_MAP` contents, `DATABASE_URL`, and the single worker instance.

**Requires an operator and explicit approval:** any migration, any database
restore, enabling `LIVE_SENDING_ENABLED`/`AUTO_REPLY_ENABLED`, key changes,
resuming a worker after a failed migration.

## 10. Evidence (what is proven, and how)

| Claim | Status |
|---|---|
| Canonical 11-step sequence + `init_db()` first runs cleanly on an empty PostgreSQL 16 and re-runs idempotently; guards refuse unsafe targets; 007 pre-check blocks and changes nothing | CI-verified by `tests/test_migrate_release.py` (disposable DB; required, cannot skip). Also run locally on PostgreSQL 16 |
| Step 001 fails on a database with no tables | Locally runtime-verified (README previously said "fresh") |
| `create_all`-only database differs from a migrated one | Locally runtime-verified (index/column diff) |
| Bare `postgresql://` resolves to `psycopg` (not installed) on SQLAlchemy 2.1.4, `psycopg2` on 2.0.54 | Locally runtime-verified; fixed + unit-tested |
| `check_config.py` shape/presence checks and no-secret output | Unit-tested (`tests/test_check_config.py`) |
| Worker SIGTERM ordered drain, `SENDING -> UNKNOWN` | Implemented; covered by existing `tests/test_external_dispatch_scheduler_shutdown.py`; **not** observed on a deployed service |
| `render.yaml` correctness, live service settings, deployed HTTPS, worker liveness, live key agreement | **Not verified** (dashboard-only / C14) |

Deferred: C14 -- deployed HTTPS/worker E2E with disposable SMTP/IMAP. C15 -- runtime
fail-closed auth, `/health` and `/docs` exposure, worker liveness, key rotation,
rate limits, per-tenant sender identity, `UNKNOWN`/`DISPATCH_FAILED` remedy
policy, automated migrations.
