# API and LeadBoost integration

This is the practical guide to calling Mailer from LeadBoost. It explains the workflow and the rules that are easy to get wrong. Mailbox request/response schemas live in the formal [L1 mailbox contract](LEADBOOST_L1_MAILBOX_CONTRACT.md) and are not repeated here. For the machine-readable route list, set `API_DOCS_ENABLED=true` on a non-production API and read `/openapi.json`; it is off by default.

Evidence labels used below (`IMPLEMENTED, NOT HOST-VERIFIED` and others) are defined in the [readiness report](FINAL_PRODUCTION_READINESS.md).

## 1. Who calls whom

Mailer is called **server-to-server** from the LeadBoost backend over HTTPS. The browser never talks to Mailer. Mailer keys, the Mailer database URL, mailbox passwords and the key mapping must never be placed in a frontend bundle or in any `NEXT_PUBLIC_*` variable.

LeadBoost decides what to send and whether it is authorized. Mailer validates, records and delivers, and reports state. Mailer does not discover leads, scrape, or score.

## 2. Authentication and organization mapping

Every protected route requires `X-API-Key: <tenant key>`. The key resolves to exactly one Mailer organization; nothing in the request body can change that.

The two systems hold **inverse** mappings of the same secret:

| Side | Variable | Shape |
|---|---|---|
| Mailer API | `ORG_KEY_MAP` | `{"<mailer_tenant_key>": "<mailer_org_id>"}` |
| LeadBoost backend | `MAILING_AGENT_ORG_API_KEYS` | `{"<leadboost_org_id>": "<mailer_tenant_key>"}` |
| LeadBoost backend | `MAILING_AGENT_BASE_URL` | `https://<mailer-api-host>` (no key in the URL) |

Use one distinct key per LeadBoost organization. These keys are not the operator key (`X-System-Operator-Key`, used only for `/system/config`, `/system/metrics/llm`, `/system/integration/validate` and `/system/metrics/{dispatch,worker,inbound}`) and not the mailbox encryption key. See [SECURITY_AND_SECRETS.md](SECURITY_AND_SECRETS.md).

| Status | Meaning |
|---|---|
| 401 | Missing or unknown key (same body either way) |
| 503 | No usable key source configured, or operator key / encryption key unset for a route that needs it |

`ALLOW_INSECURE_NO_AUTH` is never honored on integration or mailbox routes.

## 3. Provisioning a mailbox (once per organization)

A mailbox is Mailer-owned: Mailer stores its SMTP (and optionally IMAP) settings and encrypted passwords and sends from it. The LeadBoost-side provisioning flow is in the [L1 contract](LEADBOOST_L1_MAILBOX_CONTRACT.md); the routes are:

| Method and path | Purpose | Notes |
|---|---|---|
| `POST /mailboxes` | Create (always ACTIVE) | `201`. Passwords are write-only. `409` if `(organization, email)` already exists: adopt it with `GET /mailboxes`. IMAP is all-or-none (`422` on a partial set). `503` if `MAILBOX_ENCRYPTION_KEY` is unset. |
| `GET /mailboxes` | List the organization's mailboxes | |
| `GET /mailboxes/{public_reference}` | Retrieve one | Unknown and foreign references are both `404`. |
| `PATCH /mailboxes/{public_reference}` | Change status, replace passwords, update SMTP/IMAP transport fields | Identity fields are rejected with `422`. |

Dispatch requires **exactly one ACTIVE mailbox** for the organization. No ACTIVE mailbox, or more than one, answers `409` and nothing is queued.

## 4. Requesting an outreach

There are two intake routes. Both return `202 Accepted` with `{accepted, mailing_agent_reference}`, and both identify the operation by `(organization, idempotency_key)`. Confirm in the LeadBoost client which one it calls.

**Generated request (Mailer drafts the text).**
`POST /integrations/leadboost/outreach-requests`

Strict body (unknown fields are rejected with `422`):

- `external_action_id` (required), `idempotency_key` (required, 1 to 255 chars), `correlation_id` (optional, log-only)
- `recipient`: `email` (required), optional `name`, `title`, `company`
- `context`: `value_proposition` (required, up to 2000 chars), `recipient_facts` (list of short strings)

There is no sender, SMTP, mailbox, subject, body, tone or tenant field. The worker later drafts the message and grounds it against the supplied context.

**Exact-message action (caller supplies the text).**
`POST /integrations/leadboost/outreach-actions`

Same envelope with a caller-written `message`. The worker sends it verbatim; no LLM draft is involved.

### Idempotency

- A replay of an accepted key returns the existing operation and touches nothing.
- The same key with a different fingerprint (`external_action_id` plus recipient email) is `409`.
- Concurrent identical requests converge on one dispatch.
- Retry after a transport error with the **same** key. Use a **new** key only for a genuinely new action, such as retrying a `failed` dispatch.

### Other refusals

| Status | Cause |
|---|---|
| 409 | No ACTIVE mailbox; more than one ACTIVE mailbox; idempotency fingerprint conflict; recipient already has an active follow-up scheduled on the integration campaign |
| 503 | First dispatch for an organization while `LEADBOOST_INTEGRATION_SENDER_EMAIL` is unset |
| 422 | Body validation |

## 5. Reading state

| Method and path | Returns |
|---|---|
| `GET /integrations/leadboost/outreach-actions/{idempotency_key}` | `{accepted, state, mailing_agent_reference, updated_at}` |
| `GET /integrations/leadboost/outreach-actions/{idempotency_key}/conversation?limit=N` | The action (state, timestamps, mailbox reference), recent messages (direction, type, subject, body, per-message delivery state) and `has_more`. `limit` defaults to 20, maximum 50. |

An unknown key, or another organization's key, is `404`. Both endpoints are read-only.

### Interpreting `state`

| `state` | Meaning | What LeadBoost should do |
|---|---|---|
| `queued` | Accepted. May be waiting for generation, being generated, or waiting to send (the internal `generating` state is reported as `queued`). | Wait; poll. |
| `sending` | Claimed; the SMTP attempt is in progress or about to start. | Wait; poll. |
| `sent` | The SMTP server accepted the message. **Not** proof the recipient received it. | Treat as delivered to the provider. |
| `failed` | Definitely not sent (generation refused, gate failed, or SMTP refused). | Fix the cause; submit a new key if still wanted. |
| `unknown` | The send's outcome could not be established. Mailer will not resend. | Do not auto-retry. Have an operator check the Sent folder or provider log. |

Accepted, queued, claimed, SMTP attempted, SMTP accepted and recipient receipt are distinct events; only the states above are visible through the API.

## 6. Inbound email

- **IMAP (preferred).** A mailbox with IMAP configured is polled by the worker; the mailbox fixes the organization. LeadBoost sees replies as inbound messages in the conversation endpoint.
- **Webhook (optional).** `POST /webhooks/inbound-email` with a tenant key lets an inbound-parse provider post replies. The organization comes from the key, not the payload.

Mail polled through a mailbox that cannot be matched to exactly one contact in that organization is never attached to a contact and never appears in a conversation; it is retained separately for operator review (`unmatched_inbound_messages`). The webhook and legacy paths do not retain it (see [SYSTEM_DESIGN.md](architecture/SYSTEM_DESIGN.md#9-reply-ingestion-mailbox-ownership-and-deduplication)).

## 7. Health and diagnostics

| Path | Auth | Meaning |
|---|---|---|
| `GET /health` | none | The API process is up. No database or worker probe. |
| `GET /system/health` | tenant key | Organization-scoped counts from the database; implies the database is reachable. |
| `GET /system/metrics/classification`, `/system/metrics/states` | tenant key | Organization-scoped metrics. |
| `GET /system/config`, `/system/metrics/llm`, `/system/integration/validate`, `/system/metrics/dispatch`, `/system/metrics/worker`, `/system/metrics/inbound` | `X-System-Operator-Key` | Process-wide, operator only. `503` when the operator key is unset. The last three are read-only aggregates described in [OPERATOR_OBSERVABILITY.md](OPERATOR_OBSERVABILITY.md); they are not per-tenant analytics and a tenant key never opens them. |

There is no `/metrics` route (the deployed API answered 404; `HOSTED OBSERVED`). The worker has no HTTP endpoint; observe it through its logs.

## 8. Operational prerequisites before the first request

1. `DATABASE_URL`, `ORG_KEY_MAP` and `MAILBOX_ENCRYPTION_KEY` set on the API; `DATABASE_URL`, `MAILBOX_ENCRYPTION_KEY` and `GROQ_API_KEY` on the worker, with the **same** encryption key on both.
2. The migration wrapper has run against the database (see [migrations/README.md](../migrations/README.md)).
3. A mailbox created for the organization.
4. `LEADBOOST_INTEGRATION_SENDER_EMAIL` set on the API.
5. A worker running. Without one, requests are accepted and stay `queued`.
6. `LIVE_SENDING_ENABLED=true` on the worker only when real sends are intended; otherwise dispatches end as `failed` with `live_sending_disabled`.
