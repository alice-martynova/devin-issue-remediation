import logging

import httpx

from .retry import with_retry

GITHUB_API_URL = "https://api.github.com"
logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_authenticated_user(self) -> str:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{GITHUB_API_URL}/user",
                    headers=self.headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()["login"]

        return await with_retry(_call)

    async def post_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments",
                    headers=self.headers,
                    json={"body": body},
                    timeout=30,
                )
                response.raise_for_status()
                logger.info(f"Posted comment on {owner}/{repo}#{issue_number}")
                return response.json()

        return await with_retry(_call)

    async def is_pr_merged(self, owner: str, repo: str, pr_number: int) -> bool:
        """Returns True if the PR has been merged (GitHub returns 204), False otherwise."""
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                    headers=self.headers,
                    timeout=30,
                )
                return response.status_code == 204

        return await with_retry(_call)

    async def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}/labels",
                    headers=self.headers,
                    json={"labels": labels},
                    timeout=30,
                )
                response.raise_for_status()

        await with_retry(_call)
