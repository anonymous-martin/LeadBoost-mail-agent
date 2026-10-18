# LeadBoost L1 — Mailbox provisioning contract (Mailer side)

See also: [API_AND_INTEGRATION.md](API_AND_INTEGRATION.md) for the end-to-end workflow, and [SECURITY_AND_SECRETS.md](SECURITY_AND_SECRETS.md) for key handling.

LeadBoost is the customer-facing control plane; Mailer owns the mailbox
(send-time credential, SMTP execution). L1 adds **no new endpoint** and does not
touch the M2 generated-outreach request (`POST /integrations/leadboost/outreach-requests`,
still exactly one ACTIVE mailbox per organization, no mailbox reference).

## Tenant authentication
Mailer is the tenant authority. A caller presents `X-API-Key`; `ORG_KEY_MAP`
(`{"<api_key>":"<org_id>"}`) maps it to an organization. LeadBoost keeps one key
per LeadBoost organization (`MAILING_AGENT_ORG_API_KEYS` on its side). Organization
ids are never accepted in a request body. Keys are provisioned by an operator
(automated tenant-key provisioning is deferred).

## Calls LeadBoost makes (all organization-scoped by the key)
| Call | Purpose | Carries a credential? |
|---|---|---|
| `POST /mailboxes` | create (always ACTIVE); 409 if `(org, email)` exists | **yes** (once) |
| `GET /mailboxes` | adopt an existing mailbox after a 409 / lost response | no |
| `PATCH /mailboxes/{ref}` `{"status":"disabled"}` | stop sending (unverified, disabled, rotated) | no |
| `PATCH /mailboxes/{ref}` `{"status":"active","smtp_host","smtp_port","smtp_use_tls","smtp_username","smtp_password"}` | atomic activation after LeadBoost verified | **yes** |

PATCH now also accepts `smtp_host`, `smtp_port`, `smtp_use_tls`, `smtp_username`
(L1). It still rejects `email_address`, `organization_id`, `public_reference` and
a partial IMAP set (422). Since M3, PATCH accepts IMAP configuration as an
all-or-none set (`imap_host`, `imap_port`, `imap_username`, `imap_password`
together; `imap_password` alone rotates an existing configuration). LeadBoost
does NOT send IMAP fields: inbound is provisioned through the Mailer API with the
organization's key, and the password is write-only. A reference owned by another organization is
indistinguishable from an unknown one (404).

## Idempotent provisioning
`UNIQUE(organization_id, email_address)` is the backstop. Create → on 409 list and
adopt by lower-cased email → PATCH to the desired state. A retry after a lost
response, or a concurrent caller, converges on one mailbox.

## Transport limit
`smtp_use_tls=true` means STARTTLS only. Mailer has no implicit-TLS (SMTP_SSL,
port 465) path; LeadBoost therefore does not provision accounts using that mode.

## Deployment prerequisites for the integrated path
* `MAILBOX_ENCRYPTION_KEY` — API **and** worker (same value). Mailbox create/update and sending fail closed without it.
* `ORG_KEY_MAP` — API only; one key per LeadBoost organization.
* `LEADBOOST_INTEGRATION_SENDER_EMAIL` — API. While empty, the **first** generated-outreach request of every
  organization returns `503`. (Pre-existing M2 requirement; found by the L1 cross-service E2E.)
