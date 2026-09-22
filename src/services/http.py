"""Shared async HTTP client with bounded retries and redacted request logging.

trust_env=False keeps localhost gateway calls free of Windows proxy registry
surprises (httpx study). Retries cover only idempotent-ish transient statuses;
final retryable responses are returned to the caller, transport errors raise.
"""

import asyncio

import httpx

from core import constants
from core.logging import LOG

RETRY_STATUSES = (502, 503, 504)
BACKOFF = (0.2, 0.5, 1.0)


class HttpService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                trust_env=False,
                headers={"User-Agent": f"LM-Router/{constants.APP_VERSION}"},
                event_hooks={"request": [self._on_request], "response": [self._on_response]},
            )
        return self._client

    @staticmethod
    async def _on_request(request: httpx.Request) -> None:
        LOG.info("> %s %s", request.method, request.url)

    @staticmethod
    async def _on_response(response: httpx.Response) -> None:
        LOG.info("< %s %s", response.status_code, response.request.url)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last: object = None
        for attempt, delay in enumerate((0.0, *BACKOFF)):
            try:
                response = await self.client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last = response
            if attempt < len(BACKOFF):
                await asyncio.sleep(delay)
        if isinstance(last, httpx.Response):
            return last
        raise last  # type: ignore[misc]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
