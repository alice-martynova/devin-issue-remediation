import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


async def with_retry(coro_fn, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry an async callable on transient HTTP errors (5xx, 429) with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
            if not retryable or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "HTTP %s on attempt %d/%d — retrying in %.1fs",
                exc.response.status_code, attempt + 1, max_attempts, delay,
            )
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Transport error on attempt %d/%d (%s) — retrying in %.1fs",
                attempt + 1, max_attempts, exc, delay,
            )
            await asyncio.sleep(delay)
