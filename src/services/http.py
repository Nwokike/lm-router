"""Shared async HTTP client with bounded retries and redacted request logging.

trust_env=False keeps localhost gateway calls free of Windows proxy registry
surprises (httpx study). Idempotent methods retry transient statuses and ALL
transport errors (the base class covers every httpx failure shape — the old
3-type catch let RemoteProtocolError/ReadError/PoolTimeout escape with zero
retries); final retryable responses are returned to the caller, transport
errors raise. Non-idempotent methods (POST) are never replayed: a 502 after
the upstream accepted the request would duplicate it.
"""

import asyncio

import httpx

from core import constants
from core.logging import LOG

RETRY_STATUSES = (502, 503, 504)
BACKOFF = (0.2, 0.5, 1.0)
RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class HttpService:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client: httpx.AsyncClient | None = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    # House standard (Sherlock/ktv): long-lived keepalive beats
                    # re-doing a TLS handshake on every gateway probe.
                    keepalive_expiry=30.0,
                ),
                follow_redirects=True,
                trust_env=False,
                headers={"User-Agent": f"LM-Router/{constants.APP_VERSION}"},
                event_hooks={"request": [self._on_request], "response": [self._on_response]},
            )
        return self._client

    @staticmethod
    async def _on_request(request: httpx.Request) -> None:
        # DEBUG: these traces fired 2 per request x 4 retry attempts during
        # gateway start; at INFO they doubled the log-burst render load and
        # buried real errors. Failures are still logged where they're caught.
        LOG.debug("> %s %s", request.method, request.url)

    @staticmethod
    async def _on_response(response: httpx.Response) -> None:
        LOG.debug("< %s %s", response.status_code, response.request.url)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last: object = None
        # Full house schedule (0.2, 0.5, 1.0) for idempotent methods only.
        # The old loop indexed the schedule off-by-one and never applied the
        # final 1.0s step.
        attempts = 1 + len(BACKOFF) if method.upper() in RETRY_METHODS else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                # Base class of all 12 transport failures: half-dead sockets,
                # pool timeouts and protocol breaks are the classic
                # mobile-transient errors and all of them deserve retries.
                last = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last = response
            if attempt + 1 < attempts:
                await asyncio.sleep(BACKOFF[attempt])
        if isinstance(last, httpx.Response):
            return last
        raise last  # type: ignore[misc]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
