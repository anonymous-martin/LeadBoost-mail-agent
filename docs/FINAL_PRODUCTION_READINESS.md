# Mailer Agent: production readiness and evidence report

This is the single readiness document for the repository. It separates what has been demonstrated, and how, from what is only implemented or still unknown. Read sections 1 to 7 for the current state. Appendix A preserves the earlier hardening record (sessions 1 to 4) because its root-cause analyses remain useful; the verification statuses written there were true at the time and are **superseded by the ledger below**.

Related: [system design](architecture/SYSTEM_DESIGN.md), [testing](TESTING.md), [deployment](DEPLOYMENT.md), [runbook](C13_DEPLOYMENT_RUNBOOK.md).

## 1. Evidence classes

| Class | Meaning |
|---|---|
| `LOCAL E2E VERIFIED` | The complete workflow was exercised locally, with the components and test mailbox or sink stated |
| `CI VERIFIED` | A named automated job passed in a named run |
| `HOSTED OBSERVED` | Behavior was directly observed on the deployed service (health response, worker logs, operator terminal output) |
| `IMPLEMENTED, NOT HOST-VERIFIED` | The code implements the behavior; the real deployed path has not demonstrated it |
| `NOT VERIFIED` | Evidence is insufficient |
| `KNOWN LIMITATION` | An implemented or architectural constraint that remains relevant |
| `DEFERRED` | Work deliberately left for a later phase |

The classes are never merged. In particular, a passing test is not a hosted observation, and a local end-to-end run is not a CI result.

## 2. Snapshot

| Item | Value |
|---|---|
| Documentation base | `origin/main` at `ce0cbfaf44fba6817350cfe0a190bc4b4a366245` ("build: pin Render Python runtime to 3.12") |
| Runtime | Python 3.12 (`.python-version`, CI) |
| Most recent confirmed CI | Workflow `CI`, run `38626064792` on the Render-connected repository, commit `ce0cbfa`, conclusion `success`. Observed through the GitHub API; per-job results and the canonical repository's own run were not retrievable when this was written |
| Deployed API | `https://mailer-agent-api.onrender.com` (Render) |
| Deployed worker | Railway service running `python worker.py` |
| Database | Dedicated Supabase PostgreSQL |
| Topology | Split-provider staging/demo; differs from the all-Render reference in `render.yaml` |

## 3. Evidence ledger

| Capability | Evidence | Basis and scope |
|---|---|---|
| Deterministic contract, state-machine and tenant-boundary tests | `CI VERIFIED` | `quality-and-tests` job succeeded as part of workflow run `38626064792` (workflow conclusion `success`) |
| Durable dispatch, idempotency, `SKIP LOCKED` claims, leases, fencing on real PostgreSQL | `CI VERIFIED` | `postgres-integration` job file list; hosted behavior is `IMPLEMENTED, NOT HOST-VERIFIED` |
| Migration wrapper and guards on a disposable PostgreSQL | `CI VERIFIED` | `tests/test_migrate_release.py`, required (cannot skip) in CI |
| Migration wrapper on the real Supabase database | `HOSTED OBSERVED` | 11-step sequence completed; final `--status` reported expected markers present (operator-reported terminal observation) |
| SMTP outcome classification (`SENT` / `FAILED` / `UNKNOWN`) over a real socket | `CI VERIFIED` | `local-smtp` job against a local SMTP sink; real providers are `NOT VERIFIED` |
| Per-mailbox IMAP inbound, and the generation-send-reply chain | `CI VERIFIED` | `m3-imap-e2e` job against disposable Dovecot and a local SMTP sink with PostgreSQL |
| Mailbox API contract, authentication, encryption helpers and tenant isolation (SQLite-backed deterministic tests such as `tests/test_mailbox_foundation.py`, `tests/test_l1_mailbox_transport_update.py`, `tests/test_c15_1_auth_fail_closed.py`) | `CI VERIFIED` | Part of the `quality-and-tests` job in workflow run `38626064792`; says nothing about PostgreSQL behavior |
| Mailbox persistence and encryption on PostgreSQL (`tests/test_mailbox_postgres.py`) | `NOT VERIFIED` | The test exists and is marked `postgres`, but no CI job lists it, so no CI run covers it. This documentation pass did not run it either. It must not be read as having passed in CI |
| Worker graceful shutdown (`SENDING` to `UNKNOWN`) | `IMPLEMENTED, NOT HOST-VERIFIED` | Covered by `tests/test_external_dispatch_scheduler_shutdown.py`; not observed on a deployed service |
| Worker scheduling on the deployed worker | `HOSTED OBSERVED` | Railway logs show APScheduler starting and scheduled dispatch and generation jobs executing |
| API liveness on the deployed API | `HOSTED OBSERVED` | `GET /health` returned 200; `GET /metrics` returned 404 (no such route exists) |
| Fail-closed auth, `/docs` exposure, operator routes on the deployed API | `IMPLEMENTED, NOT HOST-VERIFIED` | CI-tested against the app in a fresh process; not probed on the deployment |
| Complete LeadBoost-plus-Mailer workflow: discovery, scraping, scoring, lead generation, email sending, follow-ups, SMTP and IMAP | `LOCAL E2E VERIFIED` | Operator-reported, scoped to the operator's local setup. The test mailbox or sink is not recorded here. Discovery, scraping and scoring are LeadBoost responsibilities and are not Mailer capabilities. Not an automated result and not a hosted result |
| Live Groq generation on the deployed worker | `NOT VERIFIED` | No hosted evidence |
| Deployed request, full dispatch lifecycle and receipt of a message | `NOT VERIFIED` | Hosted end-to-end delivery has not been demonstrated |
| Throughput, latency, availability, RTO and RPO | `NOT VERIFIED` | Never measured; no benchmark artifact exists |

## 4. Hosted observations, in detail

- The Render Mailer API returned HTTP 200 from `/health`, and HTTP 404 from `/metrics`. No metrics endpoint exists at that path; telemetry is under `/system/metrics/*`. `/health` does not probe the database. The API does run `init_db()` before serving and a failure there stops startup, so a responding API is consistent with a successful database connection and schema check at startup. This is observed database use, not evidence of delivery.
- The Railway worker logs showed APScheduler starting and the scheduled dispatch and generation jobs executing. The jobs query the database, so this is observed database use at runtime; it does not show a dispatch being claimed, sent or received, and whether the job runs logged errors was not recorded.
- **Railway Limited Trial caveat.** The Railway project is on a Limited Trial, and outbound SMTP from the worker may be restricted on that plan. The account's permitted networking and sending path has not been verified, so SMTP connectivity from the Railway worker is `NOT VERIFIED` and a current deployment caveat.
- Supabase was reached over its session pooler from a WSL shell. Reachability from Render was not recorded.
- The canonical migration wrapper completed its 11-step sequence; the final `--status` reported the expected markers present.
- An initial concurrent `init_db()` startup produced a duplicate-sequence error; a later startup recovered. Both the API and the worker call `init_db()` at start, and the API runs two Gunicorn workers, so several processes can reach `create_all` together. The race is **not** declared fixed; hardening is `DEFERRED`.
- A second Railway service named `web` (Gunicorn API process, from the `Procfile`) was seen. Its removal is unconfirmed.

## 5. Known limitations (`KNOWN LIMITATION`)

- **Deployment-wide sender persona.** All LeadBoost-integration mail uses one `From` identity set by `LEADBOOST_INTEGRATION_SENDER_*`, regardless of organization. Acceptable for a controlled staging deployment, not for distinct customers needing their own sending identity.
- **Bounce handling coverage is deferred.**
- **No Prometheus-style `/metrics`;** the worker has no HTTP health endpoint, so worker failure is detected from service status and logs.
- **Unmatched inbound mail is not retained.** Mailbox mail that cannot be matched to a contact in the polled organization is neither stored nor guessed at, and is marked seen.
- **`UNKNOWN` needs a human.** Ambiguous sends are never retried; reconciliation is by `Message-ID` against the Sent folder or provider log.
- **Single Fernet key, no overlap rotation.** Changing `MAILBOX_ENCRYPTION_KEY` means re-supplying every mailbox password.
- **No request signing.** Replay protection is idempotency, not authenticated timestamps.
- **One scheduler assumed.** Claims and fencing keep concurrent workers correct; the topology is one worker.
- **Shared physical inboxes and cross-organization suppression** are documented trade-offs (see [SECURITY_AND_SECRETS.md](SECURITY_AND_SECRETS.md)).
- **CI coverage gap.** `tests/test_mailbox_postgres.py` is not in any CI job's file list. Lint, `pip-audit` and `bandit` are report-only.

## 6. Remaining acceptance evidence

1. Remove or reconfigure the redundant Railway `web` service, then record the final service list.
2. Harden `init_db()` against concurrent starts. The observed race stays unresolved (`DEFERRED`) until a code change and a regression test demonstrate a fix; running the migration wrapper before starting both services is a mitigation, not a verified fix.
3. Verify the Railway account's permitted outbound networking and SMTP path (Limited Trial caveat), or move the worker to a provider where outbound SMTP is confirmed.
4. Run the controlled acceptance on the deployed system: one request through the deployed API with `LIVE_SENDING_ENABLED=true` on the worker only, to a disposable recipient the operator controls. Record the request and response, the dispatch states (`queued` through `sent`), the worker log lines and the receipt. Until then deployed LeadBoost-to-Mailer delivery is `NOT VERIFIED`.
5. Confirm the deployed fail-closed behavior (unauthenticated `401`, no-key `503`), that `/docs` answers 404, and that operator routes reject tenant keys.
6. Confirm key agreement between LeadBoost's `MAILING_AGENT_ORG_API_KEYS` and Mailer's `ORG_KEY_MAP` with a read-only call.
7. Record backup and restore evidence for the Supabase project, including the `MAILBOX_ENCRYPTION_KEY` backup.
8. Capture a canonical-repository CI run ID for the release being accepted, and either add `tests/test_mailbox_postgres.py` to the PostgreSQL CI job or record why not.

## 7. Deferred

IMAP cursor, an orphan inbound table, webhook `To:` correlation, native auto-reply transaction redesign, automated migrations, worker liveness signal, per-organization sender identity, rate limits, key rotation tooling, and an `UNKNOWN` remedy policy.

## 8. Contradictions found in this documentation pass

Resolved in the documents: the README's CI table (it listed three jobs; there are four) and its deployment statements; the runbook's claim that no Mailer resource existed; this report's stale statuses.

Not changed, because they are source comments and this task changes no application code:

- The docstring of `api/integrations_conversation.py` describes the webhook path as resolving a `Message-ID` without an organization filter, while the runbook (C15.1b), `api/webhooks.py` (tenant resolved from the presented key) and `tests/test_c15_1b_webhook_tenant_scope.py` describe it as organization-scoped. The runbook and test are treated as current; the comment looks stale and should be reviewed.
- `MessageStatus` documentation says `SENDING` is unused, but the dispatch worker sets it.
- The module docstring of `mail/external_dispatch_worker.py` says the endpoint creates the `Message`; on the generated route the generation worker creates it.

## 9. Where the test commands are

Source comments and `pytest.ini` point here for "the exact command" to run the opt-in suites. Those commands, with their environment requirements and safety notes, now live in [TESTING.md](TESTING.md#2-commands).

---

# Appendix A: historical hardening record (sessions 1 to 4)

Preserved for its root-cause analyses. The statuses below ("not re-run", "implemented, not run") describe the state when each session ended; use sections 1 to 8 for current status. The session-4 command list and cross-session status table were removed because CI now runs those suites.

### A.1-3. Sessions 1-3 summary (condensed)

**Session 1**: implemented the approval-time grounding recheck; implemented
`MessageStatus.UNKNOWN` end-to-end; fixed a cross-tenant leak in
`/system/*`; removed dead/broken code; fixed a test-isolation bug where
`llm/provider.py` bound functions at import time; rewrote the
keyword-guessing `FakeLLMProvider`.

**Session 2**: fixed the inbound-correlation ambiguous-match guard; fixed
Message-ID normalization across webhook/IMAP; fixed an authentication-
retry bug; fixed a suppression gap in auto-reply; added a deterministic
Python E2E suite and a context-contamination test.

**Session 3 (Phase 2 semantic architecture)**: found the planner
(`policy/next_action.py`) was orphaned architecture (one dead reference,
never called); rewrote it as an LLM-backed Planner and wired it into the
real reply path via new deterministic Guardrails; removed the
`positive_interest + evaluating -> pricing_discussed` semantic
assumption from the state machine; removed the hardcoded 30-day timing
fallback; fixed the critical contextual-reply fallback (was generating a
generic CTA regardless of what the prospect said); added memory
accumulation, observability/replay, an evaluation corpus, architecture
tests (genuinely executed via `ast`, not traced), and found + fixed two
real concurrency gaps (`message_id_header` had no DB-level uniqueness;
`approve_and_send_draft` had no row lock) while *designing* the
PostgreSQL test suite.

---

### A.4 Session-4 targeted correctness repair (historical)

Explicitly scoped, per the instructions that opened this session: no
semantic architecture changes, no new AI components, no new frameworks —
only fixes for confirmed defects, each backed by a specific reported
failure and diagnosis.

#### A.4.1 (TASK A) Conversation-evidence provenance — root cause of both approval-safety-gate failures

**Reported failures**: `test_never_grounded_claim_is_blocked_at_approval`
and `test_context_changed_since_draft_generation_blocks_send`, both in
`tests/test_approval_safety_gate.py`.

**Diagnosed root cause** (via direct diagnostic, not inspection):
`memory/store.py`'s `build_conversation_context()` and
`maybe_summarize_older_messages()` both did `list(contact.messages)` —
every Message row for the contact, regardless of status — and
unconditionally labeled every outbound row `"US (sent)"` in the
transcript. A DRAFT message (never sent, possibly containing an
unsupported claim) was therefore included in its own "conversation
evidence." When `validate_grounding()` checked whether a claim in the
draft was supported by `proof_points + context_notes +
conversation_transcript + value_prop`, the draft's own unsupported claim
appeared inside `conversation_transcript` (via its own inclusion in the
message list) — making the check circular. The draft appeared to
support itself.

**Fix**: one shared filter, `_conversational_evidence()` in
`memory/store.py`, applied at both call sites:
- Inbound messages are always valid evidence.
- Outbound messages count only when `status == MessageStatus.SENT.value`
  exactly. `DRAFT`, `FAILED`, and `UNKNOWN` (ambiguous SMTP outcome —
  deliberately not treated as "probably sent," consistent with how
  `UNKNOWN` is already handled everywhere else in this codebase) are all
  excluded. Uses the real `MessageStatus`/`MessageDirection` enums
  already in `models.py` — no new status values invented, no keyword/
  semantic logic involved.

This is a plausible, traced explanation for both reported failures (the
mechanism — a draft's claim appearing in its own evidence — applies
directly to both test scenarios), but has not been re-executed to
confirm the tests now pass. See §9.

**New regression tests**: `tests/test_memory_provenance.py` (7 tests) —
DRAFT absent from context; DRAFT cannot self-ground (direct
reproduction of the circular-evidence mechanism); a draft grounded at
creation becomes ungrounded after `proof_points` changes; a stale DRAFT
never enters the rolling summary (even after aging out of the verbatim
window); SENT outbound and INBOUND messages remain valid evidence;
FAILED/UNKNOWN messages are excluded the same as DRAFT.

#### A.4.2 (TASK B) Pre-existing memory summaries — documented, no migration

`contact.memory_summary` is a plain `Text` column with no per-fact
provenance, so a summary persisted before the fix in §4.1 may have been
generated from a window that included a draft's content — and there is
no way to *selectively* correct that in the stored text. No schema
migration was written: the column itself doesn't change, and
automatically re-summarizing every existing contact would mean an
unreviewed LLM call per contact with its own new-error risk, which isn't
obviously safer than a stale field. Documented directly on the
`memory_summary` column in `models.py`, including the safe manual
remedy for a specific contact if one is suspected (clear that one
`memory_summary` value; it regenerates correctly, now provenance-
filtered, next time the thread crosses `SUMMARIZE_THRESHOLD`).

#### A.4.3 (TASK C) Provider error classification precedence

**Reported failure**: a persistent-5xx provider-failure test failed
because `"504 Gateway Timeout"` was classified as `TimeoutError`, not
`ProviderUnavailableError`. Root cause given directly: the generic
`"timeout" in error_str` text check ran *before* the explicit 5xx status-
code check, and `"504 Gateway Timeout"` contains the word "timeout" in
its standard HTTP reason phrase.

**Fix**: reordered `_classify_error()` in `llm/provider_v2.py` so every
explicit-status-code branch (401/403 → `AuthenticationError`, 429 →
`RateLimitError`, 500/502/503/504 → `ProviderUnavailableError`, 400 →
`ValidationError`) is checked before the generic text-only branches
(`timeout`, malformed JSON) that exist for errors carrying no HTTP
status at all. The retry architecture itself was not touched — still
max 3 attempts, no SDK-level retry, the same three retryable exception
types, no second retry framework.

**Verified by direct execution in this session** (not traced): a
minimal standalone reproduction of the exact bug (a lowered string
containing both a 5xx code and the word "timeout") was run in this
sandbox using pure Python, confirming the *old* precedence would
misclassify and the *new* precedence classifies correctly. This is
narrower than actually running the real test suite, but it is a genuine
execution, not an inspection.

**New/updated tests**: `tests/test_provider_failures.py` gained
`test_504_gateway_timeout_classified_as_provider_unavailable_not_timeout`
(the focused regression) and
`test_generic_timeout_without_http_status_still_classified_as_timeout`
(proves the reordering didn't disable the generic case it's meant to
still cover).

#### A.4.4 (TASK D) SQLite E2E fixture cross-connection bug

**Reported failure**: `tests/e2e/test_full_lifecycle.py`, 4 failures,
`sqlite3.OperationalError: no such table: campaigns`.

**Diagnosed root cause**: `create_engine("sqlite:///:memory:", ...)`
without `poolclass=StaticPool` lets SQLAlchemy's connection pool hand out
a *different* connection — and therefore a completely separate, empty
in-memory database, since SQLite's `:memory:` databases are connection-
local — to different consumers. `Base.metadata.create_all()` ran against
one connection; `TestClient`'s request handling (which can dispatch
through a different thread depending on the ASGI transport) could be
handed a different one with no tables at all.

**Fix**: added `poolclass=StaticPool` (forces the whole engine to share
exactly one underlying connection) alongside the existing
`check_same_thread=False`. Production database wiring
(`mailer_agent/db.py`) was not touched — this is purely a test-fixture
change. `tests/test_approval_safety_gate.py` and
`tests/test_tenant_correlation.py` do not need this fix (already
reported passing) because they never go through `TestClient`/cross-
thread access — they call functions directly with the same session
object in the same thread.

#### A.4.5 (TASK E) `tests/test_flow.py` isolation and auth

**Reported failure**: 401s cascading into `KeyError("id")` (a 401
response body has no `"id"` key).

**Diagnosed root cause**: the file relied on module-level
`os.environ["API_KEY"] = ""` / `os.environ["DATABASE_URL"] = ...`
mutations, combined with `api/deps.py`'s key→org map and
`mailer_agent.db`'s engine, both built once at first import. Whichever
test file's imports happen to run first in the session decides what
those singletons end up bound to; a later file's environment mutations
have no effect on an already-built map/engine. This is the exact
fragility class flagged and fixed for other files in sessions 2-3 (see
`tests/e2e/test_full_lifecycle.py`'s own docstring), just not yet
applied here.

**Fix**: rewrote the `client` fixture around the same robust pattern
already used in `tests/e2e/test_full_lifecycle.py` — FastAPI
`dependency_overrides` for `get_db`/`require_api_key`/`get_current_org_id`,
a fresh `StaticPool`-backed in-memory engine per test, no environment-
variable reliance at all. `test_inbound_webhook_matches_thread` also
needed an explicit fix beyond the fixture: this session's Planner
changes mean a reply now makes up to three LLM calls (classify → plan →
draft), and this test doesn't care about classification content — so it
now explicitly forces `is_llm_available()` to `False` for a clean,
fixture-free deterministic-fallback path, rather than needing to queue
three fixtures for a test whose assertions don't depend on them. No
`201`/`200` assertions were weakened; the fixture change is orthogonal to
what each test actually checks.

#### A.4.6 (TASK F) Architecture test's postponed-annotation bug

**Reported failure**: `test_guardrails_authorize_action_takes_typed_proposal`
failed because `guardrails.py` has `from __future__ import annotations`
(PEP 563), so `inspect.signature(...).annotation` returns the *string*
`"NextActionProposal"`, not the class object the test compared against
with `is`.

**Fix**: switched to `typing.get_type_hints(authorize_action)`, which
resolves postponed-annotation strings back into real objects against the
function's own module globals. The production type contract itself
(`guardrails.authorize_action(proposal: NextActionProposal, ...)`) was
not touched — this was purely a test bug, not a production one.

**Verified by direct execution in this session** (not traced): built a
minimal standalone reproduction matching the exact real shape
(`from __future__ import annotations`, a dataclass parameter, keyword-
only arguments, a dataclass return type) and ran it in this sandbox.
Confirmed `inspect.signature(...).annotation` returns the string (the
bug, reproduced) and `typing.get_type_hints(...)` returns the real class
object matching `is NextActionProposal` (the fix, confirmed). This is a
genuine execution of the exact logic pattern, though not the real
`mailer_agent.policy.guardrails` module itself (which needs `tenacity`/
`pydantic-settings`, unavailable in this sandbox).

#### A.4.7 (TASK G) Integration tests leaking into the default suite

**Reported issue**: default `python -m pytest -q` executed live Groq
integration tests, making the default suite nondeterministic and rate-
limit dependent.

**Diagnosed root cause**: session 3 registered the `integration` marker
in `pytest.ini` and made the autouse `fake_llm` fixture skip patching for
integration-marked tests (so they'd genuinely hit the real API when
explicitly requested) — but never told pytest to *exclude* that marker
by default. Registering a marker and excluding it by default are two
different steps, and only the first was done.

**Fix**: added `addopts = -m "not integration"` to `pytest.ini`. Default
`pytest -q` now excludes anything marked `integration` (which includes
`tests/evaluation/test_live_llm_semantic_quality.py` and — since it's
marked with both `integration` and `postgres` — `tests/test_postgresql_concurrency.py`
too). Running `python -m pytest -m integration -v` explicitly overrides
the ini default (pytest's marker-expression option takes the
last-specified value when both `addopts` and the command line specify
`-m`, so the explicit CLI flag wins) — this is standard, documented
pytest behavior for this exact `addopts`-default-plus-CLI-override
pattern, not independently re-executed in this sandbox (no `pytest`
installed here at all).

#### A.4.8 What was explicitly NOT touched (TASK H, honored)

No new AI components, classifier layers, retrieval/vector DB, or
generic agent framework were introduced. `SemanticIntent` was not
redesigned. The planner architecture (LLM proposes via
`policy/next_action.py`, guardrails authorize via
`policy/guardrails.py`) is unchanged from session 3. The retry
architecture in `provider_v2.py` (max 3 attempts, no SDK-level retry,
exactly the same three retryable exception types) is unchanged — only
the *classification order* that decides which exception type an error
maps to was reordered.

---

### A.6 Files changed in session 4

`mailer_agent/memory/store.py` (§4.1), `mailer_agent/models.py` (§4.2,
documentation only), `mailer_agent/llm/provider_v2.py` (§4.3),
`tests/e2e/test_full_lifecycle.py` (§4.4), `tests/test_flow.py` (§4.5),
`tests/test_architecture.py` (§4.6), `pytest.ini` (§4.7). New:
`tests/test_memory_provenance.py` (7 tests, §4.1).
`tests/test_provider_failures.py` gained 2 tests (§4.3).

### A.7 Why each change was necessary

- `memory/store.py`: without it, a draft's own unsupported claim could
  appear as its own supporting evidence, defeating the entire grounding
  safety gate.
- `models.py`: no code change needed; documents a real data-lineage gap
  so it isn't silently forgotten.
- `provider_v2.py`: without it, a real 502/503/504 provider outage is
  misreported as a client-side timeout in monitoring/observability, even
  though the retry behavior itself was already correct.
- `test_full_lifecycle.py`: without `StaticPool`, the E2E suite cannot
  reliably see its own test data through `TestClient`.
- `test_flow.py`: without dependency overrides, this file's auth/DB
  setup silently depends on which other test file happens to import
  first.
- `test_architecture.py`: without `get_type_hints`, this test fails
  under postponed annotations regardless of whether the production
  typing contract is actually correct — a false negative, not evidence
  of a real problem.
- `pytest.ini`: without `addopts`, every default CI run burns real Groq
  API quota and is nondeterministic.

### A.8 Confirmation: no semantic architecture redesign

No new classifier layers, no DeBERTa/RoBERTa/RAG/vector DB, no
LangChain/Kafka/Celery, no generic agent framework. `SemanticIntent` is
unchanged from session 3. The planner/guardrails split is unchanged. The
only planner/guardrails-adjacent change this session was to a *test*
(`test_architecture.py`'s type-resolution method), not to
`policy/next_action.py` or `policy/guardrails.py` themselves — both are
byte-for-byte unchanged since session 3. The retry *architecture* in
`provider_v2.py` (attempt count, which exceptions retry, no SDK-level
retry) is unchanged; only classification *precedence* — which bucket a
given error string falls into before the existing retry logic sees it —
was reordered.
