-- Read-only pre-migration inspection for migration 010 (PR4 operator observability).
--
-- WHY: migration 010 builds an index on `messages` with a plain CREATE INDEX, which takes a SHARE lock:
-- reads continue but every INSERT/UPDATE/DELETE on `messages` waits for the whole build. The synthetic
-- measurements in the PR description do NOT describe your real database. Run this against the REAL
-- database (or a restored backup of it) first, and decide the write-interruption window from what you see.
--
-- SAFETY: every statement is a SELECT or SHOW and the session is forced read-only below. It never selects
-- query text (pg_stat_activity.query can contain customer data, so nothing here is safe to hide but
-- nothing needs to be). Do not paste output into a public ticket without reading it.
--
-- USAGE (psql, autocommit):   psql "$DATABASE_URL" -f scripts/migrate_010_inspect.sql
-- Run statement 2 twice about 60 seconds apart and subtract to estimate the write rate.

SET default_transaction_read_only = on;

-- 0. Server and the timeouts that will apply to the migration session
SELECT current_setting('server_version') AS server_version,
       current_setting('lock_timeout') AS lock_timeout_default,
       current_setting('statement_timeout') AS statement_timeout_default,
       current_setting('idle_in_transaction_session_timeout') AS idle_in_tx_timeout;

-- 1. How big is messages? (the index build reads the whole heap once)
SELECT pg_size_pretty(pg_total_relation_size('messages')) AS messages_total_size,
       pg_size_pretty(pg_relation_size('messages')) AS messages_heap_size,
       pg_size_pretty(pg_indexes_size('messages')) AS messages_indexes_size,
       c.reltuples::bigint AS estimated_rows
FROM pg_class c WHERE c.oid = 'messages'::regclass;

-- 2. Cumulative write counters since the statistics were last reset. Sample twice, 60 s apart
SELECT now() AS sampled_at, n_live_tup, n_tup_ins, n_tup_upd, n_tup_del, last_autovacuum
FROM pg_stat_user_tables WHERE relname = 'messages';

-- 3. Transactions open right now (no query text). Anything old here is what the build would wait behind
SELECT pid, backend_type, application_name, state, wait_event_type, wait_event,
       now() - xact_start AS transaction_age, now() - state_change AS in_current_state_for
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid() AND xact_start IS NOT NULL
ORDER BY xact_start;

-- 4. Locks currently held or awaited on messages (RowExclusiveLock = ordinary writers, which conflict with SHARE)
SELECT mode, granted, count(*) AS holders
FROM pg_locks WHERE relation = 'messages'::regclass
GROUP BY mode, granted ORDER BY mode, granted;

-- 5. Is the index already there, and valid? (a cancelled CREATE INDEX CONCURRENTLY leaves an INVALID one)
SELECT c.relname AS index_name, i.indisvalid AS is_valid, i.indisready AS is_ready,
       pg_size_pretty(pg_relation_size(c.oid)) AS index_size
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname = 'ix_messages_inbound_created_at';

-- 6. Are the two new tables already there? (NULL = not present)
SELECT to_regclass('worker_runtime_status') AS worker_runtime_status,
       to_regclass('worker_cycle_status') AS worker_cycle_status;

-- 7. OPTIONAL HEAVY QUERY -- intentionally disabled in the default inspection run.
-- This performs a full scan of messages. Run separately only if an exact count is operationally justified:
-- SELECT count(*) FILTER (WHERE direction = 'inbound') AS inbound_rows, count(*) AS total_rows FROM messages;
