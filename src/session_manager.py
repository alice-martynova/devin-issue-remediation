import asyncio
import logging
import os
import re
from typing import Optional

from .devin_client import DevinClient
from .github_client import GitHubClient
from . import observability

logger = logging.getLogger(__name__)

_CONTEXT_FILE = os.getenv("DEVIN_CONTEXT_FILE", "/config/context.txt")
_CONTEXT_ENV  = os.getenv("DEVIN_CONTEXT", "").strip()


def _load_context() -> str:
    """Return project context to prepend to every Devin prompt.

    Reads from DEVIN_CONTEXT_FILE if present, falls back to the DEVIN_CONTEXT
    env var, and strips comment lines (starting with #).
    """
    raw = ""
    try:
        with open(_CONTEXT_FILE) as f:
            raw = f.read()
    except OSError:
        raw = _CONTEXT_ENV

    lines = [l for l in raw.splitlines() if not l.startswith("#")]
    return "\n".join(lines).strip()


_PROJECT_CONTEXT = _load_context()

TERMINAL_STATUSES = {"finished", "expired"}

# Human-readable labels for the dashboard and GitHub comments
STATUS_LABELS = {
    "working":  "Devin at work",
    "blocked":  "Needs user input",
    "finished": "Issue addressed",
    "expired":  "Timed out",
}

# Map Devin API status values to our internal vocabulary.
_STATUS_MAP: dict[str, str] = {
    "running":   "working",
    "stopped":   "finished",
    "suspended": "expired",
}


def _normalize_status(raw: str) -> str:
    return _STATUS_MAP.get(raw, raw)


def _extract_pr_url(details: dict) -> Optional[str]:
    pr = details.get("pull_request")
    if isinstance(pr, dict):
        url = pr.get("html_url") or pr.get("url")
        if url:
            return url
    structured = details.get("structured_output")
    if isinstance(structured, dict):
        url = (
            structured.get("pr_url")
            or structured.get("pull_request_url")
            or structured.get("pull_request", {}).get("html_url")
            or structured.get("pull_request", {}).get("url")
        )
        if url:
            return url
    return None


def _extract_pr_number(pr_url: str) -> Optional[int]:
    match = re.search(r"/pull/(\d+)", pr_url)
    return int(match.group(1)) if match else None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower())[:40].strip("-")


def build_devin_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo_full_name: str,
) -> str:
    context_block = f"{_PROJECT_CONTEXT}\n\n" if _PROJECT_CONTEXT else ""
    return f"""{context_block}You are resolving a GitHub issue in the following repository.

Repository: https://github.com/{repo_full_name}
Branch: master
Issue: #{issue_number} — {issue_title}

Issue Details:
{issue_body}

Instructions:
1. Use your GitHub integration to access the repository — do not manually clone via the command line.
2. Check out branch: master
3. Create a new branch named: fix/issue-{issue_number}-{_slugify(issue_title)}
4. Apply the minimal, targeted change described above — do not refactor or change unrelated code.
5. Run the existing tests relevant to the changed file and capture the output.
6. Open a pull request against master with:
   - Title: "fix: {issue_title}"
   - Body: "Fixes #{issue_number}\\n\\n[Brief description of what was changed and why]"
7. Post a follow-up comment on the PR with the test results in this format:
   ## Test Results
   **Status:** PASSED / FAILED
   **Command run:** `<the exact command used>`
   ```
   <test output>
   ```

Important: Only modify the code necessary to address this specific issue.
"""


class SessionManager:
    def __init__(self, devin_client: DevinClient, github_client: GitHubClient):
        self.devin = devin_client
        self.github = github_client

    async def handle_issue_opened(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        issue_user: str,
        repo_full_name: str,
    ) -> Optional[str]:
        owner, repo = repo_full_name.split("/", 1)

        if await observability.session_exists_for_issue(issue_number, repo_full_name):
            logger.info(f"Session already exists for #{issue_number}, skipping")
            return None

        prompt = build_devin_prompt(issue_number, issue_title, issue_body, repo_full_name)
        idempotency_key = f"issue-{issue_number}-{repo_full_name}"

        logger.info(f"Creating Devin session for issue #{issue_number}: {issue_title}")
        result = await self.devin.create_session(
            prompt=prompt,
            title=f"Fix: {issue_title[:80]}",
            tags=[f"issue-{issue_number}"],
            idempotency_key=idempotency_key,
        )

        session_id = result["session_id"]
        devin_url = result["url"]

        await observability.record_session(
            session_id=session_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_user=issue_user,
            repo_full_name=repo_full_name,
            devin_session_url=devin_url,
        )

        await self.github.post_comment(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            body=(
                f"**Devin is working on this.**\n\n"
                f"Session: {devin_url}\n\n"
                f"Devin will comment here when a PR is ready."
            ),
        )

        logger.info(f"Session {session_id} started for issue #{issue_number}")
        return session_id

    async def _check_pr_merged(self, session: dict) -> None:
        pr_url = session.get("pr_url")
        if not pr_url or session.get("pr_merged"):
            return
        owner, repo = session["repo_full_name"].split("/", 1)
        pr_number = _extract_pr_number(pr_url)
        if not pr_number:
            return
        try:
            merged = await self.github.is_pr_merged(owner, repo, pr_number)
            if merged:
                await observability.update_pr_merged(session["session_id"])
                logger.info(f"PR #{pr_number} merged for issue #{session['issue_number']}")
        except Exception as e:
            logger.error(f"Error checking PR merge for session {session['session_id']}: {e}")

    async def _poll_one(self, session: dict) -> None:
        session_id = session["session_id"]
        owner, repo = session["repo_full_name"].split("/", 1)
        issue_number = session["issue_number"]

        await self._check_pr_merged(session)

        if session.get("devin_status") in TERMINAL_STATUSES:
            return

        try:
            details = await self.devin.get_session(session_id)
            raw_status = details.get("status", "working")
            new_status = _normalize_status(raw_status)
            pr_url = _extract_pr_url(details)

            await observability.update_session(
                session_id=session_id,
                devin_status=new_status,
                pr_url=pr_url,
            )

            last_notified = session.get("last_notified_status")
            if new_status == last_notified:
                return

            label = STATUS_LABELS.get(new_status, new_status)

            if new_status == "finished":
                body = (
                    f"**{label}.** Devin has opened a fix PR: {pr_url}"
                    if pr_url
                    else f"**{label}.** No PR was found. Review manually: {session['devin_session_url']}"
                )
                logger.info(f"Session {session_id} finished — PR: {pr_url}")
            elif new_status == "blocked":
                # Devin posts its own comment on the issue asking for input — no need to duplicate it
                logger.warning(f"Session {session_id} is blocked — Devin will comment directly")
                await observability.update_notified_status(session_id, new_status)
                return
            elif new_status == "expired":
                body = f"**{label}.** Session expired without completing.\n\nManual review needed: {session['devin_session_url']}"
                logger.error(f"Session {session_id} expired")
            else:
                return

            await self.github.post_comment(
                owner=owner, repo=repo, issue_number=issue_number, body=body
            )
            await observability.update_notified_status(session_id, new_status)

        except Exception as e:
            logger.error(f"Error polling session {session_id}: {e}")

    async def handle_issue_comment(
        self,
        issue_number: int,
        comment_body: str,
        comment_user: str,
        repo_full_name: str,
    ) -> None:
        session = await observability.get_active_session_by_issue(issue_number, repo_full_name)
        if not session:
            return

        session_id = session["session_id"]
        message = f"{comment_user} replied on GitHub:\n\n{comment_body}"
        await self.devin.send_message(session_id, message)
        logger.info(f"Relayed comment from {comment_user} to Devin session {session_id}")

    async def handle_pr_comment(
        self,
        pr_number: int,
        comment_body: str,
        comment_user: str,
        repo_full_name: str,
    ) -> None:
        session = await observability.get_session_by_pr_number(pr_number, repo_full_name)
        if not session:
            return
        session_id = session["session_id"]
        message = f"{comment_user} commented on the PR:\n\n{comment_body}"
        await self.devin.send_message(session_id, message)
        logger.info(f"Relayed PR comment from {comment_user} to Devin session {session_id}")

    async def handle_issue_closed(
        self,
        issue_number: int,
        repo_full_name: str,
    ) -> None:
        session = await observability.get_active_session_by_issue(issue_number, repo_full_name)
        if not session:
            # Also check terminal sessions so a closed finished/expired session is archived
            async def _find_any():
                all_sessions = await observability.get_all_sessions()
                for s in all_sessions:
                    if s["issue_number"] == issue_number and s["repo_full_name"] == repo_full_name:
                        return s
                return None
            session = await _find_any()
        if not session:
            return
        await observability.update_issue_closed(session["session_id"])
        logger.info(f"Issue #{issue_number} closed — session {session['session_id']} moved to archive")

    async def poll_and_update(self) -> None:
        active = await observability.get_active_sessions()
        if not active:
            return
        logger.info(f"Polling {len(active)} active session(s)")
        await asyncio.gather(*[self._poll_one(s) for s in active], return_exceptions=True)
