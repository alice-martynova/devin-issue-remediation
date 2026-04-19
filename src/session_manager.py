import logging
import re
from typing import Optional

from .devin_client import DevinClient
from .github_client import GitHubClient
from . import observability

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"finished", "expired"}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower())[:40].strip("-")


def build_devin_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo_full_name: str,
) -> str:
    return f"""You are fixing a verified security vulnerability in the Apache Superset repository.

Repository: https://github.com/{repo_full_name}
Branch: master
Issue: #{issue_number} — {issue_title}

Vulnerability Details:
{issue_body}

Instructions:
1. Clone the repository (branch: master)
2. Create a new branch named: fix/issue-{issue_number}-{_slugify(issue_title)}
3. Apply the minimal, targeted fix described above — do not refactor or change unrelated code
4. Run the existing tests relevant to the changed file and capture the output
5. Open a pull request against master with:
   - Title: "fix: {issue_title}"
   - Body: "Fixes #{issue_number}\\n\\n[Brief description of what was changed and why]"
6. Post a follow-up comment on the PR with the test results in this format:
   ## Test Results
   **Status:** PASSED / FAILED
   **Command run:** `<the exact command used>`
   ```
   <test output>
   ```

Important: Only modify the code necessary to address this specific vulnerability.
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
        repo_full_name: str,
    ) -> Optional[str]:
        owner, repo = repo_full_name.split("/", 1)

        # Deduplicate — skip if we already have a session for this issue
        if await observability.session_exists_for_issue(issue_number, repo_full_name):
            logger.info(f"Session already exists for #{issue_number}, skipping")
            return None

        prompt = build_devin_prompt(issue_number, issue_title, issue_body, repo_full_name)

        logger.info(f"Creating Devin session for issue #{issue_number}: {issue_title}")
        result = await self.devin.create_session(
            prompt=prompt,
            title=f"Fix: {issue_title[:80]}",
            tags=[f"issue-{issue_number}", "security", "superset"],
        )

        session_id = result["session_id"]
        devin_url = result["url"]

        await observability.record_session(
            session_id=session_id,
            issue_number=issue_number,
            issue_title=issue_title,
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
                f"I'll comment here when a PR is ready."
            ),
        )

        logger.info(f"Session {session_id} started for issue #{issue_number}")
        return session_id

    async def poll_and_update(self) -> None:
        active = await observability.get_active_sessions()
        if not active:
            return

        logger.info(f"Polling {len(active)} active session(s)")

        for session in active:
            session_id = session["session_id"]
            try:
                details = await self.devin.get_session(session_id)
                new_status = details.get("status_enum") or details.get("status", "working")
                pr_url: Optional[str] = None
                if details.get("pull_request"):
                    pr_url = details["pull_request"].get("url")

                await observability.update_session(
                    session_id=session_id,
                    devin_status=new_status,
                    pr_url=pr_url,
                )

                owner, repo = session["repo_full_name"].split("/", 1)
                issue_number = session["issue_number"]

                if new_status == "finished":
                    if pr_url:
                        await self.github.post_comment(
                            owner=owner,
                            repo=repo,
                            issue_number=issue_number,
                            body=f"**Devin has opened a fix PR:** {pr_url}",
                        )
                        logger.info(f"Session {session_id} finished — PR: {pr_url}")
                    else:
                        await self.github.post_comment(
                            owner=owner,
                            repo=repo,
                            issue_number=issue_number,
                            body=(
                                f"**Devin session finished** but no PR was found.\n\n"
                                f"Review the session manually: {session['devin_session_url']}"
                            ),
                        )

                elif new_status == "blocked":
                    await self.github.post_comment(
                        owner=owner,
                        repo=repo,
                        issue_number=issue_number,
                        body=(
                            f"**Devin is blocked** and needs input.\n\n"
                            f"Review: {session['devin_session_url']}"
                        ),
                    )
                    logger.warning(f"Session {session_id} is blocked")

                elif new_status == "expired":
                    await self.github.post_comment(
                        owner=owner,
                        repo=repo,
                        issue_number=issue_number,
                        body=(
                            f"**Devin session expired** without completing.\n\n"
                            f"Manual review needed: {session['devin_session_url']}"
                        ),
                    )
                    logger.error(f"Session {session_id} expired")

            except Exception as e:
                logger.error(f"Error polling session {session_id}: {e}")
