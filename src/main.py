import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager

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

DEVIN_API_KEY = os.environ["DEVIN_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

devin_client = DevinClient(api_key=DEVIN_API_KEY)
github_client = GitHubClient(token=GITHUB_TOKEN)
session_manager = SessionManager(devin_client=devin_client, github_client=github_client)
templates = Jinja2Templates(directory="templates")


async def _background_poller() -> None:
    while True:
        try:
            await session_manager.poll_and_update()
        except Exception as exc:
            logger.error(f"Poller error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


async def _print_ngrok_url() -> None:
    """Wait for ngrok to start, then log the public webhook URL."""
    for attempt in range(15):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("http://ngrok:4040/api/tunnels", timeout=5)
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
        except Exception:
            pass
        await asyncio.sleep(3)
    logger.warning(
        "Could not auto-detect ngrok URL. Check http://localhost:4040 manually."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await observability.init_db()
    asyncio.create_task(_background_poller())
    asyncio.create_task(_print_ngrok_url())
    yield


app = FastAPI(
    title="Devin Vulnerability Remediation",
    description="Event-driven automation that triggers Devin to fix security issues.",
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

    if GITHUB_WEBHOOK_SECRET:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(payload_bytes, sig, GITHUB_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    action = payload.get("action", "")

    if event != "issues" or action != "opened":
        return {"status": "ignored", "event": event, "action": action}

    issue = payload["issue"]
    repo = payload["repository"]

    asyncio.create_task(
        session_manager.handle_issue_opened(
            issue_number=issue["number"],
            issue_title=issue["title"],
            issue_body=issue.get("body") or "",
            repo_full_name=repo["full_name"],
        )
    )

    logger.info(f"Accepted issue #{issue['number']}: {issue['title']}")
    return {"status": "accepted", "issue_number": issue["number"]}


@app.get("/health")
async def health():
    metrics = await observability.get_metrics()
    return {"status": "ok", **metrics}


@app.get("/sessions")
async def list_sessions():
    return await observability.get_all_sessions()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    sessions = await observability.get_all_sessions()
    metrics = await observability.get_metrics()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "sessions": sessions, "metrics": metrics},
    )
