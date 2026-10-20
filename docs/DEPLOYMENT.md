# Deployment

A readable overview of how Mailer Agent is deployed and released. Step-by-step procedures, with guards and checklists, are in the [deployment runbook](C13_DEPLOYMENT_RUNBOOK.md); migration details are in [migrations/README.md](../migrations/README.md). Evidence labels are defined in the [readiness report](FINAL_PRODUCTION_READINESS.md).

## 1. Two topologies, not one

| | Reference topology | Observed deployment |
|---|---|---|
| Source | `render.yaml` Blueprint | Operator observations (dashboards, logs) |
| API | Render web service `mailer-agent-api` | Render, `https://mailer-agent-api.onrender.com` |
| Worker | Render worker `mailer-agent-worker` | Railway service running `python worker.py` |
| Database | External PostgreSQL supplied as `DATABASE_URL`; the Blueprint provisions none | Supabase (dedicated project), reached through its session pooler |
| Status | `IMPLEMENTED, NOT HOST-VERIFIED` as a Blueprint apply | `HOSTED OBSERVED` in parts (below); a split-provider staging or demo arrangement |

Observed facts (`HOSTED OBSERVED`): the Render API answered `200` on `/health` and `404` on `/metrics`. The API runs `init_db()` before it serves, and a failure there stops startup, so a responding API is consistent with a successful database connection and schema check at startup (`/health` itself does not probe the database). Railway worker logs showed APScheduler starting and the scheduled dispatch and generation jobs executing, which is observed database use at runtime. Neither observation shows a dispatch being delivered; Supabase was reachable through its session pooler from a developer machine; the canonical migration wrapper completed its 11-step sequence and the final `--status` reported the expected markers present.

**Railway networking caveat.** The Railway project is on a Limited Trial, and outbound SMTP from the worker may be restricted on that plan. The account's actual permitted networking and sending path has not been verified, so the worker's ability to reach a mailbox SMTP server from Railway is `NOT VERIFIED` and is a current deployment caveat. Check Railway's current plan documentation and test with a controlled send before relying on it.

A second Railway service named `web`, running a Gunicorn API process, has also been seen. It is not part of the intended topology. The repository `Procfile` declares both a `web` and a `worker` process type, which is the likely origin of a second service, but the cause was not confirmed. Whether it has been removed is unconfirmed. If it is still running with `RUN_SCHEDULER_IN_PROCESS` unset (the default is `true`), each of its Gunicorn workers would start a scheduler alongside the real worker. Claims and fencing keep that correct, but it is not the intended single-scheduler setup. Confirm its state and remove or reconfigure it.

## 2. Service responsibilities

| Service | Runs | Must have | Must not |
|---|---|---|---|
| API | `gunicorn mailer_agent.api.main:app -k uvicorn.workers.UvicornWorker -w 2 --bind 0.0.0.0:$PORT --timeout 60` | `RUN_SCHEDULER_IN_PROCESS=false`, `ORG_KEY_MAP`, `MAILBOX_ENCRYPTION_KEY`, `DATABASE_URL` | Run a scheduler when a worker exists |
| Worker | `python worker.py` | `DATABASE_URL` (same database as the API), `MAILBOX_ENCRYPTION_KEY` (same value), `GROQ_API_KEY` | Be started in more than one instance; serve HTTP (it has no endpoint) |
| Database | PostgreSQL | Reachability from both services | Be shared with LeadBoost; Mailer owns its own |
| LeadBoost backend | Elsewhere (Railway) | `MAILING_AGENT_BASE_URL`, `MAILING_AGENT_ORG_API_KEYS` | Hold or expose Mailer secrets to the browser |

Runtime: Python 3.12, pinned by `.python-version` (the pin was committed as "build: pin Render Python runtime to 3.12"). CI also uses 3.12.

## 3. Repositories, CI and mirroring

The canonical repository `anupamkr1708/LeadBoost-mail-agent` is the source of truth. Its `main` is mirrored to the Render-connected repository `anonymous-martin/LeadBoost-mail-agent` by `.github/workflows/sync-render-repo.yml`, which runs only after the canonical CI workflow succeeds on `main`. The mirror repository runs its own CI, and Render auto-deploys from it. So a merge to `main`, including a documentation-only merge, triggers CI, then the mirror, then a Render deploy of the API (and the Railway service if it tracks the mirror).

`CI VERIFIED`: CI run `38626064792` on the Render-connected repository concluded `success` at commit `ce0cbfa` (the Python 3.12 runtime pin).

**Change policy.** The intended practice is a reviewed pull request for routine changes. Branch protection on `main` has not been verified and is not claimed here, and direct pushes to `main` have been accepted in practice. The consequence is the same either way: any commit that reaches canonical `main` and passes CI triggers the mirror, the target repository's CI and the provider deployment. Treat a push to `main` as a deployment.

## 4. Release sequence

A code deployment and a database migration are **separate release actions**. The migration wrapper is never run by startup or by every deploy.

1. **Back up** the database and record the `MAILBOX_ENCRYPTION_KEY` backup location.
2. **Check configuration** on each service: `python scripts/check_config.py --role api` and `--role worker` (read-only; prints no secret values).
3. **Migrate**, as a deliberate operator step, from a machine with the production `DATABASE_URL` in its environment:
   ```bash
   scripts/migrate_release.sh --status        # read-only: report schema state
   scripts/migrate_release.sh --check-only    # read-only: run every guard
   MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<database name> scripts/migrate_release.sh
   ```
   Apply new migrations **before** deploying code that depends on them: the ORM selects new columns on every query.
4. **Deploy** the API and worker. Two processes starting together against an empty database raced once (`HOSTED OBSERVED`; a later start recovered). `init_db()` is now serialized by a PostgreSQL advisory lock, and a service may run with `SCHEMA_INIT_MODE=verify` (read-only, no DDL). The race was reproduced on disposable PostgreSQL before the fix and a regression test now covers it (`LOCAL REPRODUCED` / `LOCAL VERIFIED`); it has **not** been re-observed on hosted infrastructure. Run the migration wrapper before the first start of a new database regardless: it remains the source of truth for the schema, because `init_db()` creates tables only. To make a deployed service run no DDL at all, set `SCHEMA_INIT_MODE=verify` on it (see the staged procedure below).
5. **Verify health**: `GET /health` answers 200 (liveness only); an authenticated `GET /system/health` proves database access; worker logs show the scheduler starting and the dispatch and generation jobs running. There is no worker HTTP endpoint and no `/metrics`.
6. **Controlled acceptance** (not yet performed): with `LIVE_SENDING_ENABLED=true` on the worker only, send one request to a disposable recipient you control, through the deployed API, and record the request, the dispatch states, the worker log lines and the receipt. Until then hosted end-to-end delivery is `NOT VERIFIED`.

## 5. Secret provisioning

All secrets are entered in provider dashboards (`sync: false` in `render.yaml`), never committed. Provision `ORG_KEY_MAP`, `SYSTEM_OPERATOR_API_KEY`, `MAILBOX_ENCRYPTION_KEY`, `DATABASE_URL` and `GROQ_API_KEY` independently; give the API and worker the same `DATABASE_URL` and `MAILBOX_ENCRYPTION_KEY`. Details and rotation: [SECURITY_AND_SECRETS.md](SECURITY_AND_SECRETS.md).

## 6. Rollback and recovery principles

- Code rolls back by redeploying the previous revision. Schema does not roll back automatically: the wrapper never runs downgrades, and migrations are additive and idempotent. Roll code back only to a revision whose schema expectations the current database still satisfies.
- A bad deploy that stops the worker leaves accepted dispatches `QUEUED`; they resume when a worker returns. In-flight sends interrupted by a stop become `UNKNOWN` and need operator reconciliation by `Message-ID` (see [SYSTEM_DESIGN.md](architecture/SYSTEM_DESIGN.md#10-failure-modes-and-operational-recovery)).
- Restoring a database backup also requires the original `MAILBOX_ENCRYPTION_KEY`.
- **Pausing sending.** Do not use `LIVE_SENDING_ENABLED=false` as a queue pause. With it false the worker does not hold dispatches: a claimed dispatch is failed before SMTP with `live_sending_disabled`, so queued work is destroyed and callers must resubmit under new idempotency keys. The flag is also a separate copy on each service. The safe pause is to suspend the **worker** service in its provider: SIGTERM triggers graceful shutdown, which stops new claims, drains in-flight sends for up to `EXTERNAL_DISPATCH_SHUTDOWN_DRAIN_SECONDS` (default 25 s), moves dispatches the process still owns from `SENDING` to `UNKNOWN`, and leaves unsent work `QUEUED`. The API keeps accepting dispatches, which queue durably. Resume by restarting the worker; `UNKNOWN` items are not retried. The provider's stop timeout must exceed the drain budget (`render.yaml` sets `maxShutdownDelaySeconds: 60` for a Blueprint-created worker; the equivalent Railway setting has not been verified). Full procedure: [runbook section 7](C13_DEPLOYMENT_RUNBOOK.md#7-pause--resume-sending).

## 7. Configuration reference

The authoritative table. Scope: **API** = API service only, **Worker** = worker only, **Both** = same value on both, **Legacy** = Mailer-native (non-integration) campaigns. `LIVE_SENDING_ENABLED` and `AUTO_REPLY_ENABLED` are per-process; set them deliberately on each service (the Blueprint sets both false on both).

### Database and secrets

| Variable | Scope | Required service | Safe default | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | Both | PostgreSQL | local SQLite file | Production uses a dedicated Mailer PostgreSQL. Never commit or paste it. |
| `ORG_KEY_MAP` | API | none | none: API refuses to start without a key source | `{"<mailer_tenant_key>":"<mailer_org_id>"}`. Distinct from the operator and encryption keys. |
| `ORG_KEYS_<org>` | API | none | unset | Optional additional key source, merged below `ORG_KEY_MAP`. |
| `API_KEY` | API, Legacy | none | unset | Single legacy key mapping to organization `default`. Do not add it in production dashboards. |
| `SYSTEM_OPERATOR_API_KEY` | API | none | empty: operator routes answer 503 | Must differ from every tenant key. |
| `MAILBOX_ENCRYPTION_KEY` | Both | none | empty: credential operations answer 503; sends stay queued | Fernet key. Same value on both services. Preserve it. |
| `ALLOW_INSECURE_NO_AUTH` | API | none | `false` | Local development only; refused with live sending. |
| `API_DOCS_ENABLED` | API | none | `false` | Enabling serves `/docs`, `/redoc`, `/openapi.json` without authentication. |

### Scheduling and sending

| Variable | Scope | Safe default | Notes |
|---|---|---|---|
| `RUN_SCHEDULER_IN_PROCESS` | API | `true` | Set `false` on the API whenever a separate worker runs. |
| `LIVE_SENDING_ENABLED` | Worker (also read by API for native paths) | `false` | When false, claimed dispatches fail as `live_sending_disabled`. |
| `AUTO_REPLY_ENABLED` | Worker, Legacy | `false` | Native campaigns only; integration contacts get no auto-reply. |
| `EXTERNAL_DISPATCH_POLL_SECONDS` | Worker | `15` | Send and generation job interval. |
| `EXTERNAL_DISPATCH_RECOVERY_POLL_SECONDS` | Worker | `60` | Lease-recovery sweep interval. |
| `EXTERNAL_DISPATCH_MAX_PER_CYCLE` | Worker | `5` | Dispatches handled per job run. |
| `EXTERNAL_DISPATCH_GENERATION_LEASE_SECONDS` | Worker | `300` | Expired generation lease returns the row to `QUEUED`. |
| `EXTERNAL_DISPATCH_LEASE_SECONDS` | Worker | `900` | Expired send lease resolves to `UNKNOWN`. |
| `EXTERNAL_DISPATCH_SHUTDOWN_DRAIN_SECONDS` | Worker | `25.0` | Drain budget on SIGTERM. |
| `IMAP_POLL_SECONDS` | Worker | `120` | Per-mailbox and legacy IMAP poll interval. |
| `IMAP_TIMEOUT_SECONDS` | Worker | `30` | |

### LLM

| Variable | Scope | Safe default | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | Worker | empty | Required for generated requests; without it generation fails and dispatches end `FAILED`. |
| `LLM_MODEL` | Worker | `openai/gpt-oss-120b` | |
| `LLM_FALLBACK_MODELS` | Worker | `openai/gpt-oss-20b` | Comma-separated, tried in order, bounded count. |
| `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `LLM_REASONING_EFFORT` | Worker | `500`, `0.4`, `low` | Reasoning effort is sent only to models that support it. |

### LeadBoost integration

| Variable | Scope | Safe default | Notes |
|---|---|---|---|
| `LEADBOOST_INTEGRATION_SENDER_EMAIL` | API | empty: first dispatch per organization answers 503 | Deployment-wide `From` address for integration mail. |
| `LEADBOOST_INTEGRATION_SENDER_NAME` / `_ORG` | API | `LeadBoost Outreach` / `LeadBoost` | Deployment-wide persona; not per organization. |
| `MAILING_AGENT_BASE_URL`, `MAILING_AGENT_ORG_API_KEYS` | LeadBoost backend | n/a | Set on LeadBoost, never on Mailer. Inverse map of `ORG_KEY_MAP`. |

### Legacy (deployment-wide identity for native campaigns)

`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `FOLLOWUP_POLL_SECONDS`, `DEFAULT_FOLLOWUP_DAYS`, `MAX_SENDS_PER_CYCLE`, `SEND_DELAY_SECONDS`, `SEND_JITTER_SECONDS`. Integration dispatches do not use the global SMTP identity; they send through the organization's mailbox. See `.env.example` for placeholders.

### Switching a service to `SCHEMA_INIT_MODE=verify` (optional, staged, reversible)

1. Deploy the release that contains this change first. The default (`create`) behaves as before, now with the lock.
2. Against the REAL database, read-only, from a trusted workstation: `DATABASE_URL=... python scripts/migrate_preflight.py verify`. It prints only `host:port/database` and the names of any missing tables/columns, and exits 5 if anything is missing. Also run `scripts/migrate_release.sh --status`.
3. Only if both are clean, set `SCHEMA_INIT_MODE=verify` on ONE service (API first), redeploy it, and confirm `/health` and a read-only tenant call. Then the worker, and any other API instance (including the Railway `web` service, if it still exists). `render.yaml` is intentionally unchanged: nothing is flipped by merging.
4. Rollback is unsetting the variable (or setting it to `create`) and redeploying; no data or schema is touched either way.

A service in `verify` mode refuses to start if a table or column is missing, with a message naming it. `verify` is structural only; it does not prove migrations were applied (use `--status`).

## 8. Known deployment gaps

- `render.yaml` has no pre-deploy migration step; migrations are manual (by design).
- The `init_db()` start race: fixed in code and verified on disposable PostgreSQL (`LOCAL VERIFIED`); not re-observed on hosted infrastructure. Deployed services still run `SCHEMA_INIT_MODE=create` (the default) until an operator switches them to `verify`.
- The redundant Railway `web` service is pending confirmation and cleanup.
- Hosted end-to-end delivery has not been demonstrated: the deployed LeadBoost-to-Mailer message delivery is `NOT VERIFIED` unless new dispatch and receipt evidence is recorded.
- Outbound SMTP from the Railway worker is unverified (Limited Trial caveat above).
