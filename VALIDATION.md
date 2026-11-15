# CashVibe validation record

Checked on 15 November 2026 with Python 3.12, Streamlit 1.67.0, and Docker 29.9.2.

## Measured checks

- The application suite passed **49 tests** (`python -m pytest -q`).
- A separate disposable PostgreSQL 17 database passed **38 backend and agent tests**. These overlap the main suite; they are not 38 additional unique tests.
- Real Chromium sessions verified registration, recovery-key acknowledgement, personal income/expense entry, group creation and joining, shared expense creation and editing, settlements, SpendGenie proposal confirmation, and isolation of private transactions.
- Against the Docker app, **12 consecutive shared edits** reached the other browser session. Four edit dialogs stayed open longer than the polling interval. No JavaScript page errors were observed in that probe.
- The final updated Docker build passed the end-to-end browser flow. New transaction forms reset to Expense after income; reopened settlement confirmations reset unchecked.
- The Docker application ran as uid `1000`, returned a successful health check, and preserved all account, transaction, group expense, settlement, and budget counts after a container restart using its named data volume.
- Compose configuration and Python compilation passed.

The Docker build used a temporary BuildKit secret to trust this test environment's HTTPS proxy certificate. That certificate and the temporary test Dockerfile are not included in the source or application image. TLS verification remained enabled. The supplied production Dockerfile uses ordinary Python package installation.

## Covered invariants

- Personal records remain private to their owner; nonmembers cannot access group records.
- Passwords and recovery keys are hashed. Recovery replaces the key and invalidates existing app sessions.
- Monetary values and splits use exact integer minor units. Shared personal reports count only the user's allocated share.
- Stale edits are rejected. Concurrent settlements cannot exceed current outstanding balances.
- Overall and category budgets do not double count limits in SpendGenie summaries.
- SpendGenie proposals do not mutate records before explicit UI confirmation. Mock provider checks verify user-scoped tool grounding and honest fallback after provider errors.

## Still requires deployment verification

Public Render hosting, the user's actual Supabase database, and live Groq/Ollama inference have not been verified in this workspace. No provider key was supplied. Local insights works without a key and is labeled accordingly. Ollama inference could not be tested within the available disk capacity.

After cloud deployment, repeat the two-device procedure in README.md, check persistence after redeployment, and test SpendGenie with the configured provider. Free hosting can sleep or pause; background push notifications are not part of this release.
