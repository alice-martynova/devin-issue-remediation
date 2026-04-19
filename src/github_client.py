import logging

import httpx

GITHUB_API_URL = "https://api.github.com"
logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def post_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict:
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

    async def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}/labels",
                headers=self.headers,
                json={"labels": labels},
                timeout=30,
            )
            response.raise_for_status()

    async def create_label(
        self, owner: str, repo: str, name: str, color: str, description: str = ""
    ) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GITHUB_API_URL}/repos/{owner}/{repo}/labels",
                headers=self.headers,
                json={"name": name, "color": color, "description": description},
                timeout=30,
            )
            if response.status_code not in (201, 422):
                response.raise_for_status()
