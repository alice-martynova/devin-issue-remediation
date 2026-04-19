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
Devin opens PR → comments on issue with PR link
```

## Prerequisites

You need four things before running this app. Each section below explains what it is, why it's needed, and how to get it.

---

### Docker Desktop

**What it is:** Docker Desktop runs the app and ngrok tunnel in isolated containers so you don't need to install anything else locally.

**Why it's needed:** The app, its dependencies, and the ngrok tunnel all run as Docker containers. Without Docker Desktop running, none of the `docker-compose` commands will work.

**How to get it:**
1. Download from [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) and follow the install instructions for your OS.
2. Open Docker Desktop from your Applications folder (Mac) or Start menu (Windows).
3. Wait for the whale icon to appear in your menu bar (Mac) or system tray (Windows) — this means Docker is ready.

If you see `Cannot connect to the Docker daemon`, Docker Desktop is not running. Open it and wait for it to fully start before retrying.

---

### Devin API Key

**What it is:** A secret key that authenticates requests to the Devin API, allowing the app to create and monitor Devin sessions.

**Why it's needed:** Every call to the Devin API (`api.devin.ai`) must include this key. Without it, the app cannot start Devin sessions.

**How to get it:**
1. Log in to [app.devin.ai](https://app.devin.ai).
2. Go to **Settings → API Keys**.
3. Click **Create API Key**, give it a name, and copy the value.

Store this as `DEVIN_API_KEY` in your `.env` file. Keep it secret — anyone with this key can create Devin sessions on your account.

---

### GitHub Personal Access Token (PAT)

**What it is:** A token that lets the app post comments on GitHub issues and read repository information on your behalf.

**Why it's needed:** When Devin finishes working, the app posts a comment on the issue with the PR link. This comment appears under your GitHub account (the account that owns the token).

**How to get it:**
1. Go to [github.com/settings/tokens](https://github.com/settings/tokens).
2. Click **Generate new token (classic)**.
3. Give it a descriptive name (e.g. `devin-issue-remediation`).
4. Select the following scopes:
   - `repo` — full repository access (needed to post comments and read PR status)
   - `read:user` — needed to identify the bot account and prevent comment loops
5. Click **Generate token** and copy the value immediately — GitHub only shows it once.

Store this as `GITHUB_TOKEN` in your `.env` file.

---

### GitHub Webhook Secret

**What it is:** A shared secret string that GitHub includes in every webhook request, allowing the app to verify that incoming requests genuinely come from GitHub and not a third party.

**Why it's needed:** The webhook endpoint is publicly accessible via ngrok. Without signature verification, anyone who discovers the URL could send fake events. The secret ensures only GitHub-signed requests are processed.

**How to get it:** You create this yourself — it's any random string you choose. A good way to generate one:

```bash
openssl rand -hex 32
```

You'll use this same string in two places:
- As `GITHUB_WEBHOOK_SECRET` in your `.env` file
- As the **Secret** field when registering the webhook on GitHub (see Webhook Setup below)

---

### ngrok Account

**What it is:** ngrok creates a secure public HTTPS tunnel to your locally-running app so GitHub can send webhook events to it.

**Why it's needed:** GitHub webhooks require a publicly accessible HTTPS URL. ngrok provides one even when running on a local machine or behind a firewall.

**How to get it:**
1. Sign up at [dashboard.ngrok.com](https://dashboard.ngrok.com) — the free tier is sufficient.
2. Go to **Your Authtoken** in the ngrok dashboard and copy the token.

Store this as `NGROK_AUTHTOKEN` in your `.env` file.

---

## Setup

**1. Clone the repo and set up credentials:**

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and fill in each variable:

| Variable | Value |
|----------|-------|
| `DEVIN_API_KEY` | From `app.devin.ai/settings/api-keys` |
| `GITHUB_TOKEN` | From `github.com/settings/tokens` |
| `GITHUB_WEBHOOK_SECRET` | Any random string you choose (e.g. output of `openssl rand -hex 32`) |
| `NGROK_AUTHTOKEN` | From `dashboard.ngrok.com` → Your Authtoken |

> `.env` is listed in `.gitignore` and will never be committed.

**2. Start everything:**

```bash
docker-compose up --build
```

> To run in the background: `docker-compose up --build -d`
> View logs at any time with: `docker-compose logs -f`

**3. Register the webhook on GitHub:**

Once the containers are running, watch the logs for a line like:

```
WEBHOOK URL: https://abc123.ngrok.io/webhook/github
```

You can also find it at [localhost:4040](http://localhost:4040) in the ngrok inspector.

Then register the webhook on your GitHub repository:

1. Go to your repo on GitHub.
2. Click **Settings → Webhooks → Add webhook**.
3. Fill in the fields:

| Field | Value |
|-------|-------|
| Payload URL | The `https://....ngrok.io/webhook/github` URL from the logs |
| Content type | `application/json` |
| Secret | The same value as `GITHUB_WEBHOOK_SECRET` in your `.env` |
| Which events? | Select **Let me select individual events**, then check **Issues** and **Issue comments** |

4. Click **Add webhook**. GitHub will send a ping event — a green checkmark confirms it's working.

> The ngrok URL changes every time you restart Docker unless you configure a fixed domain in your ngrok account. If the URL changes, update the webhook Payload URL in GitHub.

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

Restart the containers after saving — context is loaded once at startup.

**5. Open an issue — Devin starts immediately.**

### Writing effective issues

The issue body is passed directly to Devin as its instructions. The more specific you are, the better the result. Always include:

- **What to change** — the exact file, function, or behaviour to address
- **Why** — enough context for Devin to make a correct, targeted change
- **Expected outcome** — what a passing fix looks like

A vague body forces Devin to guess. A body with file paths, code snippets, and a clear description of the fix gives Devin everything it needs.

---

## Observability

| Endpoint | Description |
|----------|-------------|
| `http://localhost:8000/dashboard` | Live status dashboard (auto-refreshes every 30s) |
| `http://localhost:8000/sessions` | Raw JSON of all sessions |
| `http://localhost:8000/health` | Health check + aggregate metrics |

## Session lifecycle

Each session moves through the three user-facing statuses shown on the dashboard:

| Dashboard status | When it's shown | What to do |
|------------------|-----------------|------------|
| **Issue Opened** | Issue received; the Devin session has been created but hasn't started running yet. | Nothing — Devin will pick it up shortly. |
| **Devin Working** | Devin is actively investigating the issue, editing code, or opening a PR. | Nothing — wait for Devin to finish or ask for input. |
| **GitHub User Action** | Devin is waiting on you: either it's blocked and asking a question on the issue, it has opened a PR that needs review, or the session finished/expired and needs a manual look. | Open the linked issue or PR and respond — comments you leave there are relayed back into the Devin session automatically. |

## Project structure

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt   # Test + lint dependencies
├── pyproject.toml         # pytest + ruff config
├── .env.example
├── context.example.txt    # Template for Devin project context (copy to context.txt)
├── .github/workflows/
│   └── ci.yml             # Runs ruff + pytest on every PR
├── src/
│   ├── main.py            # FastAPI app, webhook handler
│   ├── devin_client.py    # Devin API wrapper
│   ├── github_client.py   # GitHub API wrapper
│   ├── session_manager.py # Orchestration + Devin prompt builder
│   ├── observability.py   # SQLite session store + metrics
│   └── retry.py           # HTTP retry helper
├── templates/
│   └── dashboard.html     # Status dashboard
└── tests/                 # Unit tests — run with `pytest`
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI (`.github/workflows/ci.yml`) runs both on every PR and push to `main`.
