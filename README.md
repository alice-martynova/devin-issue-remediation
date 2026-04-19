# Devin Vulnerability Remediation

Event-driven automation that triggers Devin to fix security vulnerabilities in Apache Superset as soon as a GitHub issue is opened.

## How it works

```
Issue opened in fork
      ↓
GitHub webhook → POST /webhook/github
      ↓
FastAPI extracts issue title, body, file/line details
      ↓
Devin API session created with precise remediation prompt
      ↓
Background poller checks session status every 60s
      ↓
Devin opens PR → bot comments on issue with PR link
```

## Prerequisites

| Tool | Purpose |
|------|---------|
| Docker Desktop | Run the app + ngrok |
| Devin API key | `app.devin.ai/settings/api-keys` |
| GitHub PAT | `github.com/settings/tokens` — needs `repo` + `issues` scopes |
| ngrok account | `dashboard.ngrok.com` — free tier is sufficient |

### Docker Desktop

Docker Desktop is required to build and run the containers. If you see the error `Cannot connect to the Docker daemon`, Docker is not running.

**Install:** Download from [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) and follow the instructions for your OS.

**Start:** Open Docker Desktop from your Applications folder. Wait for the whale icon to appear in your menu bar (Mac) or system tray (Windows) before running any `docker-compose` commands.

For more on how Docker works, see the [Docker getting started guide](https://docs.docker.com/get-started/).

## Setup

**1. Set up your credentials:**

`.env.example` is a template listing every variable the app needs. Copy it to `.env` and fill in your real values:
```bash
cp .env.example .env
```
Then open `.env` and replace each placeholder:

| Variable | Where to get it |
|----------|----------------|
| `DEVIN_API_KEY` | `app.devin.ai/settings/api-keys` |
| `GITHUB_TOKEN` | `github.com/settings/tokens` — needs `repo` + `issues` scopes |
| `GITHUB_WEBHOOK_SECRET` | Any random string you choose — you'll paste the same value into the GitHub webhook settings |
| `NGROK_AUTHTOKEN` | `dashboard.ngrok.com` → Your Authtoken |

> `.env` is listed in `.gitignore` and will never be committed. Only `.env.example` (which contains no real secrets) is committed.

**2. Start everything:**
```bash
docker-compose up --build
```

> **The containers must stay running for the automation to work.** They act as the server that receives GitHub webhooks and polls Devin for status updates. If stopped, incoming webhooks will be lost and active sessions will stop being monitored.
>
> To run in the background so your terminal stays free:
> ```bash
> docker-compose up --build -d
> ```
> View logs at any time with `docker-compose logs -f`.

**3. Register the webhook (one-time):**

Watch the logs for:
```
WEBHOOK URL: https://abc123.ngrok.io/webhook/github
```

Then go to: `github.com/alice-martynova/superset` → Settings → Webhooks → Add webhook
- Payload URL: `https://abc123.ngrok.io/webhook/github`
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET` from your `.env`
- Event: select **"Issues"** only

**4. Start in the background (optional):**

If you don't want the process occupying your terminal, run in detached mode:
```bash
docker-compose up --build -d
```
Logs are still accessible via `docker-compose logs -f`.

> The containers must stay running for the automation to work. If you stop them, incoming webhooks will fail and active session polling will pause.

**5. Create an issue in your fork — Devin starts immediately.**

### Writing effective issues

The issue body is passed directly to Devin as its instructions. The format is not required, but the more specific you are, the better the fix. Always include:

- **File path** — exact relative path from the repo root
- **Line number** — where the vulnerability is
- **Vulnerable code** — the exact snippet
- **Description** — what the vulnerability is and why it's dangerous
- **Fix** — what change to make

A vague body like "fix SQL injection" forces Devin to guess. A body with file, line, and a code snippet gives Devin everything it needs to make a targeted, correct fix.

## Observability

| Endpoint | Description |
|----------|-------------|
| `http://localhost:8000/dashboard` | Live status dashboard (auto-refreshes every 30s) |
| `http://localhost:8000/sessions` | Raw JSON of all sessions |
| `http://localhost:8000/health` | Health check + aggregate metrics |
| `http://localhost:4040` | ngrok tunnel inspector |

## Session lifecycle

| Devin status | Meaning |
|-------------|---------|
| `working` | Devin is actively fixing the issue |
| `blocked` | Devin needs input — check the session URL |
| `finished` | Done — PR link posted to the issue |
| `expired` | Session timed out — manual review needed |

## Project structure

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── src/
│   ├── main.py            # FastAPI app, webhook handler
│   ├── devin_client.py    # Devin API wrapper
│   ├── github_client.py   # GitHub API wrapper
│   ├── session_manager.py # Orchestration + Devin prompt builder
│   └── observability.py   # SQLite session store + metrics
└── templates/
    └── dashboard.html     # Status dashboard
```

## Writing effective issues

For best results, structure issue bodies like this:

```
**File:** `superset/db_engine_specs/postgres.py`
**Line:** 828
**Vulnerable code:**
```python
f"WHERE pid='{cancel_query_id}'"
```
**Fix:** Use parameterized queries — pass `cancel_query_id` as a bound parameter instead of interpolating it into the SQL string.
```
