import logging

import httpx

from .retry import with_retry

GITHUB_API_URL = "https://api.github.com"
logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str):
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_authenticated_user(self) -> str:
        async def _call():
            response = await self._client.get("/user")
            response.raise_for_status()
            return response.json()["login"]

        return await with_retry(_call)

    async def post_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict:
        async def _call():
            response = await self._client.post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                json={"body": body},
            )
            response.raise_for_status()
            logger.info(f"Posted comment on {owner}/{repo}#{issue_number}")
            return response.json()

        return await with_retry(_call)

    async def is_pr_merged(self, owner: str, repo: str, pr_number: int) -> bool:
        """Returns True if the PR has been merged (GitHub returns 204), False otherwise."""
        async def _call():
            response = await self._client.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            )
            return response.status_code == 204

        return await with_retry(_call)

    async def has_devin_comment(self, owner: str, repo: str, issue_number: int) -> bool:
        """Return True if devin-ai-integration[bot] has posted on this issue."""
        page = 1
        while True:
            async def _call(_page=page):
                response = await self._client.get(
                    f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                    params={"per_page": 100, "page": _page},
                )
                response.raise_for_status()
                return response.json()

            comments = await with_retry(_call)
            if not comments:
                return False
            if any(c["user"]["login"] == "devin-ai-integration[bot]" for c in comments):
                return True
            if len(comments) < 100:
                return False
            page += 1

    async def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        async def _call():
            response = await self._client.post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
                json={"labels": labels},
            )
            response.raise_for_status()

        await with_retry(_call)
