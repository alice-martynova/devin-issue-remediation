# Devin Issue Remediation

Event-driven automation that triggers Devin to resolve GitHub issues as soon as they are opened.

## How it works

```
Issue opened 
      ↓
GitHub webhook → POST /webhook/github
      ↓
FastAPI extracts issue title and body
      ↓
Devin API session created with the issue details as its prompt
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

## Setup

**1. Set up your credentials:**

`.env.example` is a template listing every variable the app needs. Copy it to `.env` and fill in your real values:
```bash
cp .env.example .env
```

| Variable | Where to get it |
|----------|----------------|
| `DEVIN_API_KEY` | `app.devin.ai/settings/api-keys` |
| `GITHUB_TOKEN` | `github.com/settings/tokens` — needs `repo` + `issues` scopes |
| `GITHUB_WEBHOOK_SECRET` | Any random string you choose — paste the same value into the GitHub webhook settings |
| `NGROK_AUTHTOKEN` | `dashboard.ngrok.com` → Your Authtoken |

> `.env` is listed in `.gitignore` and will never be committed.

**2. Start everything:**
```bash
docker-compose up --build
```

> To run in the background: `docker-compose up --build -d`  
> View logs at any time with `docker-compose logs -f`

**3. Register the webhook (one-time per repo):**

Watch the logs for:
```
WEBHOOK URL: https://abc123.ngrok.io/webhook/github
```

Then go to your repo → Settings → Webhooks → Add webhook:
- Payload URL: `https://abc123.ngrok.io/webhook/github`
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET` from your `.env`
- Events: select **"Issues"** and **"Issue comments"**

**4. (Optional) Provide Devin with project context:**

`context.txt` lets you prepend standing instructions to every Devin session — codebase conventions, constraints, testing requirements, or domain knowledge. It is gitignored so each deployment can have its own without affecting the committed template.

```bash
cp context.example.txt context.txt
```

Then edit `context.txt` with whatever context is relevant. For example, for security vulnerability work:

```
You are fixing a verified security vulnerability in a Python web application.

Key facts about this codebase:
- Never interpolate user input into SQL strings — always use parameterized queries.
- All user-supplied data must be treated as untrusted at every layer.
- Fixes must not change public API signatures or break backwards compatibility.

When making changes:
- Apply the minimal targeted fix — do not refactor surrounding code.
- Run the security-relevant tests (e.g. pytest tests/security/) and include results in the PR.
- If the fix requires a dependency change, flag it explicitly in the PR description.
```

Restart the container after saving — the context is loaded once at startup. You can maintain different files for different use cases (security hardening, bug fixes, performance work) and swap them before starting Docker.

**5. Open an issue — Devin starts immediately.**

### Writing effective issues

The issue body is passed directly to Devin as its instructions. The more specific you are, the better the result. Always include:

- **What to change** — the exact file, function, or behaviour to address
- **Why** — enough context for Devin to make a correct, targeted change
- **Expected outcome** — what a passing fix looks like

A vague body forces Devin to guess. A body with file paths, code snippets, and a clear description of the fix gives Devin everything it needs.

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
| `working` | Devin is actively working on the issue |
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
├── context.example.txt    # Template for Devin project context (copy to context.txt)
├── src/
│   ├── main.py            # FastAPI app, webhook handler
│   ├── devin_client.py    # Devin API wrapper
│   ├── github_client.py   # GitHub API wrapper
│   ├── session_manager.py # Orchestration + Devin prompt builder
│   ├── observability.py   # SQLite session store + metrics
│   └── retry.py           # HTTP retry helper
└── templates/
    └── dashboard.html     # Status dashboard
```
