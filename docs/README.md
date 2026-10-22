# Mailer Agent documentation

Start with the [project README](../README.md) for the 20-second overview. This index groups the rest by what you are trying to do. Every document uses the same evidence labels (`CI VERIFIED`, `HOSTED OBSERVED`, `NOT VERIFIED`, and so on), defined in the [readiness report](FINAL_PRODUCTION_READINESS.md#1-evidence-classes).

## Understand the system

| Document | Read it to |
|---|---|
| [architecture/SYSTEM_DESIGN.md](architecture/SYSTEM_DESIGN.md) | Learn the components, trust boundaries, durable dispatch table, claims and leases, SMTP boundaries and failure modes, and why they are shaped this way. |
| [architecture/diagrams/mailer-system-context.svg](architecture/diagrams/mailer-system-context.svg) | See the whole system, its boundaries and the sync/async paths on one page. |

## Understand the request and delivery lifecycle

| Document | Read it to |
|---|---|
| [architecture/diagrams/mailer-runtime-sequence.svg](architecture/diagrams/mailer-runtime-sequence.svg) | Follow one dispatch from LeadBoost's request to a readable delivery state, including where the database transactions end. |
| [architecture/diagrams/dispatch-state-machine.svg](architecture/diagrams/dispatch-state-machine.svg) | Check which states exist, what moves a row between them, and why `UNKNOWN` is terminal. |

## Use the API and the LeadBoost integration

| Document | Read it to |
|---|---|
| [API_AND_INTEGRATION.md](API_AND_INTEGRATION.md) | Call Mailer correctly: authentication, key mapping, idempotency, outreach requests, state interpretation and prerequisites. |
| [LEADBOOST_L1_MAILBOX_CONTRACT.md](LEADBOOST_L1_MAILBOX_CONTRACT.md) | Get the formal mailbox provisioning and synchronization contract, including schemas and statuses. |

## Configure and operate the services

| Document | Read it to |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Understand reference versus observed topology, CI and mirroring, release sequence, rollback principles and the full configuration table. |
| [OPERATOR_OBSERVABILITY.md](OPERATOR_OBSERVABILITY.md) | Read the three operator-only metrics endpoints (dispatch, worker heartbeat, inbound) correctly: what each number means and does not mean, thresholds, retention, privacy and the query/index review. |
| [C13_DEPLOYMENT_RUNBOOK.md](C13_DEPLOYMENT_RUNBOOK.md) | Follow the detailed first-deployment and steady-state procedures, including pause/resume, `UNKNOWN` handling and which steps have actually been performed. |

## Test and validate

| Document | Read it to |
|---|---|
| [TESTING.md](TESTING.md) | Run each test layer with the right environment and know what each one does and does not prove. |
| [../TESTING_QUICKSTART.md](../TESTING_QUICKSTART.md) | Run the legacy manual walkthrough against a real mailbox (native campaigns only). |

## Migrate and recover the database

| Document | Read it to |
|---|---|
| [../migrations/README.md](../migrations/README.md) | Apply schema changes in the canonical order with the guarded wrapper, and understand each migration's risk. |

## Security, troubleshooting and readiness

| Document | Read it to |
|---|---|
| [SECURITY_AND_SECRETS.md](SECURITY_AND_SECRETS.md) | See the trust model, tenant boundaries, secret separation, Fernet key lifecycle and logging precautions. |
| [FINAL_PRODUCTION_READINESS.md](FINAL_PRODUCTION_READINESS.md) | See what is verified and how, known limitations, remaining acceptance evidence and the historical hardening record. |

For failures, start with the failure-mode table in [SYSTEM_DESIGN.md](architecture/SYSTEM_DESIGN.md#10-failure-modes-and-operational-recovery) and the `UNKNOWN` procedure in the runbook.
