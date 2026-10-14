# Mailer Agent

[![CI](https://github.com/anupamkr1708/LeadBoost-mail-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/anupamkr1708/LeadBoost-mail-agent/actions/workflows/ci.yml)

A standalone, intelligent sales-outreach mailer with a REST API in front of it. Give it a sender identity, an offer, and a list of contacts; it writes and sends the first email, waits for replies, drafts and (optionally) sends replies that actually address what the prospect said, and follows up on a schedule *you* configure per campaign — not one baked into the code.

Built to be integrated by URL, not by import: run it as its own service, call its API from LeadBoost (or anything else) to create campaigns and push in leads.

## Why it's built this way

Two design principles run through every file, both learned from auditing weaker versions of this exact idea:

1. **Nothing is hardcoded except the ceiling on how much damage a bug can do.** There's no library of per-industry email templates, no fixed "wait 4 days" follow-up delay, no `if industry == "software"` branches. The offer (`value_prop`), the tone, the follow-up cadence (`follow_up_days`), and every fact the agent is allowed to mention (`context_notes`, `proof_points`) are all data you supply per campaign/contact. One flexible prompt (`mailer_agent/llm/prompts.py`) does the personalization by *reasoning over your data*, not by picking a template bucket.
2. **Nothing sends live email, or auto-sends a reply, until you explicitly turn it on.** `LIVE_SENDING_ENABLED=false` (the default) logs every generated message instead of emailing it, so you can read the agent's actual output before it ever reaches a real inbox. `AUTO_REPLY_ENABLED` is a separate toggle for the same reason on the reply side, and even when it's on, low-confidence and "objection" replies are always held for human approval (`POST /messages/{id}/approve`) regardless.

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              FastAPI app                 │
                    │  /campaigns  /contacts  /messages         │
                    └───────────────┬─────────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
      followup/engine.py    mail/reply_handler.py     llm/agent.py
      (who gets messaged,   (correlates inbound         (decides what
       when -- reads each    email -> thread, runs       to write: LLM-
       campaign's own        intent classification,      first, single
       follow_up_days list)  updates status)             deterministic
              │                      │                    fallback if
              ▼                      ▼                    the LLM is down)
        mail/sender.py        mail/imap_reader.py               │
        (SMTP send with       (IMAP poll, UNSEEN               ▼
         Message-ID/In-        messages, parses          llm/provider.py
         Reply-To/References   headers+body)              (only file that
         threading headers)                                talks to Groq)

  Everything above is orchestrated by two APScheduler jobs
  (mailer_agent/followup/scheduler.py), started from the FastAPI
  lifespan: poll for replies every IMAP_POLL_SECONDS, dispatch
  due sends every FOLLOWUP_POLL_SECONDS.

  Conversation memory isn't a separate store -- it's the Message
  table itself (mailer_agent/models.py). memory/store.py just turns
  "every message for this contact" into a prompt-ready transcript,
  with older messages beyond a window rolled into a short LLM
  summary so long threads don't blow the context budget.
```

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GROQ_API_KEY, SMTP_*, IMAP_* — see below
python run.py           # serves on http://localhost:8000 (interactive docs at /docs only if API_DOCS_ENABLED=true)
```

Run the test suite any time: `pytest -q`.

## CI / Testing

GitHub Actions runs on every PR into `main` and on pushes to `main` and
`grounding-state-fix`.

| Job | What it runs | Command |
|---|---|---|
| `quality-and-tests` | compile/import check, lint (report-only), `pip-audit`/`bandit` (report-only), the full deterministic suite with coverage | `python -m pytest -q --cov=mailer_agent` |
| `postgres-integration` | the PostgreSQL row-locking/concurrency suite, against a real `postgres:16` service container | `python -m pytest -o addopts="" tests/test_postgresql_concurrency.py -v` |
| `local-smtp` | the local SMTP protocol test, against a real `aiosmtpd` server on localhost (no external network) | `python -m pytest -o addopts="" tests/test_smtp_local_integration.py -v` |

None of these jobs touch a real mailbox, real SMTP provider, or the real
Groq API — the LLM is faked (`tests/fake_llm_provider.py`, autouse) and
`LIVE_SENDING_ENABLED`/`AUTO_REPLY_ENABLED` are forced `false` for every job.

**Run the same commands locally:**
```bash
python -m pytest -q                                                   # deterministic suite
POSTGRES_TEST_URL='postgresql+psycopg2://user:pass@localhost:5432/db' \
  python -m pytest -o addopts="" tests/test_postgresql_concurrency.py -v
python -m pytest -o addopts="" tests/test_smtp_local_integration.py -v
```

**Not run in CI — manual only**, because they hit real external providers:
```bash
python -m pytest -m integration -v tests/evaluation/test_live_llm_semantic_quality.py  # real Groq
python -m pytest -m integration -v tests/evaluation/test_live_smoke.py                 # real Groq
./run_e2e_test.sh                                                                       # real Gmail, needs .env
```

### Getting SMTP/IMAP credentials
If using Gmail: enable 2FA, then create an **App Password** (Google Account → Security → App Passwords) — use that as both `SMTP_PASSWORD` and `IMAP_PASSWORD`, with `SMTP_USERNAME`/`IMAP_USERNAME` as the full Gmail address. For anything beyond light testing, use a dedicated domain/mailbox for outreach rather than a personal inbox — see the deliverability notes at the bottom.

### First run — stay in dry-run mode
Leave `LIVE_SENDING_ENABLED=false` and `AUTO_REPLY_ENABLED=false` for your first campaign. Create it, add a couple of contacts, hit `/start`, then read what got generated via `GET /contacts/{id}/thread` before flipping either flag on.

## API reference

Interactive docs (`/docs`, `/redoc`, `/openapi.json`) are **off by default** (all three return 404). Set `API_DOCS_ENABLED=true` to serve them (see `.env.example`); when enabled they are reachable without an API key, so leave them off on a deployed service unless that is intended. Summary:

| Method & path | Purpose |
|---|---|
| `POST /campaigns` | Create a campaign: sender identity, `value_prop`, `proof_points`, `tone`, `follow_up_days` |
| `GET /campaigns` / `GET /campaigns/{id}` | List / fetch a campaign |
| `POST /campaigns/{id}/contacts` | Bulk-add contacts in the service's own exact shape (idempotent by email; auto-skips suppressed addresses) |
| `POST /campaigns/{id}/leads/ingest` | Bulk-add leads in **any** shape — accepts LeadBoost's own `Lead` fields directly (`company_name`, `contact_name`, `contact_title`, `email`, `about_text`, `industry`, `employees`, `revenue_band`, `qualification_label`, `score`, ...) or generic aliases; unrecognized fields are preserved into context rather than dropped. Per-lead failures are reported individually, not batch-failing. |
| `GET /campaigns/{id}/contacts` | List contacts + status for a campaign |
| `POST /campaigns/{id}/start` | Immediately send initial outreach to every `new` contact in the campaign |
| `GET /contacts/{id}` | Fetch one contact's status |
| `GET /contacts/{id}/thread` | Full conversation history (the memory) for a contact |
| `POST /contacts/{id}/pause` / `/resume` | Manually pause/resume the sequence for one contact |
| `POST /contacts/{id}/force-followup` | Bypass the schedule, send the next follow-up now |
| `POST /contacts/{id}/mark-won` | Mark a deal closed-won, stops the sequence |
| `POST /messages/{id}/approve` | Send a `draft` message (dry-run output, or a reply held for approval) |
| `POST /suppress` | Manually add an address to the suppression list |
| `GET /suppress/{email}` | Check suppression status |
| `POST /webhooks/inbound-email` | Production-recommended reply intake — see "Reply monitoring" below |
| `GET /health` | Liveness only: returns `{"status":"ok"}`. Not a readiness check and not a configuration check (it does not report `LIVE_SENDING_ENABLED` / `AUTO_REPLY_ENABLED`) |

Every route above except `GET /health` requires an `X-API-Key` header. Authentication fails closed: with no key configured (`API_KEY`, `ORG_KEY_MAP` or `ORG_KEYS_*`) the API refuses to start, and a malformed `ORG_KEY_MAP` is rejected rather than ignored. Local development without a key requires the explicit `ALLOW_INSECURE_NO_AUTH=true` opt-in (see `.env.example`).

### System endpoints: tenant metrics vs operator diagnostics

`GET /system/health`, `/system/metrics/classification` and `/system/metrics/states` are scoped to the calling key's organisation (`X-API-Key`). `GET /system/config`, `/system/metrics/llm` and `/system/integration/validate` return **process-wide** information, so a tenant key does **not** open them: they require the separate `X-System-Operator-Key` header (`SYSTEM_OPERATOR_API_KEY`, API service only) and answer 503 while it is unset. See `docs/C13_DEPLOYMENT_RUNBOOK.md` section 6 and `.env.example`.

### Example: creating and starting a campaign

```bash
curl -X POST http://localhost:8000/campaigns -H "Content-Type: application/json" -d '{
  "name": "Q3 outbound",
  "sender_name": "Anish Kumar",
  "sender_org": "Your Company",
  "sender_email": "anish@yourcompany.com",
  "reply_to_email": "anish@yourcompany.com",
  "value_prop": "We help B2B SaaS teams cut support response time 40% with an AI triage layer that installs in under a day.",
  "proof_points": "Used by 3 YC-backed startups; average setup time 6 hours.",
  "follow_up_days": [3, 7, 14]
}'

curl -X POST http://localhost:8000/campaigns/1/contacts -H "Content-Type: application/json" -d '{
  "contacts": [
    {"name": "Priya Singh", "email": "priya@example.com", "title": "Head of Support",
     "company": "ExampleCorp", "context_notes": "Posted 2 support-engineer roles last month."}
  ]
}'

curl -X POST http://localhost:8000/campaigns/1/start
```

## How the "sales agent" behavior actually works

`llm/prompts.py` defines one system prompt that tells the model to behave like an experienced SDR: ground everything in the data it's given (never invent facts about the prospect), never use generic filler phrases, vary its opening on every follow-up instead of "just bumping" the thread, end with exactly one concrete ask, and — critically — **push toward a close once the prospect shows interest** instead of continuing to pitch. `llm/agent.py` picks which of four moves to make (`initial_outreach` / `follow_up` / `reply` / `closing`) based on the contact's actual state and, for replies, the classified intent of what they just said — so a prospect who asks about pricing gets a different next message than one who raises an objection or one who says "let's do a call."

## How follow-up cadence works (the "dynamic days" requirement)

Each campaign has `follow_up_days`, a plain list of integers you set at creation (default `[3, 7, 14]`, fully overridable). After the initial send, `followup/engine.py` computes `next_action_at = now + follow_up_days[0]`. Each time a follow-up actually sends, `contact.follow_up_index` increments and the next delay is read from the next position in the list; once the list is exhausted the contact moves to `sequence_complete` — no more follow-ups until a human re-engages it. Change the cadence for a whole campaign by creating it with a different list; nothing about timing lives in code.

## How memory works

Every inbound and outbound message is a row in `Message`, linked to its `Contact`. `memory/store.py` builds the transcript handed to the LLM on every draft: the last 6 messages verbatim, plus (once a thread passes 8 messages) a short LLM-generated summary of everything older, so the model always has the real recent exchange in full and the gist of everything before it — this is what lets a follow-up or reply actually reference what was said earlier instead of restarting the conversation from zero.

## How reply-handling works

`mail/imap_reader.py` polls the configured inbox for `UNSEEN` mail on a timer, parses each message (headers + plain-text body), and hands it to `mail/reply_handler.py`, which:
1. Correlates it to a `Contact` via `In-Reply-To`/`References` matching a `Message-ID` we previously sent (falls back to matching the sender's address if headers were stripped by their mail client).
2. Classifies intent — `interested` / `question` / `objection` / `not_interested` / `unsubscribe` / `out_of_office` / `neutral` — via the LLM (with a keyword-based fallback for `unsubscribe` specifically, so opt-outs are never missed even if the LLM is down).
3. Acts on it: `unsubscribe` → added to the suppression list, sequence stopped, permanently. `not_interested` → marked `closed_lost`. `out_of_office` → ignored, no status change. Everything else → a reply is drafted; it auto-sends only if `AUTO_REPLY_ENABLED=true`, the intent isn't `objection`, and classifier confidence is ≥ 0.6 — otherwise it's saved as a `draft` awaiting `POST /messages/{id}/approve`.

### Per-Mailbox inbound (M3)

Mailer Mailboxes (the integrated LeadBoost path) are polled by `mail/mailbox_inbound.py`, **each with its own IMAP host/port/username and encrypted credential** (set with `POST /mailboxes`, or later with `PATCH /mailboxes/{ref}` using all four `imap_*` fields together; `imap_password` alone rotates). The tenant is `Mailbox.organization_id`, bound before any message content is read — never derived from `From`/`To`/`Subject`/`Message-ID`. Per message: `UID FETCH BODY.PEEK[]` → parse → database commit → `UID STORE \Seen`; a failure before the commit leaves the message unseen so the next poll retries it, and storage is idempotent on `(mailbox, Message-ID)` (`synthetic:<sha256 of the raw message>` when a message has none). Replies are matched only inside the mailbox's organization by `In-Reply-To`/`References` and then by an unambiguous sender address; an unmatched or ambiguous message is not guessed — it is logged (metadata only), not stored, and marked Seen. Disabled or incompletely configured mailboxes are never polled, and one mailbox's failure never stops another. The deployment-global `IMAP_*` settings above remain the legacy path for non-integrated campaigns and are never a fallback for Mailbox polling; don't configure the same physical account both ways.

### Polling vs. webhook — which to use

IMAP polling (the default) needs zero setup and is fine for development or low volume, but it has two real production drawbacks: polling latency of 1-15+ minutes depending on interval, and — specifically for Gmail — automated IMAP access on a personal-style account risks the account being flagged for "suspicious automated activity" and suspended, which takes your reply monitoring offline with it. For production, point a transactional email provider's inbound-parse webhook (Postmark Inbound, Mailgun Routes, SendGrid Inbound Parse) at `POST /webhooks/inbound-email` instead — replies arrive in seconds, and there's no personal inbox being polled at all. The endpoint expects this normalized JSON shape:
```json
{"from_email": "...", "subject": "...", "body_text": "...", "message_id": "<...>", "in_reply_to": "<...>", "references": ["<...>"]}
```
Map your provider's native webhook payload to this shape using its own payload-template/transform feature (Postmark and Mailgun both support this natively), then keep IMAP polling running alongside it if you like — processing is idempotent by `Message-ID`, so the same reply arriving through both paths is harmless.

## Deploying to Render

**Nothing is deployed from this repository by itself.** `render.yaml` is a Render Blueprint for a **first production deployment** with two services and **no database**:

| Resource | Role |
|---|---|
| `mailer-agent-api` (web service) | The HTTP API. Does not run the scheduler (`RUN_SCHEDULER_IN_PROCESS=false`). `/docs`, `/redoc` and `/openapi.json` are off (`API_DOCS_ENABLED=false`). |
| `mailer-agent-worker` (background worker) | The scheduler and dispatcher. **Exactly one** instance: the scheduler must run once, not once per API process. |

The database is a **dedicated Supabase project/PostgreSQL owned by the Mailer** (not LeadBoost's database, and not a Render-managed database). Its connection string is supplied out of band as `DATABASE_URL` on **both** services, with the **exact same value**; it is never committed. Both services also start with `LIVE_SENDING_ENABLED=false` and `AUTO_REPLY_ENABLED=false`, and stay that way until a separate, controlled acceptance step.

The full, ordered first-deployment procedure (Supabase connection guidance, credential provisioning, the guarded database migration, verification, linking LeadBoost) is `docs/C13_DEPLOYMENT_RUNBOOK.md` section 0. In outline:

1. Create the dedicated Mailer Supabase project and copy the connection string from **its own Connect screen**. Prefer the direct PostgreSQL connection; if Render cannot reach it, assess the shared session pooler (port 5432). Do not default to the transaction pooler (port 6543).
2. In Render: **New -> Blueprint**, point it at this repository at the reviewed commit. Render creates the two services; you type the `sync: false` values in the dashboard (the same `DATABASE_URL` and `MAILBOX_ENCRYPTION_KEY` on both; `ORG_KEY_MAP` and `SYSTEM_OPERATOR_API_KEY` on the API only; see below).
3. Back up / record recovery evidence, run `scripts/migrate_release.sh --status` and `--check-only`, then apply the guarded migration with the required acknowledgement. `init_db()` (which the services run at start) only creates missing tables; it is **not** the canonical migration sequence.
4. Provision tenant keys and mailboxes **before** pointing LeadBoost at the Mailer.

The services use Render's paid **Starter** plan (`render.yaml`): the free tier spins web services down after 15 minutes and does not offer background workers. If you would rather not use the Blueprint, `Procfile` defines the same two process types (`web` / `worker`) for platforms that read Procfiles directly.

### Credentials for a deployed service

None of these values belong in the repository, a PR, a ticket, chat or a log. Generate them locally and set them only in the provider dashboard.

* **Tenant API keys (`ORG_KEY_MAP`, API only, required).** A JSON object `{"<mailer_tenant_api_key>": "<mailer_org_id>"}`, one key per tenant. Authentication fails closed: with no key source the API refuses to start. The legacy global `API_KEY` is **not** part of this deployment profile (it is accepted as organization `default` on every route, including the integration routes), so do not set it.
* **Operator key (`SYSTEM_OPERATOR_API_KEY`, API only).** A separate secret, sent as `X-System-Operator-Key`, for the process-wide `/system/config`, `/system/metrics/llm` and `/system/integration/validate` routes. It is **not** a tenant key, must differ from every tenant key (otherwise the API refuses to start), and must never be given to LeadBoost.
* **Mailbox encryption key (`MAILBOX_ENCRYPTION_KEY`, API and worker).** One Fernet key, the **identical** value on both services, stable forever; keep an offline copy separate from database backups. Losing or changing it makes stored mailbox credentials unreadable.
* **Sender identity (`LEADBOOST_INTEGRATION_SENDER_EMAIL`, API).** Required for LeadBoost-generated outreach; while it is empty the first dispatch per organization returns 503.

Generate a random key (tenant or operator) with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# or
openssl rand -hex 32
```
and an encryption key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Use a different value for every key, and a different set per environment. Keys are compared in constant time. Rotate one the way you would rotate any exposed secret (see `docs/C13_DEPLOYMENT_RUNBOOK.md`).

## Using this from LeadBoost

LeadBoost already contains the client for this service; follow it as implemented. **Do not write a second client**, and do not use the old campaign-based flow (`POST /campaigns`, `/campaigns/{id}/leads/ingest`) for the LeadBoost integration. The code is `core/infrastructure/mailing_agent/` in the LeadBoost backend (`client.py`, `mailbox_client.py`, `conversation_client.py`, and its `CONTRACT.md`).

### Configuration

LeadBoost (deployed on Railway) reads exactly two variables:

| LeadBoost variable | Meaning |
|---|---|
| `MAILING_AGENT_BASE_URL` | The Mailer API base URL: `https://<mailer-api-host>` (the real deployed HTTPS URL; there is no verified production hostname yet, so use your own placeholder until it exists). It must be `https://`, or `http://` to a loopback host only. Redirects are not followed. |
| `MAILING_AGENT_ORG_API_KEYS` | JSON `{"<leadboost_org_id>": "<mailer_tenant_api_key>"}`: one Mailer key per LeadBoost organization. No key for an organization means LeadBoost fails closed for it (`mailing_agent_auth_not_configured`). |

The Mailer holds the **inverse** mapping in `ORG_KEY_MAP`: `{"<mailer_tenant_api_key>": "<mailer_org_id>"}`. The two mappings must agree: the key LeadBoost sends for an organization must map, on the Mailer, to the Mailer organization that owns that organization's mailbox. The Mailer derives the organization **from the key only**; LeadBoost never sends an organization id in a request body.

These calls are **server-to-server only**. Neither the Mailer URL nor any key belongs in a public frontend configuration; the browser talks only to LeadBoost. A Mailer tenant key is **not** `SYSTEM_OPERATOR_API_KEY`, and the operator key is never used by LeadBoost. The legacy `MAILING_AGENT_API_KEY` is no longer read, and a single shared key is not supported.

### What LeadBoost calls

All requests carry `X-API-Key: <tenant key>`.

| Call | Purpose |
|---|---|
| `POST /mailboxes` | Create the organization's mailbox (always `ACTIVE`). Body: `email_address`, `smtp_host`, `smtp_port`, `smtp_use_tls` (always `true`: STARTTLS only; no implicit-TLS path), `smtp_username`, `smtp_password`. `409` if `(organization, email_address)` already exists. |
| `GET /mailboxes` | List the organization's mailboxes (adopt an existing one after a `409` or a lost response). |
| `PATCH /mailboxes/{reference}` | Disable (`{"status":"disabled"}`) or atomically re-activate with new transport settings and credential. |
| `POST /integrations/leadboost/outreach-requests` | Submit one authorized outreach action. Strict body: `external_action_id`, `idempotency_key`, optional `correlation_id`, `recipient` (`email`, optional `name`/`title`/`company`) and `context` (`value_proposition`, `recipient_facts`). Answers `202` with `accepted` and a reference. Nothing is generated or sent synchronously. |
| `GET /integrations/leadboost/outreach-actions/{idempotency_key}/conversation?limit=N` | Read-only delivery state and conversation (the key is percent-encoded). Exposed to users only through LeadBoost's own endpoint. |

Per-mailbox SMTP (and optional IMAP) credentials are encrypted in Mailer storage with `MAILBOX_ENCRYPTION_KEY` and are write-only: they never appear in responses. LeadBoost sends SMTP credentials only through mailbox provisioning, never on dispatch, and does not send IMAP fields (inbound is configured through the Mailer API with the organization's key; `PATCH /mailboxes/{reference}` accepts an all-or-none IMAP set). The process-level `SMTP_*` / `IMAP_*` variables are for the **legacy** non-integrated path only. They are deliberately not declared in the new production Render Blueprint; add them manually to the relevant service only if that legacy path is deliberately enabled. The integrated flow never reads them.

### Sending readiness

* **Exactly one `ACTIVE` mailbox per organization.** With none, or with more than one, the outreach request answers `409` before any row is created, and LeadBoost reports `mailing_agent_conflict`. A `409` also covers an idempotency key reused for a different operation and a recipient with an active follow-up.
* LeadBoost only provisions a mailbox as `ACTIVE` when its own email account is active **and** verified, and only dispatches once its sync state is `synced`.
* `LEADBOOST_INTEGRATION_SENDER_EMAIL` must be set on the Mailer API; while it is empty, the first outreach request for an organization answers `503`.
* The Mailer **worker** must be running with `MAILBOX_ENCRYPTION_KEY` and `LIVE_SENDING_ENABLED=true` for anything to leave: with `LIVE_SENDING_ENABLED=false` queued dispatches are failed pre-SMTP rather than held (see `docs/C13_DEPLOYMENT_RUNBOOK.md` section 7). A new deployment keeps it `false` until the controlled acceptance step.
* Delivery is idempotent. An outcome the Mailer cannot prove stays `UNKNOWN` and is never resent automatically.

Full wire contracts: `docs/LEADBOOST_L1_MAILBOX_CONTRACT.md` (this repository) and `core/infrastructure/mailing_agent/CONTRACT.md` (LeadBoost).

## Deliverability & compliance — do this before any real-volume sending

This document intentionally doesn't re-derive cold-email deliverability basics — they don't change based on which script sends the mail. In short: use a dedicated sending domain (not a personal inbox) with SPF/DKIM/DMARC configured, warm up a new mailbox for 2-3 weeks before real volume, and keep `MAX_SENDS_PER_CYCLE` low until you trust the pipeline. The suppression list here (`SuppressionEntry` / `POST /suppress`) is enforced on every send path in the codebase (initial, follow-up, and reply) — never bypass it, and never re-add an address that unsubscribed.

## What's intentionally out of scope for v1

- **Domain verification and per-tenant campaign sender identity** — the legacy (non-integrated) campaign path assumes one mailbox per campaign, configured via env vars. The integrated LeadBoost path has per-organization mailboxes with encrypted credentials (see "Using this from LeadBoost"), but the campaign sender identity for generated outreach is still deployment-wide (`LEADBOOST_INTEGRATION_SENDER_EMAIL`), and sending-domain verification is not built.
- **Provider bounce webhooks** — the inbound-email webhook (above) covers replies; hard bounces from providers that don't deliver a bounce-notification email back to the monitored inbox in a parseable way aren't wired to `SuppressionEntry` yet. If you move off raw SMTP to a transactional provider (SES/Resend/Mailgun), wire their bounce webhook into a new `POST /webhooks/bounce` next — same pattern as `webhooks/inbound-email`.
- **A/B testing / analytics dashboard** — every send/reply is logged with enough detail (`message_type`, `detected_intent`, `source` in the drafting result) to build this on top; it's not wired into a reporting endpoint yet.
