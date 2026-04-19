import logging
from typing import Optional

import httpx

from .retry import with_retry

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
        idempotency_key: Optional[str] = None,
    ) -> dict:
        body: dict = {
            "prompt": prompt,
            "title": title,
            "tags": tags or [],
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key

        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{DEVIN_BASE_URL}/sessions",
                    headers=self.headers,
                    json=body,
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                logger.info(f"Created Devin session {data['session_id']}")
                return data

        return await with_retry(_call)

    async def get_session(self, session_id: str) -> dict:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{DEVIN_BASE_URL}/sessions/{session_id}",
                    headers=self.headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

        return await with_retry(_call)

    async def send_message(self, session_id: str, message: str) -> dict:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{DEVIN_BASE_URL}/sessions/{session_id}/message",
                    headers=self.headers,
                    json={"message": message},
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

        return await with_retry(_call)

    async def list_sessions(self, limit: int = 100) -> list[dict]:
        async def _call():
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{DEVIN_BASE_URL}/sessions",
                    headers=self.headers,
                    params={"limit": limit},
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

        return await with_retry(_call)
