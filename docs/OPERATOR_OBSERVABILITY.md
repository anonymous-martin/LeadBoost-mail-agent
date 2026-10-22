# Operator observability (PR 4)

Three **read-only, operator-only** endpoints answer the questions an operator has at 2 a.m.:
*is the dispatch queue moving, is the worker process alive, is inbound mail being polled?*

| Endpoint | Answers |
|---|---|
| `GET /system/metrics/dispatch` | Durable `ExternalDispatch` counts by state, queue depth and age, stale `sending`, ambiguous `unknown` outcomes |
| `GET /system/metrics/worker` | Per scheduler process: heartbeat `fresh` / `stale` / `stopped`, plus the latest outcome of each scheduled job |
| `GET /system/metrics/inbound` | Mailbox-poll cycle evidence, the legacy global-IMAP path (reported separately), and a windowed count of *persisted* inbound messages |

They are **process-wide across every organization** — deployment operations numbers, not tenant analytics.
Never present them to customers or derive per-customer figures from them.

## Access

* Registered **only** on `operator_router`, behind the existing `require_system_operator` dependency
  (`X-System-Operator-Key`, backed by `SYSTEM_OPERATOR_API_KEY`). No new authentication mechanism.
* A tenant `X-API-Key` (or the legacy `API_KEY`) never opens them; the operator key never opens a tenant
  route. With no operator key configured they answer `503` (fail closed); a missing or wrong key is `401`
  with the same body for both.
* They are deliberately **not** folded into `/system/health`, `/system/metrics/states` or
  `/system/metrics/classification` (tenant-scoped), and Mailer still has **no public `/metrics` route**.
* All three are `GET` only (`405` otherwise) and never write: reading `stale` work reports it; it does not
  resolve, retry or resend anything.

```
curl -sS -H "X-System-Operator-Key: $OPERATOR_KEY" https://<api-host>/system/metrics/dispatch
```

## Four facts that must not be conflated

| Signal | Means | Does **not** mean |
|---|---|---|
| Heartbeat `fresh` | The process wrote its liveness row recently and its scheduler is ticking | Anything about SMTP, IMAP or LLM connectivity; that any job is succeeding |
| Cycle `completed` | A scheduled job ran to its normal end with no failure it could observe | The queue was empty; mail was sent; the mailbox was reachable *for every message* |
| `sent` (SMTP accepted) | A mail server accepted the message for delivery | **The recipient received it** — bounces, filtering and delivery are not observable here |
| Recipient receipt | — | Never observable from Mailer |

The responses carry a `definitions` block repeating these so the numbers are not read out of context.

## `GET /system/metrics/dispatch`

All figures come from SQL aggregation over `external_dispatches` (no row is loaded into Python):

* `states` — counts for `queued`, `generating`, `sending`, `sent`, `failed`, `unknown`, plus
  `unrecognized_state_rows` (a state outside this vocabulary is flagged, never silently dropped).
* `queue` — `depth`; `awaiting_generation` (`queued` and `message_id IS NULL`) vs `ready_to_send`
  (`queued` with a message); `oldest_queued_age_seconds` and the same per sub-queue. Age is measured from
  **acceptance** (`created_at`), not from the last re-queue: it can over-state waiting, never hide it.
  `generating` and `sending` rows are in flight and are not queue depth.
* `sending` — `stale_lease_expired`: rows with `claimed_at IS NULL` or `claimed_at` older than
  `EXTERNAL_DISPATCH_LEASE_SECONDS` (default 900) — **exactly** the predicate the lease-recovery sweep uses
  (cross-checked against the real sweep in tests, including the boundary). A value that stays above zero
  means recovery is not running. `in_flight_or_unexpired` is the remainder.
* `outcomes` — `smtp_accepted` (= `sent`), `failed_not_sent`, and `unknown_needs_operator_reconciliation`.

`unknown` means an SMTP attempt may or may not have been accepted. It needs operator reconciliation and is
**never retried automatically** (a retry could duplicate an email); `failed` is never retried either. PR 4
adds no retry path of any kind.

## `GET /system/metrics/worker`

The API and the worker are separate processes, and nothing in the worker's memory is visible to the API,
so liveness is a small database record: every process that runs the scheduler (`worker.py`, or an API with
`RUN_SCHEDULER_IN_PROCESS=true`) writes a **heartbeat** row.

* **Identity.** Each process start generates a fresh random `instance_id` (32 hex characters). It is
  independent of the hostname, the PID and of `WORKER_ID`, so two workers — even with the same `WORKER_ID` —
  are two rows. `summary.roles_with_multiple_fresh_instances` lists roles with more than one fresh process:
  the intended topology is one worker, so this is worth a look (and is normal for a few seconds during a
  rolling deploy). Roles are `worker`, `api_embedded`, or `scheduler`.
* **Cadence and thresholds.** Heartbeat every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default **30**; floor 5).
  A heartbeat older than `WORKER_STALE_AFTER_SECONDS` (default **120**; ≈4× the interval, so one slow write
  never flaps the status) is `stale`; a heartbeat exactly at the threshold is still `fresh`. Set the same
  values on the API and the worker (the API judges staleness; the worker writes and prunes).
* **States.** `fresh`, `stale`, `stopped`. `stopped` is recorded only by a **graceful** shutdown, as the very
  last step (after in-flight sends drained and claims were released), so "stopped" is never shown while a
  send is in flight. A process killed without cleanup (`SIGKILL`, OOM kill, host loss) never records it and
  simply ages into `stale` — the intended signal. `stale` cannot distinguish a dead process, a wedged
  scheduler, or lost database connectivity.
* **Cycle outcomes** (per process, per job kind: `external_dispatch`, `generation`, `inbound_mailbox`,
  `inbound_legacy`): `last_outcome` is `completed`, `degraded` (ran to its end but an item/mailbox failed)
  or `failed` (did not run to its end). Also `last_cycle_at`, `last_completed_at`,
  `consecutive_unhealthy_cycles`, an exception **class name** in `last_error_type` (never message text),
  `last_cycle_counts`, and `counts_since_instance_start`. Compare `last_cycle_age_seconds` with the job's
  poll interval to spot a job that has stalled while the heartbeat keeps ticking.
* **Counters** reset to zero with each new process and are never summed across processes (retention removes
  old processes, so a cross-process total would silently change).

### How a failure is recorded, and what recording can never do

Two cycle functions (`run_external_dispatch_cycle`, `run_generation_cycle`) **catch and log their own
database errors** and return a normal (possibly empty) list, and `poll_all_mailboxes` returns `[]` both for
"no eligible mailbox" and "could not list mailboxes". "No exception" is therefore not success. PR 4 adds a
backwards-compatible out-parameter (`CycleReport`, `MailboxCycleReport`) so a swallowed database error is
recorded as `failed`; return values and behavior are unchanged.

Status writes happen **after** the work, in their own short session, never inside a claim/send transaction.
A failed write (database unreachable, any error) is swallowed, logged as the operation name plus the
exception **class name only**, and has no other effect: it cannot cause a second SMTP attempt and cannot
alter a persisted dispatch outcome. The only visible consequence is a stale heartbeat.

### What `completed` does not mean (known limits, stated plainly)

* Zero items processed does **not** prove the queue was empty. If `MAILBOX_ENCRYPTION_KEY` is unavailable,
  or shutdown has begun, the dispatch worker leaves the row `queued` and returns nothing — indistinguishable
  from an empty queue at the function boundary. Use `oldest_ready_to_send_age_seconds` on the dispatch
  endpoint as the stuck-queue signal.
* The lease-recovery sweeps return `0` for both "nothing to recover" and a swallowed database error, so they
  are not instrumented; the dispatch endpoint's `stale_lease_expired` is their observable effect.
* Per-item business outcomes (a suppressed recipient, a refused SMTP) are counted in the counters, not
  treated as cycle failures.

### Retention (bounded growth)

One row per scheduler **process** and at most one row per (process, job kind); no event log. A running
scheduler process deletes the rows of processes whose last heartbeat is older than
`WORKER_RUNTIME_RETENTION_HOURS` (default **72**) at start-up and then at most hourly; it never deletes its
own rows. A process that died without cleanup therefore stays visible as `stale` until the next scheduler
start after the window. The endpoint lists at most the 20 most recent processes and says when it truncated.
Time comparisons assume the worker and API hosts' clocks agree to within a few seconds.

## `GET /system/metrics/inbound`

* `mailbox_polling` — evidence from the cycle snapshots the worker writes after each mailbox poll:
  `last_completed_cycle_at`, and per process the mailbox counts (`ok`, `skipped_ineligible`,
  `credential_unavailable`, `imap_error`, `unexpected_error`), `messages_marked_seen`,
  `messages_left_unseen` (a left-unseen message is offered again next poll), and what the handler returned:
  `handled_persisted`, `handled_duplicate`, `handled_unmatched`, `handled_ambiguous`, plus persisted
  classification success/failure.
* `legacy_global_imap` — the deployment-global IMAP path, instrumented **separately** and never a fallback
  for mailbox polling. Its fetch helper returns `[]` for "not configured", "nothing new" and "the poll
  failed" alike, so an empty fetch records nothing: there is **no evidence of fetch success or failure**, and
  its `last_cycle_at` is only the last time it fetched at least one email. Its handler reports no reason for
  an unmatched result, so ambiguous cannot be separated from unmatched (counted as unmatched).
* `persisted_inbound_messages` — inbound `Message` **rows** created in the window (`window_hours`, default 24,
  overridable 1–168): total, how many came via mailbox polling, and classification succeeded / failed / not
  recorded. It includes every channel (mailbox polling, webhook, legacy poll).

**Persisted rows are not "emails received".** Unmatched and ambiguous mail is deliberately *not* stored as a
`Message`; duplicates are not stored twice. The handled counters are the only trace of that mail, and they
are per process lifetime — there is no durable historical total for unpersisted mail. The handled counters are
counted when the handler returns (after its commit, before the message is marked Seen) and are **handler
outcomes, not distinct emails**: a message whose Seen flag fails to store is offered again and counted again,
as a duplicate. The "mark Seen only after the handler returns" behavior is unchanged.

## Privacy and cardinality

Responses contain counts, ages, fixed labels, timestamps, and the opaque random `instance_id`. They never
contain tenant/organization ids, contact ids, addresses, subjects, bodies, Message-IDs, idempotency keys,
public references, hostnames, PIDs, `WORKER_ID`, credentials, or raw error text. Enforced three ways, each
under test: the SQL never selects those columns (`error_message` included — recovery writes worker ids into
it); the stored aggregate can only hold whitelisted integer counters and a sanitised exception class name;
and the read path whitelists again, so even a tampered row cannot emit an unknown field. Log lines on any
status-write failure name the operation and exception class only.

## Query shape and index review

Measured on a **disposable PostgreSQL 16** with synthetic data (40,000 dispatches, 99.8% terminal; 60,000
messages, 1.7% inbound) — not production data:

| Query | Plan | Cost |
|---|---|---|
| Dispatch counts by state | Index-only scan of the **existing** `ix_external_dispatches_state_claimed_at`, 0 heap fetches | 36 buffers, ≈5.6 ms |
| Non-terminal working set (queue split, ages, stale `sending`) | Index scan of the same index, 50 rows | 7 buffers, ≈0.1 ms |
| Persisted-inbound window | Index scan of the new partial index | 15 buffers, ≈0.35 ms |
| Same, **without** the partial index | Sequential scan of `messages` | 500 buffers, ≈9.2 ms (grows with all message volume) |

Conclusions: **no new index on `external_dispatches`**; **one** partial index,
`ix_messages_inbound_created_at ON messages (created_at) WHERE direction = 'inbound'` (40 kB against a 4 MB
table here; it adds a write only to inbound inserts and serves no request path). The state-count query is
the one O(table) read (terminal rows grow without bound) but stays inside the index. These are
point-in-time numbers on synthetic data; re-measure if volumes change by orders of magnitude.

## Triage cheat-sheet

| You see | Likely meaning | First step |
|---|---|---|
| `summary.heartbeat_status` = `stale` or `no_instances_recorded` | No scheduler process is alive/recording (or the DB is unreachable from it) | Check the worker service and its database connectivity; this is **not** proof mail stopped being accepted by the API |
| Heartbeat `fresh` but `external_dispatch.last_outcome` = `failed` | Process alive; its dispatch cycle hits database errors | Worker logs (`external_dispatch cycle aborted`); database health |
| Heartbeat `fresh`, `oldest_ready_to_send_age_seconds` rising, cycles `completed` with 0 processed | Dispatch is not claiming: the mailbox encryption key is unavailable, or a shutdown is in progress (the row is left `queued`) | Worker logs for the claim/pre-flight reason; `MAILBOX_ENCRYPTION_KEY` on the worker |
| `failed_not_sent` rising and `external_dispatch` counters show `failed` | Dispatches are being processed and refused before or at SMTP (for example `LIVE_SENDING_ENABLED=false` marks them `failed` with `live_sending_disabled`, they do **not** stay queued) | The aggregate deliberately carries no reasons; read the worker logs, or the tenant dispatch status, for the reason |
| `stale_lease_expired` > 0 and not falling | Lease recovery not running | Worker; the recovery job |
| `unknown` > 0 | Ambiguous SMTP outcomes | Operator reconciliation against the provider; **do not** bulk-retry |
| `inbound_mailbox` `degraded` with `credential_unavailable` | A mailbox's credential cannot be decrypted | `MAILBOX_ENCRYPTION_KEY` on the worker |
| `messages_left_unseen` > 0 repeating | A handler failure is being retried every poll | Worker logs for the handler error |

## Deployment and rollback

See `docs/C13_DEPLOYMENT_RUNBOOK.md` (PR 4 section) and `migrations/README.md` (migration 010). In short: the
change needs migration **010** (two tables + one partial index), applied through the guarded release
procedure **before** the new code on any service in `SCHEMA_INIT_MODE=verify`. Nothing here was applied to
any live database by this change. The index build blocks `messages` writes (including the API's webhook/approval/outreach-actions routes): measure and agree a window first (runbook 9.1.1). Rolling the code back needs no schema action (old code ignores the tables);
`010 --downgrade` drops only operator telemetry.
