"""
Standalone worker process: runs the reply-poll and follow-up/dispatch
jobs on their own schedule, with no HTTP server attached.

Deploy this as a Render "Background Worker" (start command: ``python
worker.py``) alongside a separate "Web Service" running the FastAPI app
(start command: see Procfile). Keeping them as two processes means the
API can scale (multiple gunicorn workers) without ever running the
scheduler more than once -- if the scheduler ran inside every gunicorn
worker process, you'd get duplicate sends and duplicate reply
processing.

For local development, you don't need this at all: ``python run.py``
with the default RUN_SCHEDULER_IN_PROCESS=true runs everything in one
process.

Phase 7 — Scheduler ownership
------------------------------
The scheduler is deliberately split from the API process:

* ``run.py`` / ``mailer_agent/api/main.py`` lifespan handler:
  Only starts the scheduler when ``RUN_SCHEDULER_IN_PROCESS=true``
  (the default for single-process local dev).  When set to false (the
  recommended production setting) the API process logs a warning and
  never starts any APScheduler jobs -- it is purely an HTTP server.

* ``worker.py`` (this file):
  Always starts the scheduler.  In production, exactly one instance of
  this process should run (Render Background Worker, or a single
  container replica).  Having exactly one scheduler process means there
  are never duplicate reply-poll or follow-up-dispatch cycles from the
  scheduler side.

* Residual duplicate-work risk:
  Even with one scheduler, if a job cycle takes longer than its
  interval the next run begins while the previous one is still active.
  APScheduler's ``max_instances=1`` per job prevents this (any
  overlapping trigger is silently skipped).  For the remaining
  concurrent-worker case (two ``worker.py`` processes running
  simultaneously, e.g. during a rolling deploy), Phase 8's database-
  level work claiming (SELECT FOR UPDATE SKIP LOCKED) is the defence.

Phase 8 — Graceful shutdown and claim release
----------------------------------------------
On shutdown (KeyboardInterrupt / SIGTERM), this process releases any
work claims it currently holds so the next worker cycle (possibly on a
newly-started worker) can pick them up immediately rather than waiting
for the lease to expire (default 300 s).

SIGTERM (fix): the code above only ever ran on KeyboardInterrupt. Python's
default SIGTERM action terminates the process immediately WITHOUT raising
SystemExit, so on a platform that stops services with SIGTERM (Render) the
release logic never executed. ``install_signal_handlers()`` converts SIGTERM
into SystemExit so the shutdown path below actually runs.

LeadBoost ExternalDispatch shutdown ordering (``graceful_shutdown``):
  1. stop taking NEW external-dispatch claims;
  2. stop the scheduler (no new job triggers);
  3. wait a bounded time for in-flight dispatches to finish on their own;
  4. dispatches THIS process still owns after the drain deadline are moved
     SENDING -> UNKNOWN, by dispatch ID + claim token (never by worker_id
     alone, never to QUEUED -- an interrupted send cannot be assumed unsent);
  5. release Contact claims as before;
  6. (PR4) mark this process's liveness row stopped -- last, so "stopped" is never shown while a
     send is still in flight. A process killed without cleanup never reaches this step and its
     heartbeat simply ages into "stale" on the operator worker metrics.
"""

import logging
import os
import signal
import time

from mailer_agent.db import prepare_schema, session_scope
from mailer_agent.followup.scheduler import (
    mark_runtime_stopped,
    start_scheduler,
    stop_scheduler,
)
from mailer_agent.followup.work_claiming import make_worker_id, release_all_claims
from mailer_agent.mail.external_dispatch_worker import (
    begin_external_dispatch_shutdown,
    drain_external_dispatches_on_shutdown,
)
from mailer_agent.worker_status import ROLE_WORKER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mailer_agent.worker")

_terminating = False


def _handle_sigterm(signum, frame):  # noqa: ARG001
    """First SIGTERM -> SystemExit (runs graceful_shutdown). Repeats are
    ignored so a second signal cannot interrupt a shutdown in progress."""
    global _terminating
    if _terminating:
        logger.info("Repeated termination signal ignored; shutdown already in progress")
        return
    _terminating = True
    raise SystemExit(0)


def install_signal_handlers() -> None:
    """Must be called from the main thread."""
    signal.signal(signal.SIGTERM, _handle_sigterm)


def graceful_shutdown(worker_id: str) -> None:
    """The ordered shutdown described in the module docstring. Each step is
    best-effort and isolated so one failure cannot skip the next."""
    logger.info("Shutting down background worker (worker_id=%s)", worker_id)

    try:
        begin_external_dispatch_shutdown()  # 1. no new external-dispatch claims
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not stop external dispatch claiming: %s", exc)

    try:
        stop_scheduler()  # 2. no new job triggers (running jobs continue)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not stop scheduler cleanly: %s", exc)

    try:
        report = drain_external_dispatches_on_shutdown()  # 3 + 4
        if report.abandoned_unknown or not report.drained:
            logger.warning(
                "External dispatch shutdown: drained=%s resolved_to_unknown=%s not_owned=%s",
                report.drained, list(report.abandoned_unknown), list(report.still_owned_elsewhere),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("External dispatch drain failed; lease recovery will resolve: %s", exc)

    # 5. Phase 8: Release any Contact claims so the next worker can pick them
    # up immediately rather than waiting for lease expiry.
    try:
        with session_scope() as db:
            released = release_all_claims(db, worker_id)
            if released:
                logger.info("Released %d claim(s) on graceful shutdown", released)
    except Exception as exc:  # noqa: BLE001 — best-effort, don't mask shutdown
        logger.warning("Could not release claims on shutdown: %s", exc)

    # 6. PR4: mark this process's liveness row stopped -- LAST, after in-flight sends have drained
    # and claims are released, so "stopped" is never shown while work is still in flight. Best-effort:
    # if the database is unreachable the heartbeat simply ages into "stale"; nothing else changes.
    try:
        mark_runtime_stopped()
    except Exception:  # noqa: BLE001
        logger.warning("Could not mark worker runtime stopped")


if __name__ == "__main__":
    prepare_schema()
    install_signal_handlers()

    worker_id = make_worker_id()
    # Expose worker_id in env so child scheduler jobs inherit it automatically.
    os.environ.setdefault("WORKER_ID", worker_id)

    logger.info(
        "Starting Mailer Agent background worker (scheduler-only, no HTTP server) "
        "worker_id=%s",
        worker_id,
    )

    # start_scheduler() is INSIDE the try on purpose: a SIGTERM that lands
    # right after the scheduler starts (before the loop below) would otherwise
    # raise SystemExit outside the handler and skip graceful_shutdown().
    try:
        scheduler = start_scheduler(role=ROLE_WORKER)
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        graceful_shutdown(worker_id)
