import asyncio
import logging
import os
import re
from typing import Optional

from . import observability, prompt_sanitizer
from .devin_client import DevinClient
from .github_client import GitHubClient

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

    lines = [line for line in raw.splitlines() if not line.startswith("#")]
    return "\n".join(lines).strip()


_PROJECT_CONTEXT = _load_context()

TERMINAL_STATUSES = {"finished", "expired"}

# Map Devin API status values to our internal vocabulary.
#
# Devin's v1 API exposes two status fields:
#   - `status`: free-form string (e.g. "running") that reports whether the
#     session process is alive, but does NOT distinguish "actively thinking"
#     from "waiting for user input".
#   - `status_enum`: the authoritative lifecycle state, including `blocked`
#     when Devin is waiting for a human reply on an issue or PR.
#
# We prefer `status_enum` so `blocked` surfaces as "User Action" on
# the dashboard instead of being collapsed into "Devin Working".
_STATUS_MAP: dict[str, str] = {
    # Legacy free-form `status` field values
    "running":   "working",
    "stopped":   "finished",
    "suspended": "expired",
    # Transient `status_enum` values — Devin is transitioning, still active
    "suspend_requested":          "working",
    "suspend_requested_frontend": "working",
    "resume_requested":           "working",
    "resume_requested_frontend":  "working",
    "resumed":                    "working",
}


def _normalize_status(raw: str) -> str:
    return _STATUS_MAP.get(raw, raw)


def _extract_status(details: dict) -> str:
    """Pick the most informative status field from a Devin session response.

    `status_enum` distinguishes `blocked` (awaiting user input) from `working`;
    the legacy `status` string often reports "running" for both. Fall back to
    `status` only when `status_enum` is missing.
    """
    return details.get("status_enum") or details.get("status") or "working"


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
    default_branch: str,
) -> str:
    context_block = f"{_PROJECT_CONTEXT}\n\n" if _PROJECT_CONTEXT else ""
    return f"""{context_block}You are resolving a GitHub issue in the following repository.

Repository: https://github.com/{repo_full_name}
Branch: {default_branch}
Issue: #{issue_number} — {issue_title}

Issue Details:
{issue_body}

Instructions:
1. Before doing anything else, post a comment on issue #{issue_number} in {repo_full_name} \
saying "**Working on this.** I'll comment here again when a PR is ready." Use your GitHub \
integration — the comment must be posted by you (the `devin-ai-integration[bot]` identity), \
not by any other account.
2. Use your GitHub integration to access the repository — do not manually clone via the command line.
3. Check out branch: {default_branch}
4. Create a new branch named: fix/issue-{issue_number}-{_slugify(issue_title)}
5. Apply the minimal, targeted change described above — do not refactor or change unrelated code.
6. Run the existing tests relevant to the changed file and capture the output.
7. Open a pull request against {default_branch} with:
   - Title: "fix: {issue_title}"
   - Body: "Fixes #{issue_number}\\n\\n[Brief description of what was changed and why]"
8. Post a follow-up comment on the PR with the test results in this format:
   ## Test Results
   **Status:** PASSED / FAILED
   **Command run:** `<the exact command used>`
   ```
   <test output>
   ```
9. Post a final comment on issue #{issue_number} with a link to the PR: \
"**Opened fix PR:** <pr_url>". This closes the loop for the reporter, who is \
watching the issue, not the PR.

Asking for input: If at any point you need a human decision before you can continue — including \
ambiguous requirements, missing credentials, a design choice between valid approaches, the issue \
appearing to already be fixed / a duplicate / not reproducible, or any other precondition in this \
prompt turning out to be false — you MUST post your specific question as a comment on GitHub issue \
#{issue_number} using the GitHub integration, and then wait for the reply there. Do NOT ask the \
question by sending a message in this Devin session (message_user, block_on_user, user_question, \
etc.) — the issue reporter does not see this session's chat and will not be notified. The reporter \
is only notified when you comment on the issue — do not wait silently in this session. Phrase the \
question so it can be answered inline, and include any relevant evidence (file paths, commit SHAs, \
links) so the reviewer has full context. When they reply, their comment will be relayed back into \
this session automatically and you can continue from where you stopped.

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
        default_branch: str,
    ) -> Optional[str]:
        owner, repo = repo_full_name.split("/", 1)

        if await observability.session_exists_for_issue(issue_number, repo_full_name):
            logger.info(f"Session already exists for #{issue_number}, skipping")
            return None

        prompt = build_devin_prompt(
            issue_number, issue_title, issue_body, repo_full_name, default_branch
        )
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

        # record_session returns False if another concurrent webhook delivery
        # already claimed this issue (via the partial unique index on
        # (issue_number, repo_full_name)). When that happens Devin has
        # returned the same session_id thanks to our idempotency_key, so we
        # just skip the first-touch GitHub comment to avoid posting twice.
        inserted = await observability.record_session(
            session_id=session_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_user=issue_user,
            repo_full_name=repo_full_name,
            devin_session_url=devin_url,
        )

        if not inserted:
            logger.info(
                "Duplicate webhook delivery for issue #%d — skipping",
                issue_number,
            )
            return session_id

        # Devin itself posts the first-touch "working on this" comment from
        # inside the session (see the prompt), so the orchestrator does not
        # post from its own GitHub identity here.

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

        await self._check_pr_merged(session)

        if session.get("devin_status") in TERMINAL_STATUSES:
            return

        try:
            details = await self.devin.get_session(session_id)
            raw_status = _extract_status(details)
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

            # All user-facing comments on the issue are posted by Devin
            # itself from inside the session (first-touch, PR-ready, blocked
            # questions) so they render as `devin-ai-integration[bot]` and
            # stay distinguishable from human replies. The orchestrator only
            # records that it has observed each state so the dashboard can
            # surface it and we don't re-log it every poll cycle.
            if new_status == "finished":
                logger.info(f"Session {session_id} finished — PR: {pr_url}")
            elif new_status == "blocked":
                logger.warning(f"Session {session_id} is blocked — Devin will comment directly")
            elif new_status == "expired":
                logger.error(f"Session {session_id} expired")
            else:
                return

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
        message = prompt_sanitizer.sanitize_relay(
            source=f"GitHub issue #{issue_number} in {repo_full_name}",
            commenter=comment_user,
            body=comment_body,
        )
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
        message = prompt_sanitizer.sanitize_relay(
            source=f"GitHub PR #{pr_number} in {repo_full_name}",
            commenter=comment_user,
            body=comment_body,
        )
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
