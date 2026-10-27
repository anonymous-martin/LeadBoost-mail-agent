# Migrations

Release and deployment context: [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md#4-release-sequence). Schema design context: [docs/architecture/SYSTEM_DESIGN.md](../docs/architecture/SYSTEM_DESIGN.md#4-the-durable-dispatch-table).

This project does not use Alembic for schema migrations, even though
`alembic` is listed in requirements.txt (it isn't currently wired up --
there is no `alembic/versions` directory). Introducing Alembic now, against
a database that may already have been migrated by these hand-rolled scripts
in an unknown subset/order, was judged riskier than documenting what's
actually here. If you want to move to Alembic, `alembic revision
--autogenerate` against a freshly-migrated database is the safe starting
point -- don't try to reconstruct history retroactively.

Instead, every script here is a standalone, **idempotent** Python script:
each one only adds a column/index/constraint if it doesn't already exist
(`add_column_if_not_exists`, `create_index_if_not_exists`, or the
equivalent inline pattern), so re-running any of them against a database
that's already up to date is a safe no-op. That's why running them out of
order is lower-risk than a normal ordered migration chain would be -- but
"lower risk" is not "no risk" (a later script's index can still depend on
an earlier script's column existing), so use the order below.

## Canonical order

Run once, in this order, against a database whose tables already exist (created
by `init_db()` -- which the API/worker run at every start -- or by an earlier
migration run). **Step 1 fails on a database with no tables** (`relation
"campaigns" does not exist`); `scripts/migrate_release.sh` runs `init_db()` first
for that reason. A database that only ever ran `init_db()` is *not* equivalent to
a migrated one: it lacks, for example, `uq_messages_inbound_message_id` and the
work-queue indexes.

1. `001_production_hardening.py` -- organization_id + timezone on
   campaigns, unique constraints, indexes, send-attempt tracking fields.
2. `002_constraints_and_multitenancy.py` -- contact/message uniqueness
   constraints, organization_id on suppression_list, FK cascade
   documentation, hot-path indexes.
3. `002_work_claiming.py` -- **canonical** work-claiming migration:
   `claimed_by` / `claimed_at` on contacts, plus the composite
   `(status, next_action_at)` index the work-claim query in
   `followup/work_claiming.py` actually uses.
4. `../migrate_db.py` (repo root, not in this directory) -- semantic
   intelligence + state-machine columns: `last_reply_at`,
   `last_outbound_at`, `buying_stage`, `engagement_score`,
   `semantic_analysis`, `classification_success`,
   `classification_failure_reason`.
5. `003_message_id_unique_constraint.py` -- unique constraint on
   `messages.message_id_header`, closing a check-then-insert
   deduplication race under true concurrency (see the migration's own
   docstring, and `tests/test_postgresql_concurrency.py`). Checks for
   existing duplicate values first and refuses to apply (with a clear
   error) rather than silently failing mid-`ALTER` if any are found.
6. `004_external_dispatch_and_campaign_integration_source.py` -- Phase C
   (LeadBoost integration) schema: `campaigns.integration_source` +
   `uq_campaigns_org_integration_source`, and the new
   `external_dispatches` table backing
   `POST /integrations/leadboost/outreach-actions`
   (`mailer_agent/api/integrations.py`). Purely additive; does not
   change any existing column's type or nullability, and NULL
   `integration_source` (every pre-existing campaign) is unaffected by
   the new unique index. See the migration's own docstring for why the
   new table is created via `ExternalDispatch.__table__.create(...,
   checkfirst=True)` rather than hand-written DDL.

7. `005_external_dispatch_grounding_context.py` -- C9.2: nullable JSON
   `external_dispatches.grounding_context`, the immutable per-dispatch
   grounding snapshot written by
   `POST /integrations/leadboost/outreach-requests`
   (`mailer_agent/api/integrations_generated.py`). Purely additive; NULL for
   every existing row and every exact-message dispatch (the worker then
   grounds exactly as before). The only migration here with a
   `--downgrade` (drops the column, discarding any snapshots written since).
   **Apply before deploying C9.2 code:** the ORM selects this column on
   every `ExternalDispatch` query, including the existing worker's claim
   path, so code deployed ahead of the column fails on first use.

8. `006_mailboxes.py` -- M1: the Mailer-owned `mailboxes` table
   (`mailer_agent/models.py::Mailbox`, API in `mailer_agent/api/mailboxes.py`).
   Purely additive; nothing existing reads or writes it yet (M2/M3 wire it in).
   Created via `Mailbox.__table__.create(..., checkfirst=True)` (same pattern
   as 004), so it coexists with `init_db()`'s `create_all()`: whichever runs
   first creates the table, the other no-ops. Has a `--downgrade`, which is
   **destructive**: it drops the table and every stored mailbox record and
   encrypted credential. SMTP/IMAP passwords in this table are Fernet
   ciphertext produced with the deployment's `MAILBOX_ENCRYPTION_KEY` (never
   stored in the database): losing or changing that key makes stored
   credentials unreadable, and restoring a backup requires the same key.
   Security boundary: this protects credentials in database dumps/backups and
   against direct database reads; it does NOT protect against compromise of
   the application host, where the database connection and the key coexist.
   Uniqueness is `(organization_id, email_address)` and `public_reference`.
   **M3 consideration:** two organizations may register the same address, so
   per-mailbox IMAP polling could poll one physical inbox twice. M3 does NOT
   deduplicate physical inboxes: each Mailbox is its own inbound identity, and
   a message is stored once per (mailbox, Message-ID), so each organization
   that receives it keeps its own record (see migration 009). Do not give two
   Mailboxes IMAP access to the same physical account unless both organizations
   are meant to receive its mail.

9. `007_external_dispatch_mailbox.py` -- M2-A: nullable
   `external_dispatches.mailbox_id` (FK -> `mailboxes.id`, RESTRICT). Apply
   BEFORE deploying M2-A. Dispatches still QUEUED at deploy time have NULL
   `mailbox_id` and will FAIL pre-SMTP as `no_mailbox` (they never send through
   the old global SMTP identity): drain the queue first, or accept those
   failures (callers retry with a new idempotency key).

10. `008_external_dispatch_deferred_message.py` -- M2-B:
    `external_dispatches.message_id` becomes NULLABLE (PostgreSQL
    `DROP NOT NULL`; no-op on SQLite, which is built from the models). Apply
    BEFORE deploying M2-B and after 007. The new internal state `generating`
    needs no DDL. Downgrade refuses to run while any row has NULL `message_id`.

11. `009_messages_mailbox_scoped_dedupe.py` -- M3: nullable `messages.mailbox_id`
    (FK -> `mailboxes.id`, RESTRICT) and the replacement of the global
    `uq_messages_message_id_header` constraint by two partial unique indexes:
    `UNIQUE(message_id_header) WHERE mailbox_id IS NULL` (exactly the old rule for
    every existing row: outbound, webhook, legacy-global-IMAP) and
    `UNIQUE(mailbox_id, message_id_header) WHERE mailbox_id IS NOT NULL` (the
    mailbox-bound inbound identity). Apply BEFORE deploying M3 code: the ORM
    selects `messages.mailbox_id` on every Message query. Idempotent; has a
    `--downgrade` that refuses to run if a Message-ID is now stored more than
    once. SQLite note: an inline UNIQUE from pre-M3 `CREATE TABLE` cannot be
    dropped there -- recreate development databases from the models.

12. `010_worker_runtime_status.py` -- PR4 (operator observability): creates
    `worker_runtime_status` (one row per scheduler process: opaque random instance
    id, role, started / heartbeat / stopped timestamps) and `worker_cycle_status`
    (one row per process and job kind: latest outcome label and whitelisted
    integer counters), and the PARTIAL index `ix_messages_inbound_created_at` on
    `messages(created_at) WHERE direction = 'inbound'` that serves the
    operator-only inbound aggregate. Purely additive: no business column is
    altered, nothing is dropped, and the tables hold no tenant, contact, address,
    Message-ID, content, credential or error text. Idempotent (create-if-absent,
    model-driven DDL). **The index build takes a SHARE lock on `messages` (reads
    continue, every write waits) for its duration, and the API writes `messages`
    too (inbound-email webhook, message approval, LeadBoost outreach-actions), so
    suspending the worker alone is not enough. Measure the real table size, write
    rate and open transactions first (`scripts/migrate_010_inspect.sql`, runbook
    section 9.1.1) and agree a write-interruption window; the PR's numbers are
    synthetic.** On PostgreSQL the migration sets `lock_timeout` (default 5000 ms,
    `MIGRATION_010_LOCK_TIMEOUT_MS`, clamped 100-300000, never disableable) and an
    optional `statement_timeout` (`MIGRATION_010_STATEMENT_TIMEOUT_MS`), so it
    gives up instead of queueing behind a long writer; the transaction (including
    the new tables) is rolled back, nothing changes, and it exits non-zero. For a
    table too large to block, build the index by hand first with `CREATE INDEX
    CONCURRENTLY` (exact statement in the migration docstring); the migration
    skips an existing index but refuses an INVALID one. **The wrapper itself also
    contends with writers**: steps such as 001 run `CREATE INDEX IF NOT EXISTS ...
    ON messages(...)`, which needs the table's SHARE lock even when the index exists
    (observed: with a writer open, the wrapper hung at step 1/12). Set
    `MIGRATE_LOCK_TIMEOUT_MS` (100-300000, off by default) for the wrapper run to
    apply `lock_timeout` to every statement of every step; see the wrapper header
    and runbook section 9.1.1 (including the connection-pooler caveat).
    **Apply BEFORE deploying PR4 code to a service running
    `SCHEMA_INIT_MODE=verify`:** the new tables are in the ORM metadata, so such a
    service refuses to start until they exist (read-only check: `python
    scripts/migrate_preflight.py verify`, exit 5 and `missing table
    worker_runtime_status`). `--downgrade` drops the two tables and the index; it
    discards only operator telemetry (a running worker recreates it), never
    business data. Code from before PR4 ignores these tables, so rolling the
    *code* back does not require the downgrade.

13. `011_unmatched_inbound_messages.py` -- creates `unmatched_inbound_messages`, the
    durable holding area for mailbox-polled inbound mail that could not be attached
    to exactly one contact (`no_match` / `ambiguous`). Before this, such mail was not
    stored and the IMAP message was still marked Seen, so it survived only on the
    provider side. The row is now committed before Seen is set. Additive: one new
    table (unique on `mailbox_id, message_id_header`; `mailbox_id` -> `mailboxes.id`
    ON DELETE RESTRICT); same `__table__.create(checkfirst=True)` pattern as 006, so it
    coexists with `init_db()`. Nothing existing reads it. `--downgrade` DROPS the table and
    every retained message -- those may exist nowhere else, so export unresolved rows first
    (`SELECT * FROM unmatched_inbound_messages WHERE resolved_at IS NULL`). Code from before
    this change ignores the table, so rolling the *code* back does not require the downgrade.

**Wrapper evidence.** The guarded wrapper `scripts/migrate_release.sh` (with `--status` and `--check-only`) is covered in CI against a disposable PostgreSQL by `tests/test_migrate_release.py`. At the time of that observation the canonical sequence had 11 steps; it has also been run against the dedicated Supabase database: all 11 steps completed and the final `--status` reported the expected markers present (operator-reported). Migration 010 (PR4) has NOT been run against that database -- applying it is an operator-controlled release step. A code deployment and a migration are separate release actions: the wrapper is never run by startup or by every deploy.

**Startup race (fixed in code).** `init_db()` (create-missing-tables) runs at API and worker start in the default `SCHEMA_INIT_MODE=create`. Two processes starting together against an empty database produced a duplicate-sequence error once on hosted infrastructure. `init_db()` now takes a transaction-scoped PostgreSQL advisory lock, so concurrent callers queue and later ones find the tables already present; this was reproduced and fixed on disposable PostgreSQL (`tests/test_schema_init.py`), not re-observed on hosted infrastructure. Still run the wrapper before the first start of a new database: it remains the source of truth, because `init_db()` creates tables only and applies none of the canonical constraints/indexes. A deployed service can set `SCHEMA_INIT_MODE=verify` so it runs no DDL at all; `python scripts/migrate_preflight.py verify` is the read-only dry run of that check.

**Deployment note:** none of these scripts are
run automatically by this repo's `render.yaml` -- its `buildCommand` only
installs dependencies, and the Blueprint provisions no database (the production
database is a dedicated external Supabase PostgreSQL supplied as `DATABASE_URL`;
see `docs/C13_DEPLOYMENT_RUNBOOK.md` section 0 for the first-deployment procedure).
Until a pre-deploy migration step is added (tracked
separately), apply new migrations manually, in the order above, before
deploying application code that depends on them -- in particular, deploying
the Phase C integration endpoint without first running `004_...py` (or the
C9.2 code without `005_...py`) against that environment will 500 on first use.

## Known duplicate

`002_add_work_claiming.py` adds the same two columns as
`002_work_claiming.py` -- both exist because they were written
independently at different points. It is **not** deleted (a real
deployment may have already run it, and rewriting migration history a
production database may have already applied is riskier than leaving a
confirmed-safe duplicate in place), but it is marked deprecated in its own
docstring and is safe to skip for new deployments: `002_work_claiming.py`
is the canonical one and additionally creates the composite index the
work-claim query needs, which the deprecated file does not.

The "002" prefix collision between the three files in this directory is
intentional-by-accident (two different concerns both landed on "002" at
different times) rather than a real ordering conflict -- all three are
idempotent and safe to run in the order listed above regardless.

## Release procedure (operator-run, guarded)

Use `scripts/migrate_release.sh` rather than typing the commands below by hand.
The application's `DATABASE_URL` setting silently defaults to a local SQLite
file, so a hand-run script with the variable missing "succeeds" against nothing.
Migrations are never run by the API or the worker; run them as a one-off
release step, **before** deploying code that needs the new schema.

1. **Back up** the target database and verify the backup restores.
   *First bootstrap of a new, empty dedicated database:* there is no application data
   to back up. Do not claim one exists; record the evidence that does (the `--status`
   output showing the empty state and the provider's own recovery capability, as verified by
   the operator). The apply acknowledgement in step 6 is still required and is never
   bypassed (`docs/C13_DEPLOYMENT_RUNBOOK.md` section 0.4).
2. Suspend the Mailer **worker** and let it drain (see
   `docs/C13_DEPLOYMENT_RUNBOOK.md`, "Pause / resume"). Do not use
   `LIVE_SENDING_ENABLED=false` to pause: it FAILs queued dispatches.
   *First bootstrap:* there is no queue, so nothing is drained and nothing should be
   recorded as drained.
3. Export the target (never printed in full by the tool):
   `export DATABASE_URL='postgresql://...'`
4. `scripts/migrate_release.sh --status` -- read-only schema-marker report.
5. `scripts/migrate_release.sh --check-only` -- every guard, no changes.
6. `MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<database name> scripts/migrate_release.sh`
7. Deploy the new API and worker revision together, then resume the worker.

A database that has only had `init_db()` (the services run it at every start) is **not**
migrated; the wrapper always runs `init_db()` and then the 13 steps, in order.

The tool refuses a missing/SQLite/non-PostgreSQL URL, an unacknowledged apply,
and (migration 007) an upgrade that would strand non-terminal dispatches. It
stops at the first failing step, keeps that step's exit status, never runs a
downgrade, and prints only `host:port/database`.

**Migration 007 -- drain first.** If `external_dispatches.mailbox_id` does not
exist yet, any dispatch still `queued`/`generating`/`sending` when the
mailbox-aware code goes live is failed pre-SMTP as `no_mailbox`. The tool
blocks (exit 4) until none remain; it never edits or deletes them. If the column
already exists the check is informational only.

## Running (reference: what the wrapper executes)

```
python migrations/001_production_hardening.py
python migrations/002_constraints_and_multitenancy.py
python migrations/002_work_claiming.py
python migrate_db.py
python migrations/003_message_id_unique_constraint.py
python migrations/004_external_dispatch_and_campaign_integration_source.py
python migrations/005_external_dispatch_grounding_context.py
python migrations/006_mailboxes.py
python migrations/007_external_dispatch_mailbox.py
python migrations/008_external_dispatch_deferred_message.py
python migrations/009_messages_mailbox_scoped_dedupe.py
python migrations/010_worker_runtime_status.py
python migrations/011_unmatched_inbound_messages.py
```

To revert 005 only: `python migrations/005_external_dispatch_grounding_context.py --downgrade`.
To revert 006 only (destroys all mailbox records/credentials): `python migrations/006_mailboxes.py --downgrade`.
To revert 007 / 008 / 009: `--downgrade` on each (008 refuses while any dispatch has `message_id` NULL; 009 refuses if a Message-ID is now stored more than once).
To revert 010 (drops operator telemetry only -- two status tables and one index; no business data): `python migrations/010_worker_runtime_status.py --downgrade`.
To revert 011 (DESTROYS every retained unmatched/ambiguous message -- export unresolved rows first): `python migrations/011_unmatched_inbound_messages.py --downgrade`.

Safe to re-run the whole sequence any time; every step no-ops on columns/
indexes/constraints that already exist.
