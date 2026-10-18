# Testing quick start (legacy path note)

This file is a short pointer. It used to be a manual walkthrough for the native campaign API with a deployment-wide SMTP identity. That path is not the LeadBoost integration path, and the old instructions were out of date, so they were removed.

Use these instead:

| Need | Read |
|---|---|
| Run the test suites (deterministic, PostgreSQL, SMTP, IMAP, synthetic end-to-end) with exact commands, requirements and safety notes | [docs/TESTING.md](docs/TESTING.md) |
| Call the LeadBoost integration API, provision a mailbox, interpret dispatch state | [docs/API_AND_INTEGRATION.md](docs/API_AND_INTEGRATION.md) |
| Deploy, migrate, pause and resume sending, handle `UNKNOWN` dispatches | [docs/C13_DEPLOYMENT_RUNBOOK.md](docs/C13_DEPLOYMENT_RUNBOOK.md) |
| Database schema changes | [migrations/README.md](migrations/README.md) |

## Local setup

Python 3.12 is required (`.python-version`).

```bash
python3.12 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q                  # deterministic suite; no credentials or external services
```

## Optional manual exercise of the native campaign API

The scripts `run_e2e_test.sh` and `test_e2e.sh` drive the native campaign endpoints of a locally running API. They are not part of CI. If you use them:

- Run the API locally with `python run.py`, with `LIVE_SENDING_ENABLED=false` and `AUTO_REPLY_ENABLED=false` in your untracked `.env`. In that mode nothing is emailed.
- Use a throwaway local `API_KEY` (generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`). `run_e2e_test.sh` prints the first characters of the key it loads, so do not use a key that protects anything real.
- Read the script before running it. Do not point it at a shared or hosted API.
- This guide does not describe enabling real sending. If you need to test real SMTP delivery, use an isolated disposable mailbox or a local SMTP sink and follow [docs/TESTING.md](docs/TESTING.md); never use a personal or customer mailbox or a real recipient list.

Schema creation for a new local SQLite database happens automatically when the API starts. For PostgreSQL environments, use the guarded wrapper described in [migrations/README.md](migrations/README.md), not an ad-hoc script.
