#!/usr/bin/env bash
# Guarded, operator-run release migration for the Mailer Agent database.
#
# This is a RELEASE step, run once per release by an operator. It is never
# called from the API or the worker, and it is not part of application startup.
#
#   scripts/migrate_release.sh --status        read-only: report schema state
#   scripts/migrate_release.sh --check-only    read-only: run every guard, change nothing
#   scripts/migrate_release.sh                 apply (needs the backup acknowledgement)
#
# Required environment:
#   DATABASE_URL            PostgreSQL URL of the database to migrate. Must be set in
#                           the environment: never read from .env, never defaulted
#                           (the application's own default is a local SQLite file).
#   MIGRATE_CONFIRM_BACKUP  (apply only) I_HAVE_A_VERIFIED_BACKUP_OF_<database name>
#
# Optional environment (apply only):
#   MIGRATE_LOCK_TIMEOUT_MS  100..300000. Sets PostgreSQL lock_timeout (via PGOPTIONS) for EVERY statement
#                           of this run, so a step that cannot get its table lock gives up instead of
#                           waiting behind a long writer -- while it waits, every NEW writer queues behind
#                           it. RECOMMENDED when any writer (the API's webhook/approve routes, the worker)
#                           may be active: several existing steps run CREATE INDEX IF NOT EXISTS on
#                           `messages`, which needs the table's SHARE lock even when the index exists.
#                           A failed step reports its number; the run stops and later steps are not run.
#                           Off by default (no behaviour change). If a connection pooler rejects the
#                           startup option ("unsupported startup parameter: options"), use a direct or
#                           session-mode connection for migrations, or quiesce all writers instead.
#                           Migration 010 additionally applies its own lock_timeout (see its docstring).
#
# Test-only: MIGRATE_DISPOSABLE_TEST_DB=yes waives the acknowledgement for a
# LOOPBACK host and a database named mailer_migrate_test_* only. See
# scripts/migrate_preflight.py for every guard.
#
# Order = migrations/README.md "Canonical order", preceded by init_db() (the same
# create-missing-tables call the API and worker make at start). Step 001 fails on a
# database with no tables, so init_db() must come first on an empty database.
# Downgrades are never run by this script.
#
# Exit status: 0 success; otherwise the first failing step's / guard's non-zero code.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
PREFLIGHT=("$PYTHON" scripts/migrate_preflight.py)

MODE=apply
case "${1:-}" in
  "")           MODE=apply ;;
  --status)     MODE=status ;;
  --check-only) MODE=check ;;
  *) echo "usage: $0 [--status | --check-only]   (no downgrade option exists)" >&2; exit 64 ;;
esac
if [ "$#" -gt 1 ]; then
  echo "usage: $0 [--status | --check-only]" >&2
  exit 64
fi

STEPS=(
  "migrations/001_production_hardening.py"
  "migrations/002_constraints_and_multitenancy.py"
  "migrations/002_work_claiming.py"
  "migrate_db.py"
  "migrations/003_message_id_unique_constraint.py"
  "migrations/004_external_dispatch_and_campaign_integration_source.py"
  "migrations/005_external_dispatch_grounding_context.py"
  "migrations/006_mailboxes.py"
  "migrations/007_external_dispatch_mailbox.py"
  "migrations/008_external_dispatch_deferred_message.py"
  "migrations/009_messages_mailbox_scoped_dedupe.py"
  "migrations/010_worker_runtime_status.py"
)

if [ "$MODE" = status ]; then
  "${PREFLIGHT[@]}" status
  exit 0
fi

if [ "$MODE" = check ]; then
  "${PREFLIGHT[@]}" gate
  "${PREFLIGHT[@]}" status
  echo "[migrate] check-only: no changes were made."
  exit 0
fi

# Opt-in: fail fast instead of queueing behind writers (see the header). Validated strictly; applies to
# every Python process this script starts, because libpq reads PGOPTIONS from the environment.
if [ -n "${MIGRATE_LOCK_TIMEOUT_MS:-}" ]; then
  case "$MIGRATE_LOCK_TIMEOUT_MS" in
    *[!0-9]*) echo "[migrate] MIGRATE_LOCK_TIMEOUT_MS must be an integer number of milliseconds (100..300000)." >&2; exit 2 ;;
  esac
  if [ "$MIGRATE_LOCK_TIMEOUT_MS" -lt 100 ] || [ "$MIGRATE_LOCK_TIMEOUT_MS" -gt 300000 ]; then
    echo "[migrate] MIGRATE_LOCK_TIMEOUT_MS must be between 100 and 300000 (0 is refused: PostgreSQL reads it as 'wait forever')." >&2
    exit 2
  fi
  export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c lock_timeout=${MIGRATE_LOCK_TIMEOUT_MS}"
  echo "[migrate] lock_timeout=${MIGRATE_LOCK_TIMEOUT_MS}ms applied to every statement of this run."
else
  echo "[migrate] NOTE: no MIGRATE_LOCK_TIMEOUT_MS set -- a step that needs a table lock will wait for active writers (and queue new writers behind it). See the header." >&2
fi

# Apply: every guard first. A non-zero exit here (set -e) stops everything.
"${PREFLIGHT[@]}" gate --apply
"${PREFLIGHT[@]}" init

total="${#STEPS[@]}"
n=0
for step in "${STEPS[@]}"; do
  n=$((n + 1))
  echo "[migrate] ($n/$total) $step"
  rc=0
  "$PYTHON" "$step" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[migrate] FAILED at step $n/$total ($step), exit $rc. Later steps were NOT run." >&2
    echo "[migrate] Do not continue manually. Restore from the backup or investigate; see docs/C13_DEPLOYMENT_RUNBOOK.md." >&2
    exit "$rc"
  fi
done

echo "[migrate] all $total steps completed. Schema state after the run:"
"${PREFLIGHT[@]}" status
