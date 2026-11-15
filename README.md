# CashVibe

**Student money, in sync.** A Docker-ready Streamlit expense tracker with personal records, monthly budgets, shared group expenses, settlements, financial insights, and SpendGenie.

This repository is the application and deployment configuration. A local server or a passing build is **not** a permanent public deployment. To publish it, complete the account and secret setup below and verify the live Render URL. No hosting credentials or API keys are bundled.

![CashVibe dashboard with synthetic test records](docs/dashboard.png)

Screenshots use test records. New accounts start with an empty ledger. [Phone view](docs/mobile.png) · [SpendGenie](docs/spendgenie.png)

## What is included

- Register, sign in, sign out, and recover an account with a private recovery code.
- Record income and expenses; search, filter, edit, delete, and export personal history.
- Monthly category budgets with configurable warning thresholds and financial charts.
- Invite-only groups, exact shared splits, participant balances, shared expense changes, and settlement records.
- Shared records stored in one database; active pages poll for updates (five seconds by default).
- Savings goals and SpendGenie financial tools with confirmed write actions.
- Docker for Windows, macOS, and Linux; persistent SQLite for a single local server, or PostgreSQL for public hosting.

The system follows the supplied research paper's focus on recording, budgeting, readable summaries, reliable storage, and threshold alerts. Group expense sharing and SpendGenie extend that baseline. There is no automatic bank integration or transfer service.

## Run with Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS) or Docker Engine with Compose (Linux). In this directory:

```sh
cp .env.example .env
docker compose up --build -d
```

On Windows PowerShell, replace `cp` with `Copy-Item`. Open **http://localhost:8501** and create your account. There are no default credentials or sample records. Save the recovery code shown when you register; it replaces email-based password recovery.

Data is stored in the `cashvibe_data` named volume. `docker compose down` preserves it; `docker compose down -v` **deletes it**. Keep a backup before removing volumes, reinstalling Docker, or moving machines.

Two devices must connect to the **same running server**, not separate local installations. On a trusted local network, open `http://SERVER_LAN_IP:8501` from the other device, allowing port 8501 in the server's firewall. For internet access use the HTTPS deployment below. Each participant registers their own account, then joins the same group using its invite code.

```sh
docker compose logs -f cashvibe
docker compose ps
docker compose down
```

## Deploy free: Render + Supabase

Recommended stack: **Render Free Docker web service**, **Supabase Free PostgreSQL**, and an optional **Groq Free** API key for a hosted language model. The same Docker image is used locally and on Render. Accounts belong to you; no paid plan is required for the baseline stack within its free allowances.

### 1. Create a persistent database

1. Create a free project in [Supabase](https://supabase.com/dashboard). Keep the database password in your password manager.
2. Open the project's **Connect** dialog. Select the **Transaction pooler** PostgreSQL connection string (port `6543`), which supports IPv4 hosting.
3. Replace the password placeholder with your database password. URL-encode special characters in the password. Use this shape, copying the actual host and project reference from your dashboard:

   ```text
   postgresql+psycopg://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@POOLER_HOST:6543/postgres?sslmode=require
   ```

4. Save that full URI as `DATABASE_URL` in the hosting dashboard. Set `REQUIRE_POSTGRES=true` so a missing or invalid PostgreSQL configuration cannot silently fall back to local storage. Do not commit the URI or paste it into chat.

The backend creates its tables under the `cashvibe` PostgreSQL schema. Leave that schema out of Supabase's exposed API schemas. Browser clients never receive the database URI or a service-role key; all database access goes through the Streamlit server. This app manages hashed passwords and recovery codes itself, so Supabase Auth email/SMTP setup is not needed.

### 2. Publish the source to your GitHub account

Create a repository and upload the contents of this folder, including `Dockerfile`, `requirements.txt`, `.streamlit/config.toml`, `cashvibe/`, `app.py`, and `render.yaml`. Keep `.env`, `.streamlit/secrets.toml`, database files, and recovery codes out of the repository. The supplied ignore files help prevent accidental inclusion.

### 3. Create the Render service

1. Sign into [Render](https://dashboard.render.com/) with access to that repository.
2. Choose **New → Blueprint**, select the repository, and apply `render.yaml`. Confirm the service's plan is **Free**.
3. Enter `DATABASE_URL` when prompted. The Blueprint uses Docker, port `8501`, and health path `/_stcore/health`.
4. Optional: under **Environment**, add `GROQ_API_KEY` from your own [Groq console](https://console.groq.com/keys). Its value must be a server secret. `GROQ_MODEL` defaults to `openai/gpt-oss-20b`; update it if your provider changes model availability.
5. Deploy and wait for **Live**. Open the literal HTTPS URL returned by Render, register, and save your recovery code.

For a manual Render setup, choose **New → Web Service → Git repository → Docker → Free**, set port `8501`, `DATABASE_URL`, and `REQUIRE_POSTGRES=true`, then use health check `/_stcore/health`. Do not use an ephemeral local SQLite file for public hosting.

### 4. Verify the deployed product

Use two browsers or two devices with separate accounts:

1. Create a group in account A; join it in account B with its invite code.
2. Record a shared expense in A, then keep B's group page open. Its expense and balance should update after the polling interval.
3. Edit that expense and check both balances again. Record a settlement from the account that owes money.
4. Add a personal expense and budget. Verify the budget status and chart totals.
5. Ask SpendGenie for a summary and ask it to record a small expense. Review the proposal, confirm once, and check the ledger.
6. Sign out, sign back in, and restart/redeploy the web service. Records must persist in Supabase.

Only after the live URL and these checks pass should the app be described as deployed. Account sign-up, secret entry, and platform deployment must happen in your hosting account.

### Free hosting limits

Free tiers have quotas and availability limits; they are not an always-on service guarantee. As checked in November 2026:

- [Render Free](https://render.com/docs/free) web services sleep after 15 minutes without inbound traffic. A cold start can take about a minute. Free instance hours and memory limits apply, and the local filesystem is ephemeral. Do not choose Render's free PostgreSQL for permanent storage; that database expires after 30 days.
- [Supabase Free](https://supabase.com/pricing) includes a 500 MB database and may pause projects after a week of low activity. Resume a paused project in its dashboard. Back up important records; free plans do not provide the same backup guarantees as paid plans.
- [Groq Free](https://console.groq.com/docs/rate-limits) imposes model-specific request/token limits. When the provider is unavailable or rate-limited, CashVibe labels its fallback as Local insights.

Never enable paid upgrades or usage-based add-ons without reviewing their pricing. Keep one small Render service and one Supabase project for this product. Hosting providers may change their free plans.

## SpendGenie

SpendGenie has two provider options and a clearly labeled fallback:

- **Groq:** set `GROQ_API_KEY` for the real hosted language-model agent. It can call user-scoped financial tools and propose recording an income, expense, or budget. The interface requires confirmation before applying a proposal.
- **Ollama:** run a local language model on your own computer. This avoids a hosted API bill, but uses your computer's RAM, disk, and CPU/GPU.
- **Local insights:** works without an API key for supported financial summaries and structured requests. It is a deterministic assistant, not a hosted language model. Unsupported requests should be rephrased or sent with a configured provider.

Financial context sent to your chosen model provider can include transaction descriptions and financial totals. Passwords, recovery codes, and database credentials are not included. Follow the provider's data policy when deciding which option to use.

### Optional local AI with Docker

Set this in `.env`:

```dotenv
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:3b
```

Then run:

```sh
docker compose --profile local-ai up --build -d
docker compose exec ollama ollama pull qwen2.5:3b
```

The model download is several gigabytes. Start with at least 8 GB available system RAM; speed depends on hardware. The Ollama API is available only on the Compose network, not published to the internet. Remove `GROQ_API_KEY` if you want to use Ollama instead of Groq. Restart the CashVibe service after changing `.env`.

## Python development

Python 3.12 is recommended:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
streamlit run app.py
```

On PowerShell, activate with `.venv\Scripts\Activate.ps1`. For direct Python execution, environment values must be exported in the terminal or configured in Streamlit secrets; Compose's automatic `.env` substitution does not apply to this command.

```sh
pytest -q
```

Dependency versions are pinned in `requirements.txt`. Install and test updates deliberately. The Docker image uses a non-root application user and a Streamlit health check.

The GitHub Actions workflow runs the test suite and builds the Docker image on pushes and pull requests. It uses disposable test databases and does not upload expense records, model responses, screenshots, database files, or other artifacts. Run `pytest -q` locally before deploying; automated tests do not verify your live hosting account or provider quota.

## Alternative: Streamlit Community Cloud

[Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud) can deploy this Python source for free from GitHub, but it does not run the Docker image. Create an app with `app.py` as entrypoint. In the app's **Settings → Secrets**, configure:

```toml
DATABASE_URL = "postgresql+psycopg://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@POOLER_HOST:6543/postgres?sslmode=require"
REQUIRE_POSTGRES = "true"
CASHVIBE_CURRENCY = "INR"
CASHVIBE_TIMEZONE = "Asia/Kolkata"
CASHVIBE_SYNC_SECONDS = "5"
# Optional:
# GROQ_API_KEY = "your-private-key"
# GROQ_MODEL = "openai/gpt-oss-20b"
```

Use PostgreSQL here too; local app files are not a reliable persistent database. Community Cloud has its own resource and inactivity limits. Docker remains available for local use and migration to another host.

## Data, access, and operation

- One deployment uses one currency, `INR` by default. Changing the currency label does **not** convert saved amounts. Choose it before recording data.
- Shared expenses allocate each participant's share in exact integer minor units. Personal spending reports count your share; money you paid for friends remains a group receivable.
- Invite codes grant group membership. Share them privately; rotate a code if it is exposed. A rotated code does not remove existing participants.
- Shared updates are polling-based while the relevant page is open. The app does not promise push notifications when closed or background mobile synchronization.
- Settlements record money that participants have transferred separately. CashVibe does not move money.
- Save your recovery code privately. There is no automatic email recovery or email verification in this version.
- Keep database credentials server-side, restrict access to hosting dashboards, retain HTTPS, and export/backup records regularly. Financial insights are descriptive and estimated from recorded data.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | empty | PostgreSQL connection URI; empty means local SQLite. |
| `REQUIRE_POSTGRES` | `false` locally | Set `true` on every public host to reject local/absent database configuration. |
| `CASHVIBE_DB_PATH` | `data/cashvibe.db` | Local SQLite file; Docker overrides to `/app/data/cashvibe.db`. |
| `CASHVIBE_CURRENCY` | `INR` | Currency label for the deployment. |
| `CASHVIBE_TIMEZONE` | `Asia/Kolkata` | Dates and month boundaries. |
| `CASHVIBE_SYNC_SECONDS` | `5` | Active-view refresh interval. |
| `GROQ_API_KEY` | empty | Enables hosted SpendGenie language model. |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Provider model identifier. |
| `OLLAMA_BASE_URL` | empty | Optional trusted Ollama server root URL. |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Local model identifier. |

## Troubleshooting

- **Database connection error:** use the Supabase pooler URI from its Connect dialog, URL-encode the password, keep `sslmode=require`, and ensure the project is running. Do not publish the full URI in logs or bug reports.
- **Slow first load:** a free Render service may be waking up. Wait about a minute and retry.
- **Data lost after redeployment:** the app was using ephemeral local storage. Configure `DATABASE_URL` before entering production records.
- **Another participant cannot see an expense:** confirm both people are signed in to the same deployed URL and joined the same group. Leave the group view open for the refresh interval.
- **SpendGenie shows Local insights:** configure a valid provider or wait for its free rate limit to recover; the fallback label reflects its actual operating mode.
- **Docker data permission error:** use the supplied named volume and uid `1000`. A manually bound host folder must be writable by that user.

MIT licensed. See [LICENSE](LICENSE).
