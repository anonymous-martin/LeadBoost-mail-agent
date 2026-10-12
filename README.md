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
python run.py           # serves on http://localhost:8000, docs at /docs
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

Full interactive docs are auto-served at `/docs` (Swagger) once running. Summary:

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
| `GET /health` | Liveness + current safety-toggle state |

Every route above except `GET /health` requires an `X-API-Key` header. Authentication fails closed: with no key configured (`API_KEY`, `ORG_KEY_MAP` or `ORG_KEYS_*`) the API refuses to start, and a malformed `ORG_KEY_MAP` is rejected rather than ignored. Local development without a key requires the explicit `ALLOW_INSECURE_NO_AUTH=true` opt-in (see `.env.example`).

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

This repo ships a `render.yaml` Blueprint that provisions everything together: a Postgres database, a Web Service (the API), and a Background Worker (the scheduler — reply polling + follow-up dispatch). They're kept as two separate Render services deliberately: the API can run multiple `gunicorn` worker processes for throughput, but the scheduler must run exactly once, so it lives in its own single process instead of inside every API worker (which would otherwise send every follow-up multiple times).

1. Push this repo to GitHub/GitLab.
2. In the Render dashboard: **New → Blueprint**, point it at the repo. Render reads `render.yaml` and provisions the database, web service, and background worker together, with `DATABASE_URL` already wired from the database to both services.
3. Both services default to Render's paid **Starter** plan ($7/mo each, ~$13-20/mo total with the database) rather than free tier — the free tier spins down web services after 15 minutes of inactivity (30-60s cold start on the next request) and deletes free Postgres databases after about 30 days, neither of which is workable for something that needs to receive webhooks and run scheduled jobs continuously.
4. Fill in the secrets Render left blank (`sync: false` in `render.yaml` means "set this in the dashboard, don't commit it"): `GROQ_API_KEY`, `SMTP_USERNAME`/`SMTP_PASSWORD`, `IMAP_USERNAME`/`IMAP_PASSWORD`, and `API_KEY` (see below) — set each on **both** the web service and the worker.
5. Leave `LIVE_SENDING_ENABLED=false` for your first deploy, create a test campaign against the live URL, and read what it drafted before flipping it to `true`.

If you'd rather not use the Blueprint: `Procfile` defines the same two process types (`web` / `worker`) for platforms that read Procfiles directly.

### Generating the API key

The service accepts any string you set as `API_KEY` — generate a proper random secret rather than a memorable phrase:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# or
openssl rand -hex 32
```
Set the output as `API_KEY` in Render's environment variables for both services. Every request to this API then requires the header `X-API-Key: <that value>` (comparison is constant-time, so it can't be brute-forced via response-timing). Treat it like any other secret: don't commit it, use a different key per environment (dev/staging/prod), and regenerate it if it's ever exposed the same way you'd rotate an OpenAI/Groq key.

## Using this from LeadBoost

Once deployed, this is just another external API — configure it in LeadBoost's `.env` exactly like you would `OPENAI_API_KEY` or `GROQ_API_KEY`:
```bash
# LeadBoost's .env
MAILER_AGENT_BASE_URL=https://mailer-agent-api.onrender.com
MAILER_AGENT_API_KEY=<the value you generated above>
```
Then, in LeadBoost's backend, add a thin HTTP client instead of importing anything from this repo:
```python
# core/infrastructure/messaging/mailer_agent_client.py  (new file in LeadBoost)
import os
import httpx

BASE_URL = os.getenv("MAILER_AGENT_BASE_URL")
API_KEY = os.getenv("MAILER_AGENT_API_KEY")
HEADERS = {"X-API-Key": API_KEY}

def ensure_campaign_for_org(organization) -> int:
    """Create (once) or reuse a Mailer Agent campaign_id for this LeadBoost org."""
    # Store the returned id on Organization (a new nullable column,
    # e.g. mailer_agent_campaign_id) the first time this runs for an org.
    resp = httpx.post(f"{BASE_URL}/campaigns", headers=HEADERS, json={
        "name": f"{organization.name} outreach",
        "sender_name": organization.sender_name,       # whatever LeadBoost already
        "sender_org": organization.name,                 # collects for this org
        "sender_email": organization.sender_email,
        "value_prop": organization.value_prop,
        "follow_up_days": [3, 7, 14],
    })
    resp.raise_for_status()
    return resp.json()["id"]

def send_lead_to_mailer_agent(campaign_id: int, lead) -> dict:
    """Push one LeadBoost Lead row as-is -- the ingestion endpoint
    understands its field names directly, no mapping needed."""
    resp = httpx.post(
        f"{BASE_URL}/campaigns/{campaign_id}/leads/ingest",
        headers=HEADERS,
        json={"leads": [{
            "company_name": lead.company_name, "website": lead.website,
            "industry": lead.industry, "about_text": lead.about_text,
            "contact_name": lead.contact_name, "contact_title": lead.contact_title,
            "email": lead.email, "employees": lead.employees,
            "revenue_band": lead.revenue_band, "qualification_label": lead.qualification_label,
            "score": lead.score,
        }]},
    )
    resp.raise_for_status()
    return resp.json()
```
Call `send_lead_to_mailer_agent(...)` from `graph_nodes.py`'s `message_generation` stage (the exact spot that currently calls `MessagingAgent.run()`) — the practical effect is LeadBoost stops generating its own `outreach_message` text and instead hands the raw, real lead facts to this service, which does the drafting *and* the sending *and* the follow-up/reply handling that LeadBoost never had.

### What to delete/deprecate in LeadBoost once this is wired in
- `application/agents/messaging_agent.py`'s LLM-drafting call — replaced by the send above (you can keep the file as a thin wrapper around `send_lead_to_mailer_agent`, or remove it and call the client directly from `graph_nodes.py`).
- `core/infrastructure/messaging/messenger.py` — no longer needed; sending now happens in this separate service.
- The `mailto:` button in `outreach-card.tsx` — replace with a status readout of what this service's `/contacts/{id}/thread` reports (sent / awaiting reply / replied), since sending is no longer something the LeadBoost user does by hand.
- `Lead.outreach_message` — can stay as a historical/read-only field, or be dropped in a later migration once you're confident nothing still reads it.



## Deliverability & compliance — do this before any real-volume sending

This document intentionally doesn't re-derive cold-email deliverability basics — they don't change based on which script sends the mail. In short: use a dedicated sending domain (not a personal inbox) with SPF/DKIM/DMARC configured, warm up a new mailbox for 2-3 weeks before real volume, and keep `MAX_SENDS_PER_CYCLE` low until you trust the pipeline. The suppression list here (`SuppressionEntry` / `POST /suppress`) is enforced on every send path in the codebase (initial, follow-up, and reply) — never bypass it, and never re-add an address that unsubscribed.

## What's intentionally out of scope for v1

- **Multi-tenant sending identities** — this service assumes one mailbox per campaign, configured via env vars. If LeadBoost needs *each of its own customers* to send from their own verified domain, that's a real feature (per-org SMTP credentials + domain verification), not a config tweak — worth a follow-up build once the core agent behavior is validated.
- **Provider bounce webhooks** — the inbound-email webhook (above) covers replies; hard bounces from providers that don't deliver a bounce-notification email back to the monitored inbox in a parseable way aren't wired to `SuppressionEntry` yet. If you move off raw SMTP to a transactional provider (SES/Resend/Mailgun), wire their bounce webhook into a new `POST /webhooks/bounce` next — same pattern as `webhooks/inbound-email`.
- **A/B testing / analytics dashboard** — every send/reply is logged with enough detail (`message_type`, `detected_intent`, `source` in the drafting result) to build this on top; it's not wired into a reporting endpoint yet.
