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

## Verified live deployment

- Live URL: https://cashvibe-ckpv.onrender.com
- Render service: Docker web service on the Free compute plan; persistent data is stored in Supabase on a verified Free organization.
- Application source commit: `b758f0417af4b6cadbdafa641aa2ae94f3daeff2`.
- GitHub Actions passed the full test suite, Compose validation, and production Docker build for that commit: https://github.com/aryavaayuarpan/CashVibe/actions/runs/43722982892
- Production acceptance passed account creation, recovery-key gates, login, income/expense recording, budgets, personal-record isolation, group creation/joining, shared expense creation and editing across two independent browser sessions, and settlements.
- A shared expense appeared on the second account at 600 and updated there automatically after an edit to 800.
- SpendGenie used the real Groq provider with `openai/gpt-oss-20b`; the production UI showed Groq AI, proposed an expense, and saved it only after confirmation. A separate live-provider tool check returned the exact recorded 180 total from synthetic records without inventing data.
- All four temporary QA accounts and their dependent group/financial records were removed after the checks.

The managed test computer's Chromium did not trust its HTTPS inspection CA directly. Production browser tests used a loopback-only relay to the fixed Render host; that relay validated certificates and hostnames for every upstream HTTPS/WebSocket connection. No TLS verification was disabled. Test credentials stayed in memory and production screenshots stayed outside the repository.

## Operating limits

Render Free can sleep after inactivity; Supabase Free can pause after extended inactivity. Groq Free has rate limits. Shared changes refresh while the relevant page is open; background push notifications are not included. Keep recovery keys private and back up important records.

## Personal spendings update — 2026-11-16

- 60 automated tests pass, including real synthetic searchable/encrypted PDF extraction, scanned-page OCR, CSV mapping/errors and spreadsheet formula escaping.
- Statement storage tests verify user isolation, exact-file and overlapping-row duplicate handling, atomic rollback, transfer exclusions, amount/date matching and ledger/shared reconciliation without adding duplicate expenses.
- SpendGenie statement-tool tests verify date filtering, account scope and removal of long numeric/VPA identifiers from descriptions.
- Docker build succeeds with local Tesseract installed. The managed build uses its local proxy CA as a temporary BuildKit secret; production Dockerfile uses standard TLS trust. The local sandbox lacked space for a second container filesystem, so runtime browser checks use the same Python source environment; GitHub CI checks the production image build.
- Parsing is bank-independent but layout-dependent. Review and completeness confirmation are mandatory. Unknown app attribution remains explicit; arbitrary bank layouts and OCR accuracy are not guaranteed.
