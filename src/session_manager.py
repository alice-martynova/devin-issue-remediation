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

# Sessions in pending state older than this are auto-transitioned to error.
PENDING_TIMEOUT_MINUTES = int(os.getenv("PENDING_TIMEOUT_MINUTES", "15"))


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

TERMINAL_STATUSES = {"finished", "expired", "error"}

# Map Devin API status values to our internal vocabulary.
#
# Devin's v1 API exposes two status fields:
#   - `status`: free-form string (e.g. "running") that reports whether the
#     session process is alive, but does NOT distinguish "actively thinking"
#     from "waiting for user input".
#   - `status_enum`: the authoritative lifecycle state, including `blocked`
#     when Devin is waiting for a human reply on an issue or PR.
#
# We prefer `status_enum` so `blocked` surfaces as "User Input" on
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

# Status values that are valid internal states and should pass through as-is
# when not present in _STATUS_MAP.
_KNOWN_STATUSES = {"working", "blocked", "finished", "expired", "pending", "error"}


def _normalize_status(raw: str) -> str:
    mapped = _STATUS_MAP.get(raw)
    if mapped:
        return mapped
    if raw in _KNOWN_STATUSES:
        return raw
    logger.warning("Unknown Devin status_enum %r — treating as error", raw)
    return "error"


def _extract_status(details: dict) -> str:
    """Pick the most informative status field from a Devin session response.

    `status_enum` distinguishes `blocked` (awaiting user input) from `working`;
    the legacy `status` string often reports "running" for both. Fall back to
    `status` only when `status_enum` is missing.
    """
    return details.get("status_enum") or details.get("status") or "working"


_PR_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/\d+")


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
    # Fallback: scan free-text output for a GitHub PR URL. Devin sometimes
    # reports the PR only in its session summary text rather than structured fields.
    output = details.get("output") or details.get("summary") or ""
    if isinstance(output, str):
        match = _PR_URL_RE.search(output)
        if match:
            return match.group(0)
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
            logger.info("Duplicate webhook for #%d — session already exists, skipping", issue_number)
            return None

        idempotency_key = f"issue-{issue_number}-{repo_full_name}"
        placeholder_id = f"pending:{idempotency_key}"

        # Write the issue to the DB immediately so it appears on the dashboard
        # even if the Devin API call fails (e.g. out of tokens).
        inserted = await observability.record_pending_session(
            placeholder_id=placeholder_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_user=issue_user,
            repo_full_name=repo_full_name,
        )
        if not inserted:
            logger.info("Duplicate webhook for #%d — lost race, skipping", issue_number)
            return None

        prompt = build_devin_prompt(
            issue_number, issue_title, issue_body, repo_full_name, default_branch
        )

        logger.info("Creating Devin session for #%d: %s", issue_number, issue_title)
        try:
            result = await self.devin.create_session(
                prompt=prompt,
                title=f"Fix: {issue_title[:80]}",
                tags=[f"issue-{issue_number}"],
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            error_msg = str(exc)
            await observability.set_session_error(placeholder_id, error_msg)
            logger.error(
                "Devin session FAILED  #%d \"%s\"  —  %s",
                issue_number, issue_title, error_msg,
            )
            return None

        session_id = result["session_id"]
        devin_url = result["url"]

        await observability.activate_pending_session(placeholder_id, session_id, devin_url)
        logger.info(
            "Devin session started  #%d \"%s\"  →  %s",
            issue_number, issue_title, devin_url,
        )
        return session_id

    async def _sync_pr_merge_status(self, session: dict) -> None:
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
                logger.info(
                    "PR #%d merged  →  #%d \"%s\" resolved",
                    pr_number, session["issue_number"], session.get("issue_title", ""),
                )
        except Exception as e:
            logger.error("Error checking PR merge for session %s: %s", session["session_id"], e)

    async def _check_devin_commented(self, session: dict) -> None:
        """Check GitHub to see if Devin has posted its first-touch comment.

        Runs on every poll cycle until devin_commented flips to 1, then
        never runs again for that session.
        """
        if session.get("devin_commented"):
            return
        owner, repo = session["repo_full_name"].split("/", 1)
        try:
            if await self.github.has_devin_comment(owner, repo, session["issue_number"]):
                await observability.update_devin_commented(session["session_id"])
                logger.info(
                    "Devin confirmed active  #%d \"%s\"  (first GitHub comment seen)",
                    session["issue_number"], session.get("issue_title", ""),
                )
        except Exception as e:
            logger.warning(
                "Could not check Devin comment for #%d: %s", session["issue_number"], e
            )

    async def _poll_one(self, session: dict) -> None:
        session_id = session["session_id"]

        await self._sync_pr_merge_status(session)

        # error is permanently done — skip Devin API call.
        # expired is NOT skipped: Devin can resume after a quota increase.
        # finished WITH a PR URL is done. finished WITHOUT a PR URL keeps polling
        # so the text-fallback extraction gets another chance to find it.
        if session.get("devin_status") == "error":
            return
        if session.get("devin_status") == "finished" and session.get("pr_url"):
            return

        await self._check_devin_commented(session)

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

            issue_label = f"#{session['issue_number']} \"{session.get('issue_title', '')}\""
            if new_status == "finished":
                pr_display = pr_url or "no PR URL captured"
                logger.info("Devin finished   %s  →  %s", issue_label, pr_display)
            elif new_status == "blocked":
                logger.warning(
                    "Devin BLOCKED    %s  —  waiting for human reply on issue", issue_label
                )
            elif new_status == "expired":
                logger.error("Devin EXPIRED    %s  —  session timed out, no PR opened", issue_label)
            elif new_status == "error":
                logger.error("Devin ERROR      %s  —  unexpected status: %r", issue_label, raw_status)
            else:
                return

            await observability.update_notified_status(session_id, new_status)

        except Exception as e:
            logger.error("Error polling session %s: %s", session_id, e)

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
        logger.info("Relayed comment from @%s on #%d to Devin", comment_user, issue_number)

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
        logger.info("Relayed PR comment from @%s on PR #%d to Devin", comment_user, pr_number)

    async def handle_issue_closed(
        self,
        issue_number: int,
        repo_full_name: str,
    ) -> None:
        session = await observability.get_active_session_by_issue(issue_number, repo_full_name)
        if not session:
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
        logger.info("Issue #%d closed — session %s moved to archive", issue_number, session["session_id"])

    async def poll_and_update(self) -> None:
        # Three phases each tick:
        # 1. Expire stale pending rows — catches crashes between pre-write and Devin API call.
        # 2. Poll active sessions — fetch latest status and PR URL from Devin.
        # 3. Check PR merge status — handled inside _poll_one for sessions with an open PR.
        stale = await observability.get_stale_pending_sessions(PENDING_TIMEOUT_MINUTES)
        for s in stale:
            await observability.set_session_error(
                s["session_id"],
                f"Session creation timed out after {PENDING_TIMEOUT_MINUTES} minutes",
            )
            logger.error(
                "Devin NOT STARTED  #%d \"%s\"  —  pending for >%dm, marking as error",
                s["issue_number"], s.get("issue_title", ""), PENDING_TIMEOUT_MINUTES,
            )

        active = await observability.get_active_sessions()
        if not active:
            return
        logger.debug("Polling %d active session(s)", len(active))
        await asyncio.gather(*[self._poll_one(s) for s in active], return_exceptions=True)
