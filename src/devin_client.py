import logging
from typing import Optional

import httpx

DEVIN_BASE_URL = "https://api.devin.ai/v1"
logger = logging.getLogger(__name__)


class DevinClient:
    def __init__(self, api_key: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def create_session(
        self,
        prompt: str,
        title: str,
        tags: Optional[list[str]] = None,
    ) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{DEVIN_BASE_URL}/sessions",
                headers=self.headers,
                json={
                    "prompt": prompt,
                    "title": title,
                    "tags": tags or [],
                    "idempotent": False,
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Created Devin session {data['session_id']}")
            return data

    async def get_session(self, session_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{DEVIN_BASE_URL}/sessions/{session_id}",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

    async def list_sessions(self, limit: int = 100) -> list[dict]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{DEVIN_BASE_URL}/sessions",
                headers=self.headers,
                params={"limit": limit},
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
