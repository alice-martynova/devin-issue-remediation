import logging
from typing import Optional

import httpx

from .retry import with_retry

DEVIN_BASE_URL = "https://api.devin.ai/v1"
logger = logging.getLogger(__name__)


class DevinClient:
    def __init__(self, api_key: str):
        self._client = httpx.AsyncClient(
            base_url=DEVIN_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
            response = await self._client.post("/sessions", json=body)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Created Devin session {data['session_id']}")
            return data

        return await with_retry(_call)

    async def get_session(self, session_id: str) -> dict:
        async def _call():
            response = await self._client.get(f"/sessions/{session_id}")
            response.raise_for_status()
            return response.json()

        return await with_retry(_call)

    async def send_message(self, session_id: str, message: str) -> dict:
        async def _call():
            response = await self._client.post(
                f"/sessions/{session_id}/message",
                json={"message": message},
            )
            response.raise_for_status()
            return response.json()

        return await with_retry(_call)

    async def list_sessions(self, limit: int = 100) -> list[dict]:
        async def _call():
            response = await self._client.get("/sessions", params={"limit": limit})
            response.raise_for_status()
            return response.json()

        return await with_retry(_call)
