# C13 -- Mailer deployment wiring and operations runbook

Scope: how the Mailer API, the Mailer worker and the Mailer-owned PostgreSQL
are first deployed, then configured, migrated, paused, rolled back and checked.
It changes no delivery logic. A shorter overview is in [DEPLOYMENT.md](DEPLOYMENT.md).

**Two topologies are described here and they are not the same.**

* **Reference topology (`render.yaml`):** LeadBoost stays on Railway; the Mailer
  API runs as a Render web service; **exactly one** Mailer worker runs as a Render
  background worker; the Mailer uses a **dedicated Supabase PostgreSQL** (not
  LeadBoost's database, and not a Render-managed database). Section 0 is the
  first-deployment procedure for it; the rest is the steady-state contract.
* **Observed deployment (split-provider staging/demo):** the API is deployed on
  Render at `https://mailer-agent-api.onrender.com`; the worker has been run as a
  Railway service with `python worker.py`; the database is the dedicated Supabase
  project. A separate Railway service named `web` (a Gunicorn API process, from the
  repository `Procfile`) has also been seen. It is not part of the intended
  topology; its removal is **unconfirmed**. Treat the deployment as staging/demo
  unless newer dashboard evidence says otherwise.

**What has actually been done, and what has not.** Steps marked *performed* are
operator-reported or observed on the hosted services; everything else in this
document is an instruction for future deployments.

| Step | Status |
|---|---|
| Dedicated Supabase project reached over its session pooler (from a WSL shell) | Performed, `HOSTED OBSERVED` |
| Canonical migration wrapper run: all 11 steps completed; final `--status` reported the expected markers present | Performed, operator-reported terminal observation |
| Mailer API deployed on Render; `GET /health` returned 200; `GET /metrics` returned 404. The API runs `init_db()` before serving (a failure stops startup), so this is consistent with a successful database connection at startup; `/health` itself does not probe the database | Performed, `HOSTED OBSERVED` |
| Worker running on Railway: APScheduler started and scheduled dispatch/generation jobs executed (worker logs); observed database use at runtime, not evidence of a dispatch being sent | Performed, `HOSTED OBSERVED` |
| Railway project is on a Limited Trial; outbound SMTP from the worker may be restricted on that plan and the permitted networking and sending path has not been verified | Current deployment caveat; SMTP connectivity from the Railway worker is `NOT VERIFIED` |
| Python 3.12 runtime pin: CI succeeded on the canonical-mirror target repository (run `38626064792`, commit `ce0cbfa`) | `CI VERIFIED` |
| First `init_db()` startup of two processes against the database | A duplicate-sequence error was observed once; a later startup recovered. Since then the race has been reproduced and fixed in code (advisory lock; `SCHEMA_INIT_MODE=verify`), `LOCAL VERIFIED` on disposable PostgreSQL and not re-observed on hosted infrastructure |
| Worker deployed through the Render Blueprint | Not performed (the worker runs on Railway) |
| Removal of the redundant Railway `web` service | Unconfirmed |
| Controlled acceptance: deployed request, dispatch lifecycle and receipt of one message | **Not performed.** Hosted end-to-end delivery is `NOT VERIFIED` |

Frozen invariants this runbook must never conflict with: `ExternalDispatch` is the
durable queue; claims use `FOR UPDATE SKIP LOCKED` + lease/fencing; Txn B -> SMTP
(outside any DB transaction) -> Txn C; Message-ID persisted before SMTP; an
expired `SENDING` becomes `UNKNOWN`, never `QUEUED`; ambiguous outcomes stay
`UNKNOWN` and are never automatically resent.

## 0. First deployment (new Render services + dedicated Supabase project)

Everything in this section is an **operator action in provider dashboards and
shells**. It is the procedure for the reference topology; the status table at the
top of this document records which parts have actually been carried out. Read sections 1-7 first; this section orders them for a
first deployment.

### 0.1 What the Blueprint creates (and does not)

`render.yaml` declares **two** services and **no** database:

| Resource | Created by the Blueprint | Notes |
|---|---|---|
| `mailer-agent-api` (web) | yes | `/health` health check; Gunicorn + Uvicorn; `RUN_SCHEDULER_IN_PROCESS=false`; `API_DOCS_ENABLED=false`; `LIVE_SENDING_ENABLED=false`; `AUTO_REPLY_ENABLED=false` |
| `mailer-agent-worker` (background worker) | yes | `python worker.py`; `maxShutdownDelaySeconds: 60`; sending and auto-reply `false`. **Exactly one** |
| PostgreSQL | **no** | The database is the dedicated Mailer Supabase project, supplied as `DATABASE_URL` |

Every `sync: false` entry in the manifest is typed into the Render dashboard by the
operator; nothing secret is in the file. In particular:

* `DATABASE_URL` is `sync: false` on **both** services and must be the **exact same
  value** on both.
* `MAILBOX_ENCRYPTION_KEY` is `sync: false` on both and is deliberately **not**
  `generateValue`d (Render would generate an independent value per service). Generate
  **one** Fernet key locally, paste the identical value into both services, and store an
  offline copy separate from any database backup (section 4).
* `ORG_KEY_MAP` (tenant keys) and `SYSTEM_OPERATOR_API_KEY` (operator key) are `sync: false`
  on the **API only**. The legacy global `API_KEY` is intentionally **not** declared:
  it is accepted as organization `default` on every protected route, including the
  integration and mailbox routes, so this deployment uses tenant-specific
  `ORG_KEY_MAP` entries only. Do not add `API_KEY` in the dashboard.
* `LEADBOOST_INTEGRATION_SENDER_EMAIL` (API) is the deployment-wide sender identity the
  LeadBoost-generated flow needs; while empty, the first generated-outreach dispatch per
  organization returns 503 (section 2).
* `GROQ_API_KEY` is needed by both services for generation.
* `SMTP_*` / `IMAP_*` are **legacy process-level** settings for non-integrated
  campaigns only. They are deliberately **not declared in the new production Blueprint**,
  so first-time setup does not prompt for unrelated credentials. The integrated LeadBoost
  flow stores per-mailbox SMTP/IMAP credentials encrypted in the Mailer database through
  `POST /mailboxes` and does not read these process-level settings. The legacy IMAP poll is
  skipped when `IMAP_USERNAME`/`IMAP_PASSWORD` are empty (`mailer_agent/mail/imap_reader.py`).
  If the legacy path is deliberately enabled later, add only the required variables
  manually to the appropriate service. Never configure the same physical mailbox through
  both paths.

### 0.2 Connection string: an explicit operator decision

The provider and connection target are decided by the operator, not inferred from any
URL belonging to another environment.

* Copy the connection string from the **Connect** screen of the **dedicated Mailer
  Supabase project itself**. Do not invent hostnames, project references or usernames,
  do not reuse LeadBoost's database URL, and do not paste the string into a ticket, PR,
  chat or log. It contains a password; it lives only in the two Render dashboard entries.
* Prefer the **direct PostgreSQL connection** where the Render execution environment can
  reach it, for both the migration run and the services.
* If the direct endpoint is not reachable from Render because of a network/IP
  compatibility limitation (the Connect screen states which connection types apply to the
  project), assess the **shared session pooler on port 5432**.
* Do **not** default to the shared **transaction pooler on port 6543**. This repository
  has not validated it for the application or for the migration scripts.
* Compare the redacted `host:port/database` printed by `scripts/migrate_release.sh --status`
  with the Mailer Supabase project (and confirm out of band that it is **not** the host
  LeadBoost uses) before anything is applied. The tools never print the user name,
  password or full URL.
* A bare `postgresql://` / `postgres://` URL is pinned to `psycopg2` by
  `db.normalize_database_url`. Use the string as the Connect screen gives it and confirm
  the connection uses TLS; do not weaken it.

### 0.3 Procedure

Steps are in order. Do not start a step before the previous one is confirmed.

1. **Confirm the dedicated Mailer Supabase project is empty or in a documented known
   state.** A new project should have no Mailer tables. Record the state; do not assume it.
2. **Obtain the connection string** through the approved dashboard workflow (0.2).
3. **Configure the exact same database URL on the API and the worker** in the Render
   dashboard (identical value, both services).
4. **Provision credentials before the first start.** Generate (locally, never in chat) one
   tenant key per LeadBoost organization and the separate operator key, plus the single
   `MAILBOX_ENCRYPTION_KEY`. Set `ORG_KEY_MAP` (`{"<mailer_tenant_api_key>":"<mailer_org_id>"}`)
   and `SYSTEM_OPERATOR_API_KEY` on the API only. The operator key must differ from every
   tenant key. With no valid key source the API **refuses to start** (section 3). Keep
   offline copies of the keys and of the encryption key.
5. **Build/deploy the services from the reviewed Blueprint** (Render: New -> Blueprint, point
   at this repository at the reviewed commit). Both services start; each runs `prepare_schema()`
   (by default `init_db()`: create missing tables only, serialized by a database lock). This is **not** a migration: `init_db()` does not apply
   the canonical constraints/indexes (section 5), so a started service does not mean a
   migrated database. Live sending and auto-reply stay `false`.
6. **Before applying anything, establish recovery evidence and run the read-only modes**
   in the approved Render execution environment (for example a shell on the deployed API
   service, whose environment already carries `DATABASE_URL`, so the string need not be
   pasted anywhere; otherwise another approved operator environment holding this
   repository revision and the same value in its **process environment**):
   `scripts/migrate_release.sh --status`, then `scripts/migrate_release.sh --check-only`.
   Recovery evidence for a first bootstrap is described in 0.4.
7. **Review every guard and the migration 007 check** in that output (section 5; the tool
   decides, not this document). Resolve anything unexpected before proceeding.
8. **Apply the guarded, ordered migration only after explicit operator approval** and the
   backup acknowledgement the tool requires: `MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<database name>`
   (the database name the tool printed) `scripts/migrate_release.sh`. The wrapper runs
   `init_db()` and then the canonical **12 steps** in order (`migrations/README.md`; step 12 is the PR 4
   observability migration 010).
9. **If a step fails, stop immediately** and use the recovery procedure (sections 5 and 9).
   Do not run later steps manually, do not run steps individually, and do not
   `stamp`/skip anything.
10. **Verify the final schema state and health**: re-run `--status` (every marker present),
    `GET https://<mailer-api-host>/health` (liveness only), and an authenticated
    `GET /system/health` with a tenant key (key mapping + database reachability, API only).
    Worker liveness: once the PR 4 code is deployed **and** migration 010 applied, the operator-only
    `GET /system/metrics/worker` reports a database-backed heartbeat (section 6). Until then -- and until it has been
    observed on the hosted worker -- there is no worker liveness signal: check the worker's status and logs.
11. **Complete tenant-key and mailbox provisioning before linking LeadBoost.** Confirm each
    tenant key maps to the Mailer organization that will own the mailbox, and that exactly one
    **ACTIVE** mailbox exists per organization (`POST /mailboxes`, normally issued by
    LeadBoost's mailbox sync; see the README's "Using this from LeadBoost"). Only then set
    `MAILING_AGENT_BASE_URL` (`https://<mailer-api-host>`, the real deployed HTTPS URL) and
    `MAILING_AGENT_ORG_API_KEYS` on the LeadBoost backend (Railway). That is a separate,
    later operator action.
12. **Keep `LIVE_SENDING_ENABLED=false` and `AUTO_REPLY_ENABLED=false`** on both services
    until the separate controlled acceptance step. Enabling either is an explicit operator
    approval (section 9).

### 0.4 First bootstrap vs. a later migration

| | New, empty dedicated database (first deployment) | Later migration of a database holding real dispatches/mailboxes |
|---|---|---|
| Starting state | Empty, or the documented known state recorded in step 1 | Live data |
| Queue | There is no queue. **Do not state or record that a queue was drained.** | Suspend the worker and let it drain first (section 7); migration 007 blocks while non-terminal dispatches exist and the column is absent |
| Recovery evidence | There is no application data to back up. **Do not claim a backup of application data exists.** Record the evidence that actually exists: the `--status` output showing the empty state, and the provider's own recovery capability for the project as verified by the operator | A backup of the data, **verified to restore**, with the matching `MAILBOX_ENCRYPTION_KEY` (a backup is useless for mailbox credentials without it) |
| Acknowledgement | **Still required** -- the wrapper's apply gate is never bypassed for a fresh database. Set `MIGRATE_CONFIRM_BACKUP` only after the operator has recorded the evidence above and approved; it is the tool's single approval gate, not a statement that data was backed up | Required, and means exactly what it says |
| `--status` / `--check-only` | Read-only; run them (step 6) | Read-only; run them |
| Migration 007 | Reviewed like any other guard. If `external_dispatches.mailbox_id` already exists (it does once `init_db()` has run on current code) the check is informational; read the tool's output rather than assuming | Reviewed; drain first |

`init_db()` creating tables is **not** the same as the canonical migrations having been
applied. The wrapper always runs both, in order.

## 1. Process model

| Process | Start command | Scheduler | Instances |
|---|---|---|---|
| API (`render.yaml`: `mailer-agent-api`, web) | Gunicorn + Uvicorn workers (`-w 2`) | **None.** Requires `RUN_SCHEDULER_IN_PROCESS=false` | scale freely |
| Worker (`mailer-agent-worker`, background worker) | `python worker.py` | **Always starts one** (reply poll, follow-up, dispatch and recovery jobs, `max_instances=1` per job) | **Exactly one** |
| Database | dedicated Mailer Supabase PostgreSQL (**not** created by `render.yaml`; supplied as `DATABASE_URL`) | -- | one |

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
It reports presence, validity and shape without printing secrets. It also emits
non-secret diagnostic details, including a redacted database target, organization
IDs and scheduler status, and reports `LIVE_SENDING_ENABLED` /
`AUTO_REPLY_ENABLED` as seen by the **environment of the shell it runs in**.
It does not read the provider dashboard or another service's environment, does
not report `API_DOCS_ENABLED`, and cannot tell you what an already-running
process started with (section 6).

| Variable | API | Worker | Secret | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | yes | yes | yes | The dedicated Mailer Supabase URL, **identical on both**, set in the dashboard (`sync: false`), never in the repository (section 0.2). **Defaults to local SQLite if unset** (no startup guard). A bare `postgresql://` is pinned to `psycopg2` by `db.normalize_database_url` |
| `MAILBOX_ENCRYPTION_KEY` | yes | yes | yes | Fernet key; **identical on both, stable forever** (section 4) |
| `ORG_KEY_MAP` | yes (required to start) | no | yes | `{"<mailer_tenant_api_key>":"<mailer_org_id>"}`; API only |
| `API_KEY` | **not set in this profile** | no | yes | Legacy. If set it is **always** an accepted key mapped to org `default` on every route, including integration routes. The Blueprint does not prompt for it; leave it unset |
| `ORG_KEYS_<org>` | optional | no | yes | Third key source, merged below `ORG_KEY_MAP` |
| `LEADBOOST_INTEGRATION_SENDER_EMAIL` | yes (`sync: false`) | no | no | Deployment-wide sender persona (C15 limitation). Empty -> first generated-outreach dispatch per org returns 503 |
| `GROQ_API_KEY` | yes | yes | yes | Generation |
| `LIVE_SENDING_ENABLED` | own copy, **not** what decides LeadBoost dispatch (used by the legacy `POST /messages/{id}/approve` route and the insecure-auth start-up guard; also logged at API startup) | **decides dispatch** | no | Separate copy per service -- see sections 6 and 7 |
| `AUTO_REPLY_ENABLED` | yes | yes | no | Separate copy per service. Keep `false` unless authorised |
| `SYSTEM_OPERATOR_API_KEY` | yes (dashboard secret, declared `sync: false`) | **do not set** (ignored) | yes | Operator-only credential for the process-wide `/system/*` routes, sent as `X-System-Operator-Key`. **Not a tenant key**; must differ from every tenant key or the API refuses to start. Unset = those routes answer 503. See section 6 |
| `API_DOCS_ENABLED` | yes, pinned `"false"` in the Blueprint | ignored | no | `/docs`, `/redoc` and `/openapi.json` return 404. Enabling is a deliberate operator choice -- see section 6 |
| `RUN_SCHEDULER_IN_PROCESS` | **must be `false`** (pinned in the Blueprint) | n/a | no | See section 1 |
| `EXTERNAL_DISPATCH_SHUTDOWN_DRAIN_SECONDS` | no | yes | no | Default 25 |
| `EXTERNAL_DISPATCH_POLL_SECONDS`, `_RECOVERY_POLL_SECONDS`, `_MAX_PER_CYCLE`, `_LEASE_SECONDS`, `_GENERATION_LEASE_SECONDS` | no | yes | no | Tunables; defaults 15 / 60 / 5 / 900 / 300. Do not change as part of a deploy |

SMTP/IMAP for the LeadBoost flow are **per-mailbox** (stored encrypted via
`POST /mailboxes`), not process environment variables. The process-level `SMTP_*` /
`IMAP_*` variables are **not declared in the new production Blueprint**. Add them
manually only if deliberately enabling the legacy non-integrated campaign path.
LeadBoost does not yet provision IMAP settings; the staging operator PATCH is a C14
procedure. Exactly one ACTIVE mailbox per organization is required for the integrated
flow; with none (or several) the generated-outreach route answers 409 and LeadBoost
reports `mailing_agent_conflict`.

## 3. API-key contract with LeadBoost

The two sides use **inverse** shapes. Neither value should ever be pasted into a
ticket, PR, log or chat. These are the exact LeadBoost variable names (read by
`core/infrastructure/mailing_agent/client.py` on LeadBoost `main`); the legacy
`MAILING_AGENT_API_KEY` and any `MAILER_AGENT_*` names are not read. A Mailer tenant key
is **not** `SYSTEM_OPERATOR_API_KEY`: never give LeadBoost the operator key.

| Side | Variable | Shape |
|---|---|---|
| LeadBoost (Railway) | `MAILING_AGENT_BASE_URL` | the Mailer API's HTTPS base URL (`https://<mailer-api-host>`, no key in it) |
| LeadBoost (Railway) | `MAILING_AGENT_ORG_API_KEYS` | `{"<leadboost_org_id>": "<mailer_tenant_api_key>"}` |
| Mailer | `ORG_KEY_MAP` | `{"<mailer_tenant_api_key>": "<mailer_org_id>"}` |

Both sides must agree per organization: the key LeadBoost sends for org X must
map, on the Mailer, to the org id the Mailer holds mailboxes for. The Mailer
derives the organization **from the key**, never from a request body.

**Fail closed (C15.1, applies from the revision that contains it).** If no keys
are effective (empty `ORG_KEY_MAP`, no `API_KEY`, no `ORG_KEYS_*`) every
protected route answers 503 and the API **refuses to start**. A malformed
`ORG_KEY_MAP` (invalid JSON, JSON that is not an object, or entries that are not
non-empty string pairs) rejects the whole authentication configuration the same
way: nothing is ignored, the legacy key is not used as a fallback, and the
startup error never contains key values. `ALLOW_INSECURE_NO_AUTH=true` is the only
opt-out: local development only, ignored whenever any key is configured,
refused together with `LIVE_SENDING_ENABLED=true`, and never honoured by the
integration or mailbox routes. Never set it on a deployed service.
**Before deploying that revision, confirm in the dashboard that the API service
has at least one valid key source** (`ORG_KEY_MAP`, `ORG_KEYS_*` or `API_KEY`);
otherwise the new revision fails its start-up check and does not come up
(confirm in the deploy logs and Render's deploy behaviour that the previous
revision keeps serving). `check_config.py
--role api` reports every case. Legacy `API_KEY` semantics are unchanged: it is
still accepted on every route, including the integration and mailbox routes, as
org `default`.

Safe verification after a key change (read-only, no send): from a trusted shell,
`GET https://<mailer-host>/mailboxes` with `X-API-Key: <key>` should return 200
for a valid key and 401/403 for a wrong key, and `GET /system/health` (authenticated; that organisation's
counts only; queries the database) should return 200. Both prove TLS +
key mapping + database reachability **for the API only**; they say nothing about
the worker. Rotation/revocation procedure is C15/C16.

### Inbound webhook (`POST /webhooks/inbound-email`) -- tenant scoping (C15.1b)

From the revision that contains C15.1b the webhook resolves the tenant **from the
presented key** (fail-closed: 401 for a missing/unknown key, 503 when no keys are
configured) and restricts every lookup -- duplicate check, threading-header
correlation, e-mail fallback and the post-`IntegrityError` recovery -- to that
organization's campaigns. A reply that references another tenant's thread, or a
`Message-ID` another tenant already holds, is answered `{"matched": false, ...}`
with no identifier and nothing is written. The global uniqueness constraint on
inbound `Message-ID` is unchanged.

**Compatibility -- an operator decision is required before deploying.** Whether a
live inbound-parse provider (Postmark/Mailgun/SendGrid) posts to this endpoint,
and which key it sends, is a dashboard fact this repository cannot show. A key now
reaches only the campaigns of the organization it maps to. A provider that sends
the legacy `API_KEY` (org `default`) will no longer match campaigns owned by any
other organization; give it a key mapped to the owning organization (or stop using
the webhook for that tenant). Do not "fix" this by loosening the scoping. The
deployment-global legacy IMAP poll is deliberately unscoped (trusted internal
input) and is unchanged; the per-mailbox IMAP path is separate and unchanged.

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
("Release procedure"). For the **first** deployment on an empty dedicated database
follow section 0.3/0.4, which keeps every guard below. Summary for a database that
already holds data:

1. Back up; verify the backup restores.
2. Suspend the worker (section 7).
3. `DATABASE_URL=... scripts/migrate_release.sh --status`, then `--check-only`.
4. `MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<db> scripts/migrate_release.sh`.
5. Deploy API + worker together; resume the worker.

Rules: migrations never run from API/worker startup; never downgrade a database
that holds customer data (006 downgrade destroys mailbox credentials; 007-009
drop columns/indexes); never run scripts out of order or individually on a
production database. **Existing database (not a first bootstrap):** the live schema state is unknown to
this repository -- run `--status` first and treat any `ABSENT` marker as "needs
migrating". A database that only ever ran the application's own `create_all`
lacks the inbound-dedupe unique index and the work-queue indexes (measured in
C13), so "the app starts" does not mean "migrated".

`init_db()` creates missing tables only; it is **not** a substitute for the canonical
constraints/indexes, and "the services started" never means "the migrations ran".

Migration 007: drain non-terminal dispatches first (the tool blocks otherwise). On a
verified-empty first bootstrap there is nothing to drain; do not record that anything was.

## 6. Health semantics (do not over-read them)

| Endpoint | Auth | What it proves | What it does **not** prove |
|---|---|---|---|
| Mailer `GET /health` | none | The API process is up and serving. Returns exactly `{"status":"ok"}` | Database, worker, SMTP, IMAP, readiness -- and **no configuration**: it does not report `LIVE_SENDING_ENABLED` or `AUTO_REPLY_ENABLED` (earlier revisions did) |
| Mailer `GET /system/health` | a tenant API key | Auth + key mapping + database queries work, for that org. Returns **only that organisation's** counts (earlier revisions also returned process-wide LLM counters and the API's flags; they were removed) | Worker liveness, SMTP/IMAP, any process-wide state |
| Worker | operator key, `GET /system/metrics/worker` | **PR 4, not yet deployed or observed anywhere:** a heartbeat `fresh`/`stale`/`stopped` per scheduler process (requires migration 010). Until it has been observed on the hosted worker, detect failure from service status/restarts and logs | SMTP/IMAP/LLM connectivity or job success -- a fresh heartbeat is process liveness only (see `OPERATOR_OBSERVABILITY.md`) |

Render's health check on `/health` therefore only gates routing to a live
process. `/health` is unauthenticated by design and now minimal. PR 4 adds a worker
liveness signal in code (`/system/metrics/worker`); **do not claim the worker is monitored**
until that endpoint has been observed reporting a fresh heartbeat from the deployed worker,
and then only for what a heartbeat shows (the process is alive and its scheduler ticking).

### Where to look for each fact

The API and the worker are separate processes with separate environments. A
setting on one says nothing about the other.

| Question | Look at | Not a valid source |
|---|---|---|
| Will the **worker** send mail? (its `LIVE_SENDING_ENABLED`) | The worker service's environment in the provider dashboard; `python scripts/check_config.py --role worker` in the worker's shell (prints the flag as seen by that shell) | `/health`, `/system/health`, `/system/config`, the API service's settings |
| What is the **API** configured with? (key sources, `RUN_SCHEDULER_IN_PROCESS`, `API_DOCS_ENABLED`, its own copy of the flags) | The API service's environment in the dashboard; `check_config.py --role api` in the API's shell; or `GET /system/config` with the operator key (describes the API process only) | The worker's settings |
| Is the worker alive and making progress? | Provider service status, restarts and logs; a known test dispatch advancing `queued -> sending -> sent` | `/health` (it only speaks for the API) |
| Is a running process using the value that is set now? | The provider's deploy/restart history for that service. Settings are read when the process starts | The dashboard value alone |

None of this proves the live provider configuration is correct; that is verified
in C14 against the deployed services.

### `/system/*`: tenant metrics vs operator diagnostics (`SYSTEM_OPERATOR_API_KEY`)

Some `/system/*` data belongs to one organisation; some describes the whole
process and is not tenant data. They are separated:

| Route | Data | Credential |
|---|---|---|
| `GET /system/health`, `/system/metrics/classification`, `/system/metrics/states` | **Organisation-scoped** (the calling key's organisation only) | Tenant key: `X-API-Key` |
| `GET /system/config` | **Process-wide**: the API process's non-secret settings (including its `LIVE_SENDING_ENABLED` / `AUTO_REPLY_ENABLED`, which are **not** the worker's) | Operator key: `X-System-Operator-Key` |
| `GET /system/metrics/llm` | **Process-wide**: LLM provider call counters | Operator key |
| `GET /system/integration/validate` | **Process-wide**: wiring/database checks; on failure it returns the raw error text, for the operator | Operator key |
| `GET /system/metrics/dispatch` | **Process-wide** (all organizations): dispatch counts by state, queue depth/age, stale `sending`, `unknown` count. `sent` = SMTP accepted, not delivered. No identifiers or error text | Operator key |
| `GET /system/metrics/worker` | **Process-wide**: scheduler-process heartbeat and latest job-cycle outcomes. Opaque random process ids only | Operator key |
| `GET /system/metrics/inbound` | **Process-wide**: mailbox-poll cycle evidence, legacy global-IMAP path (separate), persisted inbound counts. Aggregates only | Operator key |

Access rules (the operator routes are independent of tenant keys):

- **A tenant key never grants operator access.** Neither does the legacy `API_KEY`, nor any `ORG_KEY_MAP` / `ORG_KEYS_*` value. The operator routes also need no tenant key.
- Unset or blank `SYSTEM_OPERATOR_API_KEY` -> the three operator routes answer **503** to everyone (fail closed; `ALLOW_INSECURE_NO_AUTH` does not open them). A missing, empty or wrong presented key -> **401** (same message for both). A correct key -> 200.
- The operator key must be a **distinct** secret. If it equals any tenant key the API **refuses to start** (and the routes answer 503 until fixed); `python scripts/check_config.py --role api` reports this before a deploy.
- API service only. The worker never reads it; `check_config.py --role worker` warns if it is set there.
- The tenant `/system/health` response no longer contains `llm_metrics_process_wide` or `settings`. Any caller that read those fields must switch to the operator routes. No caller in this repository or on LeadBoost `main` does; external monitoring tooling is not visible from the repositories.

Configuring it (no value is ever shown in this repository or by these tools):

1. Generate a random value locally (as for any key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`) and do not paste it into tickets, chat or commits.
2. Set it as a secret environment variable on the **API** service in the provider's dashboard only (`render.yaml` declares it `sync: false` on the API so the Blueprint prompts for it; it provisions no value), then restart/redeploy the API (settings are read at start-up). Never set it on the worker.
3. Verify without sharing output: with the key, `GET /system/config` -> 200; with a tenant key only -> 401.

Rotation: there is a single operator key and no overlap window. Generate a new value, set it on the API service, restart the API, then update whatever operator tooling holds it. The old value is rejected (401) as soon as the API restarts. Never reuse a tenant key, and rotate it like any other secret if exposed.

### API documentation endpoints (`API_DOCS_ENABLED`)

`/docs`, `/redoc` and `/openapi.json` return 404 unless `API_DOCS_ENABLED=true` is
set on the **API** service (default `false`; read at API start-up, so a
restart/redeploy applies a change; the worker ignores it). When enabled, all
three are served **without an API key**, they reveal the complete route and
schema inventory to anyone who can reach the service, and FastAPI's default
pages load their assets from a public CDN. Enable it only deliberately (for
example local development or a private network) and record the decision. The Blueprint
pins it to `false`; leave it that way on a deployed service, and do not use the docs page to check that the API is
up -- use `/health`.

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

`maxShutdownDelaySeconds: 60` is set on the worker in `render.yaml`, so a worker created
from the Blueprint has it. If the worker is ever created or edited outside the Blueprint,
apply the equivalent shutdown-delay setting in the dashboard; the repository file does not
change a live service by itself.

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

### 9.1 PR 4 release checklist (operator observability)

Nothing below has been performed. Every hosted fact is **unknown until observed**; record what you see, not what is expected.

1. **Before anything:** confirm the exact CI run for the PR head (all four jobs) and read its report-only Ruff/Bandit/pip-audit output. Those three steps are **not** blocking gates.
2. **Backup and approval** per section 5. Migration 010 is additive (two small tables, one partial index) but it is still a migration: it needs the verified backup and your explicit approval like any other.
3. Read-only first: `scripts/migrate_release.sh --status`, then `--check-only`. Expect markers `010 worker_runtime_status table`, `010 worker_cycle_status table` and `010 ix_messages_inbound_created_at` to be **ABSENT** before the run.
4. **Measure before you migrate -- mandatory, do not run it blindly.** The index build blocks writes to `messages` for its duration, and the PR's measurements are synthetic. Complete section 9.1.1 (size, write rate, open transactions, the callers that write `messages`, an agreed write-interruption window) and write down the numbers and the decision.
5. Apply with the wrapper (never run `010_worker_runtime_status.py` by hand against the real database), with the worker suspended **and the API writers of `messages` handled as decided in 9.1.1**, and with **`MIGRATE_LOCK_TIMEOUT_MS=5000`** (or the value you chose) set. Without it the wrapper waits behind active writers: this was observed, not assumed (9.1.1, point 5). Expect `(12/12)`, then every marker present and valid.
6. **Then** deploy API and worker together. Order matters for any service in `SCHEMA_INIT_MODE=verify`: it will refuse to start until 010 exists (`python scripts/migrate_preflight.py verify` shows `missing table worker_runtime_status` beforehand; this check is read-only).
7. Set `SYSTEM_OPERATOR_API_KEY` (API service only) if it is not already set; without it the new endpoints answer `503`. Leave the four `WORKER_*`/`OPERATOR_*` settings at their defaults unless you have a reason; if you change one, change it on **both** services.
8. Observe, and write down only what you see: `GET /system/metrics/worker` shows one `fresh` instance with role `worker` within about a minute of the worker starting; its `heartbeat_age_seconds` stays below the stale threshold; `GET /system/metrics/dispatch` returns plausible counts. **Not yet knowable from this change:** that SMTP/IMAP work from the hosted worker, that any dispatch is delivered, the hosted worker's database latency, or how a hosted restart presents.
9. Graceful stop vs kill is demonstrated locally with real processes; on the host, a deliberate redeploy should show the old instance `stopped` and a new one `fresh`. A host-side forced kill should show `stale`. Neither has been observed on hosted infrastructure.

**Rollback.** Code: redeploy the previous revision of both services; it ignores the new tables and index, so no schema action is needed (the older "verify" caveat in section 9 about 009 is unrelated to 010). Schema, only if you must: `python migrations/010_worker_runtime_status.py --downgrade` drops the two telemetry tables and the index and touches no business data. A backup restore is not needed for 010.

### 9.1.1 Write-interruption assessment for the `messages` index (mandatory before step 5)

A plain `CREATE INDEX` takes a `SHARE` lock on `messages`: reads continue, but every `INSERT`/`UPDATE`/`DELETE` on it waits for the whole build. Suspending the worker is **not** enough, because the HTTP API writes `messages` too. This is read from the code, not observed in production:

| Writer | Entry point |
|---|---|
| API | `POST /webhooks/inbound-email` (stores inbound mail, may store an auto-reply), `POST /messages/{id}/approve`, `POST /integrations/leadboost/outreach-actions` |
| Worker | generation, dispatch, follow-up engine, mailbox inbound polling, lease-recovery sweeps |

`POST /integrations/leadboost/outreach-requests` writes `external_dispatches` only, and `POST /suppress` writes suppressions only; neither touches `messages`.

1. **Inspect the real database** (read-only; never selects query text): `psql "$DATABASE_URL" -f scripts/migrate_010_inspect.sql`. Record the table and index size, estimated rows, the write counters (statement 2: run it twice about 60 s apart and subtract), every open transaction and its age (statement 3: anything older than the lock timeout is something the build would wait behind), the locks on `messages`, and whether the index already exists and is valid.
2. **Measure the build on a restored backup**, not on production: restore the backup you already took, run `\timing` and `CREATE INDEX ix_messages_inbound_created_at ON messages (created_at) WHERE direction = 'inbound';` there, and note the elapsed time. Do not extrapolate from the PR's synthetic numbers (60,000 rows, 4 MB): they say nothing about your table.
3. **Agree the write-interruption window** with the owners of the callers, and write the number down. Specifically find out, rather than assume, whether the inbound-email provider retries a webhook that times out or errors, and for how long (not knowable from this repository), and how LeadBoost's callers of `approve` / `outreach-actions` behave on a timeout.
4. **Choose:**

| Measured build time and open transactions | Action |
|---|---|
| Comfortably inside the agreed window, no transaction older than a few seconds | Standard path: the wrapper with `MIGRATE_LOCK_TIMEOUT_MS=5000` (migration 010 also applies its own 5 s default). Pause or drain callers if you can. |
| Longer than the window, or long-lived transactions are present | Build the index first **by hand with `CONCURRENTLY`** (below), confirm it is valid, then run the wrapper: it sees the index and skips it. |
| No quiet moment is available | Do not force it. The lock timeout aborts with `NOTHING was changed`; retry later. |

5. **The wrapper itself contends with writers, not only step 12.** Several *existing* steps (001, 002, ...) run `CREATE INDEX IF NOT EXISTS ... ON messages(...)`, which needs the table's `SHARE` lock **even when the index already exists** (reproduced on a disposable PostgreSQL 16: with an uncommitted writer open, the real wrapper hung at step 1 of 12 until stopped, and a manual `CREATE INDEX IF NOT EXISTS` on an existing index hit `lock timeout`). So re-running the wrapper on an already-migrated database is not "free" while writers are active. This predates PR 4 and PR 4 inherits it. Guard: set `MIGRATE_LOCK_TIMEOUT_MS` (100-300000; `0` is refused) for the wrapper run; it applies PostgreSQL `lock_timeout` to every statement of every step through `PGOPTIONS`, and the same run then fails in seconds with `LockNotAvailable` and `FAILED at step N/12` instead of hanging. It is **off by default** (no behaviour change; the wrapper prints a NOTE saying so). Pooler caveat: if your `DATABASE_URL` goes through a connection pooler that rejects startup options (`unsupported startup parameter: options`), use a direct or session-mode connection for migrations or quiesce all writers; **whether your hosted pooler accepts the option is unknown until tried with `--check-only`-adjacent testing on a restored copy.** After an aborted wrapper run: steps 001-009 are idempotent and safe to re-run, `init_db()` may already have created the two empty new tables (additive and harmless), and migration 010 itself is atomic (an abort inside it changes nothing). The wrapper's "restore from the backup" hint is the generic one; a pure lock-timeout abort does not need a restore.
6. **Why a lock timeout in migration 010 as well.** The build first has to acquire its lock. While it waits behind a long-running writer, every *new* writer queues behind it, so even a build that has not started can stall the application. With `MIGRATION_010_LOCK_TIMEOUT_MS` (default 5000, accepted range 100-300000; `0` is clamped up, never "wait forever") the migration gives up, the transaction rolls back (PostgreSQL DDL is transactional, so the two new tables are rolled back too) and the script exits non-zero. Optionally cap the build itself with `MIGRATION_010_STATEMENT_TIMEOUT_MS` (unset = no cap; a cancelled build also changes nothing). If a run aborts, look at statement 3 again and retry; do not raise the timeout to "wait it out" unless you have accepted writes queueing for that long.
7. **Manual `CONCURRENTLY` path** (only if step 4 says so). In `psql` autocommit, never inside a transaction block, as the application's database role: `CREATE INDEX CONCURRENTLY ix_messages_inbound_created_at ON messages (created_at) WHERE direction = 'inbound';`. It does not block writes, takes longer and uses more I/O. If it is cancelled or fails it leaves an **INVALID** index: `DROP INDEX ix_messages_inbound_created_at;` and retry. The migration, `--status` and `verify` all refuse to count an invalid index as present, so a leftover cannot be mistaken for success.
8. After the run: `--status` shows all three 010 markers present, and `python scripts/migrate_preflight.py verify` exits 0.

**Unknown until observed:** the real table size and growth, the real write rate, the transactions open at migration time, how long the build takes on the real instance, whether the hosted pooler accepts `lock_timeout` as a startup option, and the webhook provider's retry behaviour. None of these are established by this change.

## 10. Evidence (what is proven, and how)

Labels: `CI VERIFIED`, `LOCAL E2E VERIFIED`, `HOSTED OBSERVED`, `IMPLEMENTED, NOT HOST-VERIFIED`, `NOT VERIFIED`, `KNOWN LIMITATION`, `DEFERRED` (definitions in [FINAL_PRODUCTION_READINESS.md](FINAL_PRODUCTION_READINESS.md)). Rows that say "CI-verified", "CI-tested" or "unit-tested" below refer to the test files named in the row; they are not hosted observations.

| Claim | Status |
|---|---|
| Canonical 11-step sequence + `init_db()` first runs cleanly on an empty PostgreSQL 16 and re-runs idempotently; guards refuse unsafe targets; 007 pre-check blocks and changes nothing | CI-verified by `tests/test_migrate_release.py` (disposable DB; required, cannot skip). Also run locally on PostgreSQL 16 |
| Step 001 fails on a database with no tables | Locally runtime-verified (README previously said "fresh") |
| `create_all`-only database differs from a migrated one | Locally runtime-verified (index/column diff) |
| Bare `postgresql://` resolves to `psycopg` (not installed) on SQLAlchemy 2.1.4, `psycopg2` on 2.0.54 | Locally runtime-verified; fixed + unit-tested |
| `check_config.py` shape/presence checks and no-secret output | Unit-tested (`tests/test_check_config.py`) |
| Worker SIGTERM ordered drain, `SENDING -> UNKNOWN` | Implemented; covered by existing `tests/test_external_dispatch_scheduler_shutdown.py`; **not** observed on a deployed service |
| `/docs`, `/redoc`, `/openapi.json` return 404 by default and 200 only with `API_DOCS_ENABLED=true`; `/health` returns exactly `{"status":"ok"}` with 200 | CI-tested by `tests/test_pr2_api_surface.py` (the real app built in a fresh process). **Not** observed on a deployed service |
| Tenant `/system/health` is organisation-scoped and has no process-wide fields; the three operator routes reject a tenant key / legacy key / missing / wrong operator key, fail closed when unset, and need no tenant key; an operator key equal to a tenant key is refused at start-up; no credential reaches responses or logs | CI-tested by `tests/test_pr3_system_operator_boundary.py` and `tests/test_c15_1_auth_fail_closed.py`; `check_config.py` handling by `tests/test_check_config.py`. **Not** observed on a deployed service, and **no operator key has been provisioned** anywhere |
| `render.yaml` declares two services and no database; `DATABASE_URL` is `sync: false` on both; `ORG_KEY_MAP`/`SYSTEM_OPERATOR_API_KEY` are API-only secrets; no legacy `API_KEY`; docs, in-process scheduler, live sending and auto-reply pinned `false`; one worker with the shutdown drain; no secret/URL in the file | CI-tested (static, no YAML dependency) by `tests/test_render_blueprint.py` |
| Dedicated Supabase project reachable over its session pooler; canonical 11-step wrapper run completed with expected markers on `--status` | `HOSTED OBSERVED` (operator-reported terminal observation; pooler reached from a WSL shell, not from Render) |
| Render API `GET /health` 200; `GET /metrics` 404 (no such route exists) | `HOSTED OBSERVED` |
| Worker running on Railway: APScheduler started; dispatch and generation jobs executed | `HOSTED OBSERVED` (worker logs) |
| A concurrent first `init_db()` start produced a duplicate-sequence error once; a later start recovered | `HOSTED OBSERVED`; fixed in code with a regression test (`LOCAL VERIFIED`); hosted re-observation pending |
| Outbound SMTP from the Railway worker (Limited Trial caveat) | `NOT VERIFIED` |
| Backups/recovery of the Supabase project, Render-to-Supabase connection type, deployed HTTPS behavior of fail-closed auth, `/docs` exposure, **hosted** worker liveness signal (PR 4 implements it; not yet observed on the hosted worker), live key agreement between LeadBoost and Mailer, removal of the redundant Railway `web` service | `NOT VERIFIED` (dashboard-only facts; no evidence recorded) |
| A deployed request through to a received message (hosted end-to-end delivery) | `NOT VERIFIED` (controlled acceptance not performed) |

Deferred: C14 -- deployed HTTPS/worker E2E with disposable SMTP/IMAP. C15 -- deployed
verification of the C15.1 fail-closed auth change, `/health` and `/docs` exposure, worker liveness, key rotation,
rate limits, per-tenant sender identity, `UNKNOWN`/`DISPATCH_FAILED` remedy
policy, automated migrations.
