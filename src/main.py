import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import observability
from .devin_client import DevinClient
from .github_client import GitHubClient
from .session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "Copy .env.example to .env and fill in all values."
        )
    return value


DEVIN_API_KEY = _require_env("DEVIN_API_KEY")
GITHUB_TOKEN = _require_env("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET = _require_env("GITHUB_WEBHOOK_SECRET")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT_SECONDS", "120"))
# URL of ngrok's local API; empty string disables auto-detection
NGROK_API_URL = os.getenv("NGROK_API_URL", "http://ngrok:4040").strip()

devin_client = DevinClient(api_key=DEVIN_API_KEY)
github_client = GitHubClient(token=GITHUB_TOKEN)
session_manager = SessionManager(devin_client=devin_client, github_client=github_client)
templates = Jinja2Templates(directory="templates")


async def _background_poller() -> None:
    while True:
        try:
            await asyncio.wait_for(
                session_manager.poll_and_update(),
                timeout=POLL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"Poll cycle exceeded {POLL_TIMEOUT}s timeout — skipping")
        except Exception as exc:
            logger.error(f"Poller error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


async def _print_ngrok_url() -> None:
    """Wait for ngrok to start, then log the public webhook URL.

    Skipped entirely when NGROK_API_URL is unset — useful when running outside
    the docker-compose stack (e.g. behind a real reverse proxy in production).
    """
    if not NGROK_API_URL:
        return
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(15):
            try:
                resp = await client.get(f"{NGROK_API_URL}/api/tunnels")
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        url = tunnel["public_url"]
                        logger.info(
                            "\n"
                            + "=" * 60
                            + f"\n  WEBHOOK URL: {url}/webhook/github"
                            + "\n  Add this in: GitHub repo → Settings → Webhooks"
                            + "\n" + "=" * 60
                        )
                        return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
    logger.warning(
        "Could not auto-detect ngrok URL at %s. Check http://localhost:4040 manually.",
        NGROK_API_URL,
    )


_bot_github_user: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_github_user
    await observability.init_db()
    _bot_github_user = await github_client.get_authenticated_user()
    logger.info(f"Bot GitHub user: {_bot_github_user}")
    asyncio.create_task(_background_poller())
    asyncio.create_task(_print_ngrok_url())
    try:
        yield
    finally:
        await devin_client.aclose()
        await github_client.aclose()


app = FastAPI(
    title="Devin Vulnerability Remediation",
    description="Event-driven automation that triggers Devin to resolve GitHub issues.",
    version="1.0.0",
    lifespan=lifespan,
)


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/github", status_code=202)
async def github_webhook(request: Request):
    payload_bytes = await request.body()

    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(payload_bytes, sig, GITHUB_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    action = payload.get("action", "")

    if event == "issues" and action == "opened":
        issue = payload["issue"]
        repo = payload["repository"]
        asyncio.create_task(
            session_manager.handle_issue_opened(
                issue_number=issue["number"],
                issue_title=issue["title"],
                issue_body=issue.get("body") or "",
                issue_user=issue["user"]["login"],
                repo_full_name=repo["full_name"],
                default_branch=repo.get("default_branch") or "main",
            )
        )
        logger.info(f"Accepted issue #{issue['number']}: {issue['title']}")
        return {"status": "accepted", "issue_number": issue["number"]}

    if event == "issues" and action == "closed":
        issue = payload["issue"]
        repo = payload["repository"]
        asyncio.create_task(
            session_manager.handle_issue_closed(
                issue_number=issue["number"],
                repo_full_name=repo["full_name"],
            )
        )
        logger.info(f"Issue #{issue['number']} closed — archiving session")
        return {"status": "accepted", "issue_number": issue["number"]}

    if event == "issue_comment" and action == "created":
        comment = payload["comment"]
        commenter = comment["user"]["login"]
        if commenter == _bot_github_user or commenter == "devin-ai-integration[bot]":
            return {"status": "ignored", "reason": "bot_comment"}
        issue = payload["issue"]
        repo = payload["repository"]
        is_pr_comment = "pull_request" in issue
        if is_pr_comment:
            asyncio.create_task(
                session_manager.handle_pr_comment(
                    pr_number=issue["number"],
                    comment_body=comment["body"],
                    comment_user=commenter,
                    repo_full_name=repo["full_name"],
                )
            )
            logger.info(f"Relaying PR comment from {commenter} on PR #{issue['number']}")
        else:
            asyncio.create_task(
                session_manager.handle_issue_comment(
                    issue_number=issue["number"],
                    comment_body=comment["body"],
                    comment_user=commenter,
                    repo_full_name=repo["full_name"],
                )
            )
            logger.info(f"Relaying comment from {commenter} on issue #{issue['number']}")
        return {"status": "accepted", "issue_number": issue["number"]}

    return {"status": "ignored", "event": event, "action": action}


@app.get("/health")
async def health():
    metrics = await observability.get_metrics()
    return {"status": "ok", **metrics}


@app.get("/sessions")
async def list_sessions():
    return await observability.get_all_sessions()


def _session_age(session: dict) -> tuple[int, str]:
    """Return (age_minutes, human-readable string) for a session."""
    try:
        start = datetime.fromisoformat(session["created_at"])
        end_raw = session.get("updated_at") if session["devin_status"] in {"finished", "expired"} else None
        end = datetime.fromisoformat(end_raw) if end_raw else datetime.utcnow()
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Could not compute age for session %s: %s", session.get("session_id"), exc)
        return 0, "—"
    minutes = max(0, int((end - start).total_seconds() / 60))
    if minutes < 60:
        return minutes, f"{minutes}m"
    return minutes, f"{minutes // 60}h {minutes % 60}m"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    sessions = await observability.get_all_sessions()
    metrics = await observability.get_metrics()
    for s in sessions:
        s["age_minutes"], s["age_display"] = _session_age(s)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "sessions": sessions, "metrics": metrics},
    )
