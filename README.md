# Mailer Agent

[![CI](https://github.com/anupamkr1708/LeadBoost-mail-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/anupamkr1708/LeadBoost-mail-agent/actions/workflows/ci.yml)

Mailer Agent is a multi-tenant Python email-delivery service. It accepts authorized outreach requests over an HTTPS API, stores each one as a durable dispatch row in PostgreSQL, and delivers it from a separate worker through the tenant's own SMTP mailbox. Replies are read back over IMAP (or an optional webhook), and delivery state is reported through a read-only API.

It exists so that a crashed worker, a replayed request or an ambiguous SMTP connection is handled by explicit rules (organization-scoped idempotency, leased and fenced claims, a pre-SMTP `Message-ID`, and an `UNKNOWN` state that is never retried automatically) instead of by silent retry. These rules reduce the chance of duplicate or lost mail; they do not provide exactly-once delivery. See [delivery guarantees and their limits](docs/architecture/SYSTEM_DESIGN.md#delivery-guarantees-and-their-limits).

Mailer is the email-delivery side of the LeadBoost product. LeadBoost owns discovery, scraping, lead scoring and the decision to authorize an outreach action; Mailer owns tenant authentication, mailbox configuration, durable dispatches, generation and sending, SMTP delivery, inbound reply processing and delivery-state reporting.

> **Validation status.** The code, CI-gated tests and the local cross-system workflow are described in [docs/FINAL_PRODUCTION_READINESS.md](docs/FINAL_PRODUCTION_READINESS.md), which labels every claim as `CI VERIFIED`, `LOCAL E2E VERIFIED` (operator-reported), `HOSTED OBSERVED`, `IMPLEMENTED, NOT HOST-VERIFIED` or `NOT VERIFIED`. Hosted end-to-end delivery on the deployed API and worker is **not yet verified**.

## Capabilities

| Area | What the code does | Detail |
|---|---|---|
| Tenant authentication | `X-API-Key` resolves to exactly one organization (`ORG_KEY_MAP`); the organization is never read from a request body. Fails closed with 503 when no keys are configured. | [Security](docs/SECURITY_AND_SECRETS.md) |
| Mailer-owned mailboxes | `POST/GET/PATCH /mailboxes`; SMTP and IMAP passwords are write-only and stored as Fernet ciphertext. | [L1 contract](docs/LEADBOOST_L1_MAILBOX_CONTRACT.md) |
| Durable dispatch | An accepted request is a committed `external_dispatches` row, unique per `(organization_id, idempotency_key)`. | [System design](docs/architecture/SYSTEM_DESIGN.md) |
| Idempotent intake | Replaying a key returns the existing operation; the same key with a different fingerprint is 409. | [API](docs/API_AND_INTEGRATION.md) |
| Claims, leases, fencing | `FOR UPDATE SKIP LOCKED` claims, lease expiry sweeps, and outcome writes fenced on the exact claim. | [State machine](docs/architecture/diagrams/dispatch-state-machine.svg) |
| Message-ID persistence | The `Message-ID` is committed before SMTP is attempted and reused on the wire. | [System design](docs/architecture/SYSTEM_DESIGN.md) |
| SMTP outcomes | `SENT`, `FAILED` or `UNKNOWN`; ambiguous sends are never retried automatically. | [System design](docs/architecture/SYSTEM_DESIGN.md) |
| Inbound replies | Per-mailbox IMAP polling and an optional authenticated webhook; stored once per `(mailbox, Message-ID)`. | [System design](docs/architecture/SYSTEM_DESIGN.md) |
| Reply handling | Replies are classified and stored. Contacts owned by the LeadBoost integration get no Mailer-native follow-ups or auto-replies; LeadBoost authorizes every send. | [System design](docs/architecture/SYSTEM_DESIGN.md) |
| Operator diagnostics | `/system/*` routes; operator routes use a separate credential and answer 503 when it is unset. | [Security](docs/SECURITY_AND_SECRETS.md) |
| Release safeguards | A guarded, operator-run migration wrapper; migrations never run at startup. | [migrations/README.md](migrations/README.md) |
| Synthetic end-to-end test | CI runs the chain against disposable Dovecot, a local SMTP sink and PostgreSQL. | [Testing](docs/TESTING.md) |

## Architecture

![Mailer Agent system context](docs/architecture/diagrams/mailer-system-context.svg)

LeadBoost's backend calls the Mailer API server-to-server. The API validates the request, commits a durable dispatch and answers `202`. It never calls the worker. The worker is a separate process that claims dispatches from the database, drafts the message, sends it through the mailbox's SMTP server and writes the outcome back. PostgreSQL is the only channel between the two.

Full documentation: [system design](docs/architecture/SYSTEM_DESIGN.md), [runtime sequence](docs/architecture/diagrams/mailer-runtime-sequence.svg), [dispatch state machine](docs/architecture/diagrams/dispatch-state-machine.svg), [API and integration](docs/API_AND_INTEGRATION.md), [security and secrets](docs/SECURITY_AND_SECRETS.md), [migrations](migrations/README.md), [testing](docs/TESTING.md), [deployment](docs/DEPLOYMENT.md), [deployment runbook](docs/C13_DEPLOYMENT_RUNBOOK.md), [readiness report](docs/FINAL_PRODUCTION_READINESS.md). The full index is [docs/README.md](docs/README.md).

## How a request flows

1. LeadBoost approves an outreach action and its backend calls `POST /integrations/leadboost/outreach-requests` with that organization's key.
2. Mailer resolves the organization from the key, validates the strict request body and requires exactly one ACTIVE mailbox for that organization.
3. The API commits one `external_dispatches` row (`QUEUED`, no message yet) and returns `202`.
4. The worker claims the row for generation, drafts the message with the LLM provider and grounding check, and records the generated `Message`.
5. The worker claims it again for sending. In one transaction it re-checks tenant, mailbox, suppression and grounding gates, decrypts the SMTP credential and commits `SENDING` together with the `Message-ID`.
6. The SMTP call happens with no database transaction open.
7. A fenced transaction records `SENT`, `FAILED` or `UNKNOWN`.
8. LeadBoost reads the state with `GET /integrations/leadboost/outreach-actions/{key}/conversation`.
9. Replies arrive through the mailbox's IMAP poll or the webhook and are bound to that mailbox's organization.

These are different events and are never collapsed into "sent": API acceptance, worker claim, SMTP attempt, SMTP acceptance and recipient receipt. `SENT` means the SMTP server accepted the message; Mailer cannot observe receipt.

## Quick start

Requires Python 3.12 (`.python-version`).

```bash
python3.12 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                 # then set the values below
```

Minimum local `.env` (placeholders; never commit real values):

```bash
API_KEY=<random-local-key>           # or ALLOW_INSECURE_NO_AUTH=true, local development only
LIVE_SENDING_ENABLED=false           # keep false until you mean to send
```

Run everything in one process (API plus scheduler, the default `RUN_SCHEDULER_IN_PROCESS=true`):

```bash
python run.py                        # http://localhost:8000/health
```

Run the split topology used in production, as two processes:

```bash
RUN_SCHEDULER_IN_PROCESS=false python run.py    # API only
python worker.py                                 # the one scheduler
```

Run exactly one scheduler. Do not start `python worker.py` while the API still has `RUN_SCHEDULER_IN_PROCESS=true`.

Tests:

```bash
python -m pytest -q tests/test_pr2_api_surface.py   # one targeted file
python -m pytest -q                                 # deterministic suite (SQLite, no external services)
```

PostgreSQL, disposable SMTP/IMAP and synthetic end-to-end tests need a disposable database or services and are excluded by default (`addopts = -m "not integration"`). Live Groq, real-mailbox SMTP and IMAP checks need credentials and are never part of the default run. Commands and requirements are in [docs/TESTING.md](docs/TESTING.md).

## Configuration

The authoritative variable table, grouped by service, is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#7-configuration-reference). The ones that matter first:

| Variable | Used by | Safe default | Note |
|---|---|---|---|
| `DATABASE_URL` | API, worker | local SQLite file | Production uses PostgreSQL; never commit it. |
| `ORG_KEY_MAP` | API | none: API refuses to start without a key source | `{"<mailer_tenant_key>":"<mailer_org_id>"}` |
| `SYSTEM_OPERATOR_API_KEY` | API | empty: operator routes answer 503 | Distinct from every tenant key. |
| `MAILBOX_ENCRYPTION_KEY` | API, worker | empty: credential operations answer 503 | Fernet key; losing it makes stored credentials unreadable. |
| `LIVE_SENDING_ENABLED` | worker | `false` | When false a claimed dispatch fails as `live_sending_disabled`; nothing is sent. |
| `AUTO_REPLY_ENABLED` | worker | `false` | Native, non-integration campaigns only. |
| `RUN_SCHEDULER_IN_PROCESS` | API | `true` | Set `false` on the API when a separate worker runs. |
| `GROQ_API_KEY`, `LLM_MODEL` | worker | empty / `openai/gpt-oss-120b` | Draft generation. |
| `LEADBOOST_INTEGRATION_SENDER_EMAIL` | API | empty: first dispatch for an organization answers 503 | Deployment-wide sender persona. |

Tenant keys, the operator key and the mailbox-encryption key are three different secrets. LeadBoost holds the inverse mapping (`MAILING_AGENT_ORG_API_KEYS`: `{"<leadboost_org_id>":"<mailer_tenant_key>"}`) on its backend only.

## API surface

| Responsibility | Method and path | Auth |
|---|---|---|
| Liveness only (no database or worker probe) | `GET /health` | none |
| Mailboxes | `POST /mailboxes`, `GET /mailboxes`, `GET /mailboxes/{public_reference}`, `PATCH /mailboxes/{public_reference}` | tenant key |
| Outreach request (Mailer generates the text) | `POST /integrations/leadboost/outreach-requests` | tenant key |
| Outreach action (caller supplies the exact text) | `POST /integrations/leadboost/outreach-actions` | tenant key |
| Dispatch state | `GET /integrations/leadboost/outreach-actions/{idempotency_key}` | tenant key |
| State plus conversation | `GET /integrations/leadboost/outreach-actions/{idempotency_key}/conversation` | tenant key |
| Inbound email webhook | `POST /webhooks/inbound-email` | tenant key |
| Organization-scoped diagnostics (database-backed) | `GET /system/health`, `/system/metrics/classification`, `/system/metrics/states` | tenant key |
| Operator diagnostics | `GET /system/config`, `/system/metrics/llm`, `/system/integration/validate`, `/system/metrics/dispatch`, `/system/metrics/worker`, `/system/metrics/inbound` (see [operator observability](docs/OPERATOR_OBSERVABILITY.md)) | `X-System-Operator-Key` |

Mailer is called server-to-server. Mailer keys, database URLs, mailbox passwords and the key mapping must never enter a frontend bundle or a `NEXT_PUBLIC_*` variable. Details: [docs/API_AND_INTEGRATION.md](docs/API_AND_INTEGRATION.md).

## Security, reliability and limitations

- **Tenant isolation.** The organization comes only from the presented key; unknown and foreign resources return 404 rather than 403.
- **Secrets.** Three distinct secrets; the API refuses to start if the operator key equals a tenant key. Mailbox passwords are never returned and never logged.
- **Fernet.** Stored credentials are decryptable only with the same `MAILBOX_ENCRYPTION_KEY`; back it up separately from the database.
- **Idempotency.** `(organization_id, idempotency_key)` is a database unique constraint, so replays and concurrent duplicates converge on one dispatch.
- **Durability and claims.** The queue is a database table. Claims use `SKIP LOCKED` with leases; every outcome write is fenced on the exact claim.
- **Message-ID.** Committed before SMTP, so the wire message and the stored record agree and an operator can find an ambiguous send by id.
- **`UNKNOWN`.** An ambiguous send (an error during `sendmail()` after the message may have been transferred, lease expiry, drain deadline) becomes `UNKNOWN` and is never re-queued. A blind retry could deliver the email twice; the cost is that a send which did not happen stays unsent until an operator reconciles it by `Message-ID`.
- **Separation.** On the LeadBoost integration path the API never calls an LLM or SMTP; it only commits the dispatch. The worker serves no HTTP.
- **Migrations.** A separate, operator-run release step; never part of startup or every deploy.

Known limitations (`KNOWN LIMITATION`):

- One deployment-wide sender persona (`LEADBOOST_INTEGRATION_SENDER_EMAIL`) for every organization's integration mail; the SMTP identity itself is per mailbox.
- Bounce handling coverage is deferred.
- There is no Prometheus-style `/metrics` route (the deployed API answers 404 there). Telemetry is under `/system/metrics/*`.
- The worker has no HTTP health endpoint; observe it through its logs.
- At API and worker start `prepare_schema()` runs. In the default `SCHEMA_INIT_MODE=create` it calls `init_db()` (create missing tables only, never alters), now serialized across processes by a PostgreSQL advisory lock; two processes starting together against an empty database raced once on hosted infrastructure and the race is reproduced and fixed locally (see `docs/DEPLOYMENT.md`). A deployed service should run `SCHEMA_INIT_MODE=verify`: no DDL, it only checks that the tables and columns exist, and `scripts/migrate_release.sh` is the only thing that changes the schema.

## Testing and deployment

| Layer | What it proves |
|---|---|
| Deterministic suite (SQLite, fakes) | Contract, state-machine logic, tenant boundaries, error handling |
| PostgreSQL suite | Row locking, `SKIP LOCKED` claims, unique constraints and the migration wrapper on a real database |
| Disposable SMTP | The MIME wire format and the `SENT` / `FAILED` / `UNKNOWN` classification over a real socket |
| Disposable Dovecot IMAP and C12 chain | Per-mailbox inbound handling and the generation, send and reply chain with a local sink |
| Operator-reported local end-to-end | The LeadBoost-plus-Mailer workflow worked on the operator's machine; not an automated result |
| Hosted acceptance | Not yet performed end to end |

Deployment: `render.yaml` defines the reference all-Render topology; the currently observed deployment is split across Render (API), Railway (worker) and Supabase (database). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), [docs/C13_DEPLOYMENT_RUNBOOK.md](docs/C13_DEPLOYMENT_RUNBOOK.md) and [docs/FINAL_PRODUCTION_READINESS.md](docs/FINAL_PRODUCTION_READINESS.md).
