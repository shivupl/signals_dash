"""One HTTP client for every adapter.

Everything that leaves this process goes through ``SourceClient``: the declared
User-Agent, the shared token bucket, the circuit breaker, and the translation of
status codes into the retry/give-up distinction the runner understands.

The User-Agent is not politeness. SEC returns a bare 403 to requests without one,
with no hint as to why, and it is the single most common way this integration
fails.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from .clock import Clock, SystemClock
from .errors import PermanentSourceError, RateLimited, TransientSourceError
from .ratelimit import HostLimiter, Priority

SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "sec.gov"})

# SEC publishes 10 req/s. Sitting at 8 leaves headroom for the retry that would
# otherwise be the request that tips it over.
SEC_RATE = 8.0
DEFAULT_RATE = 4.0

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class SourceClient:
    """A rate-limited, breaker-guarded HTTP client keyed by host."""

    def __init__(
        self,
        user_agent: str,
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("user_agent is required: SEC returns 403 without one")
        self._clock = clock or SystemClock()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            http2=True,
            follow_redirects=True,
            transport=transport,
        )
        self._limiters: dict[str, HostLimiter] = {}

    def limiter_for(self, host: str) -> HostLimiter:
        if host not in self._limiters:
            rate = SEC_RATE if host in SEC_HOSTS else DEFAULT_RATE
            self._limiters[host] = HostLimiter(rate=rate, clock=self._clock)
        return self._limiters[host]

    async def get(
        self,
        url: str,
        *,
        priority: Priority = Priority.HIGH,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Fetch a URL, or raise a Transient/Permanent error the runner can act on."""
        host = httpx.URL(url).host
        limiter = self.limiter_for(host)

        if limiter.is_open:
            # Parked by the breaker. Raising Transient keeps this source retrying
            # on its own schedule while every other adapter carries on.
            raise TransientSourceError(f"{host}: circuit breaker open, backing off")

        async with limiter.slot(priority):
            try:
                response = await self._client.get(url, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                raise TransientSourceError(f"{host}: timeout") from exc
            except httpx.TransportError as exc:
                raise TransientSourceError(f"{host}: {exc}") from exc

        if response.status_code == 403 and host in SEC_HOSTS:
            # Almost always the User-Agent, occasionally a throttle that escalated.
            # Either way, stop hammering: continuing is how a soft block becomes a
            # 10-minute ban.
            limiter.trip()
            raise PermanentSourceError(
                f"{host} returned 403. Check SEC_USER_AGENT is set to a real "
                f"contact address; SEC rejects anonymous clients. Host parked for "
                f"10 minutes."
            )
        if response.status_code == 429:
            retry_after = _retry_after(response)
            limiter.trip(retry_after)
            raise RateLimited(f"{host}: 429", retry_after=retry_after)
        if response.status_code in RETRYABLE_STATUS:
            raise TransientSourceError(f"{host}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PermanentSourceError(f"{host}: HTTP {response.status_code} for {url}")

        limiter.reset()
        return response

    async def get_bytes(self, url: str, *, priority: Priority = Priority.HIGH) -> bytes:
        """Parsers take bytes, so this is the shape almost every caller wants."""
        return (await self.get(url, priority=priority)).content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SourceClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
