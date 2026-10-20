# Security and secrets

Trust model, authentication roles, tenant boundaries, secret handling and logging precautions for Mailer Agent. This document contains no real secret, hostname, mailbox address or log content; every example uses a placeholder.

Related: [system design](architecture/SYSTEM_DESIGN.md), [API and integration](API_AND_INTEGRATION.md), [deployment](DEPLOYMENT.md), [migrations](../migrations/README.md).

## 1. Trust model

| Principal | How it authenticates | What it can reach |
|---|---|---|
| LeadBoost backend (per LeadBoost organization) | Tenant key in `X-API-Key` | Its own organization's mailboxes, dispatches and conversations |
| Inbound-parse provider (optional) | Tenant key in `X-API-Key` | `POST /webhooks/inbound-email` for that organization |
| Operator | `X-System-Operator-Key` | Process-wide diagnostics only: `/system/config`, `/system/metrics/llm`, `/system/integration/validate` |
| Mailer worker | No network identity; holds `DATABASE_URL`, the encryption key, LLM key | Everything in the database; it is trusted infrastructure |
| Browser | None | Nothing; it must never see Mailer |

The API is fail-closed. With no key source configured the process refuses to start and protected routes answer 503. An unknown key and a missing key both answer 401 with the same body, so the response does not reveal which keys exist. Keys are compared in constant time. `ALLOW_INSECURE_NO_AUTH` exists for local development only, is ignored when any key is configured, is refused together with `LIVE_SENDING_ENABLED=true`, and is never honored on integration or mailbox routes.

## 2. Tenant isolation

- The organization is derived only from the presented key. A body `organization_id`, a header, or message content never selects or widens the tenant.
- Queries on campaigns, contacts, messages, dispatches and mailboxes are scoped by organization. A resource that does not exist and a resource owned by another organization both answer 404, never 403.
- Inbound IMAP mail is bound to the polled mailbox's organization before any content is processed, and deduplicated per `(mailbox, Message-ID)`.
- Known boundaries to be aware of: two organizations can register the same email address, and Mailer does not deduplicate physical inboxes; and `is_suppressed()` consults suppression across organizations by design. Both are documented trade-offs, not hidden behavior.
- Tenant-boundary behavior is covered by the isolation and correlation tests listed in [TESTING.md](TESTING.md). That is a test result, not a statement about a hosted environment: multi-tenant operation on the deployed system has `NOT VERIFIED` status. The deployed configuration observed so far uses a small, operator-controlled set of organizations.

## 3. The secrets, and why they are separate

| Secret | Held by | Purpose | Must never |
|---|---|---|---|
| Tenant API keys (`ORG_KEY_MAP`) | Mailer API; LeadBoost backend holds the inverse map | Identify a tenant to the API | Appear in a frontend bundle, `NEXT_PUBLIC_*`, a URL or a log |
| `SYSTEM_OPERATOR_API_KEY` | Mailer API only | Authorize process-wide diagnostics | Equal any tenant key (the API refuses to start, and re-checks per request) |
| `MAILBOX_ENCRYPTION_KEY` | Mailer API and worker (same value) | Fernet key protecting mailbox passwords at rest | Be stored in the database or auto-generated |
| `DATABASE_URL` | API and worker | Database access | Be committed or shown in docs, logs or tickets |
| `GROQ_API_KEY` | Worker | LLM drafting | Be logged |
| Mailbox SMTP/IMAP passwords | Stored encrypted in the database | Authenticate to the tenant's mail provider | Be returned by any endpoint or logged |

Keeping tenant keys, the operator key and the encryption key distinct means that leaking one does not grant the powers of another: a leaked tenant key cannot read diagnostics, and a leaked operator key cannot call tenant routes.

## 4. Generation and placement

Generate each secret independently:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"                                   # tenant or operator key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"      # MAILBOX_ENCRYPTION_KEY
```

Place values only in provider secret stores (Render, Railway and Supabase dashboards) or a local untracked `.env`. `render.yaml` marks secrets `sync: false` so they are never committed. Do not paste a value into an issue, a pull request or a chat; report that a variable is set, not what it is.

## 5. Mailbox credential encryption lifecycle

- Passwords are accepted on `POST /mailboxes` and `PATCH /mailboxes/{ref}` as write-only fields, encrypted with Fernet immediately, and stored as ciphertext. Response models are an explicit allowlist and omit them.
- The worker decrypts the SMTP password for one send, inside the send-claim transaction, and does not touch the IMAP password there.
- If `MAILBOX_ENCRYPTION_KEY` is unset or not a valid Fernet key, mailbox credential operations answer 503, and a send claim rolls back and leaves the dispatch `QUEUED`.
- **Preserve the key.** Backups of the database are useful only together with the same key. Losing or changing it makes every stored credential unreadable. Back it up separately from the database and restrict access to both.
- **Rotation.** The code uses a single Fernet key (not a multi-key set), so there is no built-in overlap period. Changing the key means every mailbox password must be re-supplied with `PATCH` afterward. Plan it as a maintenance step.
- **What it does and does not protect.** It protects credentials in database dumps and against direct database reads. It does not protect against compromise of a host where the database connection and the key coexist.
- The `006_mailboxes.py --downgrade` is destructive: it drops the table and all stored ciphertext.

## 6. Rotating tenant and operator keys

- The key map is `key -> organization`, so a rollover adds a second key for the same organization, updates LeadBoost's `MAILING_AGENT_ORG_API_KEYS`, then removes the old key. Both keys authenticate as that one tenant during the overlap. This is covered by `tests/test_pr2_key_map_collisions.py`.
- **One credential, one tenant.** The API refuses to start (and every protected route would answer 503) if the same key is configured for two different organizations, whether inside one source or across `ORG_KEY_MAP`, `ORG_KEYS_*` and the legacy `API_KEY` (which counts as organization `default`), or if a key is repeated inside the `ORG_KEY_MAP` JSON object. There is no precedence between sources any more, so a configuration mistake can no longer silently re-assign a credential to another tenant. The error names only the conflicting sources, never a key, an organization id or a variable name. Keys are compared after trimming whitespace; organization ids are compared exactly (`Acme` and `acme` are different tenants).
- **Before deploying this change**, run `python scripts/check_config.py --role api` with the deployed environment (on a trusted workstation; the script never prints values) and confirm there is no collision, in particular that the legacy `API_KEY` is not also a tenant key in `ORG_KEY_MAP`. An existing deployment with such a collision used to start with the tenant mapping silently taking precedence; it will now refuse to start. The fix is to give each credential a single tenant (or unset `API_KEY` once nothing depends on it) and redeploy. Do not rotate secrets as part of this change.
- Rotating the operator key is a single-variable change on the API; no tenant is affected.
- After any rotation, confirm with a read-only call such as `GET /mailboxes` using the new key.

## 7. Replay protection

Idempotency is a database unique constraint on `(organization_id, idempotency_key)` plus a request fingerprint. A replay returns the existing operation; a conflicting reuse is 409. There is no request signing or timestamp window: replay protection means "the same key never creates a second dispatch", not "a captured request cannot be sent again".

## 8. Logging and sensitive data

- 422 responses on mailbox routes are reduced to `type`, `loc` and `msg`, so submitted values (including passwords) are never echoed.
- Secrets are `SecretStr` in settings and models so reprs do not leak them. The operator key is never logged.
- Request identifiers and organization identifiers appear in logs; message bodies and recipient addresses are PII. Treat worker logs as sensitive and do not paste them into tickets or into this repository.
- Run `scripts/check_config.py` before enabling live sending; it validates configuration without printing secret values.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are off by default. Enabling them publishes the route inventory without authentication.

## 9. Safe operating practices

- Keep `LIVE_SENDING_ENABLED=false` on any environment that is not meant to send. With it false, a claimed dispatch fails closed and nothing leaves the building.
- Test against disposable mailboxes and sinks; never against a customer's mailbox or a real recipient list.
- Run the migration wrapper only after a verified backup, as a separate release step (see [migrations/README.md](../migrations/README.md)).
- Run one scheduler. Do not leave `RUN_SCHEDULER_IN_PROCESS=true` on the API when a worker is deployed.
- Remove redundant services (for example a second API process) once the intended topology is confirmed, so there is one place to rotate secrets.
