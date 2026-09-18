"""One HTTP client for every adapter.

Everything that leaves this process goes through ``SourceClient``: the declared
User-Agent, the shared token bucket, the circuit breaker, and the translation of
status codes into the retry/give-up distinction the runner understands.

The User-Agent is not politeness. SEC returns a bare 403 to requests without one,
with no hint as to why, and it is the single most common way this integration
fails.
"""

from __future__ import annotations

import asyncio
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
        timeout: float = 30.0,
        http2: bool = True,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("user_agent is required: SEC returns 403 without one")
        self._clock = clock or SystemClock()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            # Generous on purpose. EDGAR's getcurrent CGI has heavy-tailed latency
            # -- measured at 0.2 s typically, with independent spikes of 10-40 s and
            # occasional phases where everything is slow. A short timeout was tried
            # and made things strictly worse: it aborted requests SEC was about to
            # answer. Slow requests are dodged by hedging instead (see `get`).
            timeout=httpx.Timeout(timeout, connect=10.0),
            http2=http2,
            follow_redirects=True,
            transport=transport,
        )
        self._limiters: dict[str, HostLimiter] = {}
        self.hedges_sent = 0
        self.hedges_won = 0

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
        hedge_after: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Fetch a URL, or raise a Transient/Permanent error the runner can act on.

        With ``hedge_after`` set, a second identical request is sent if the first
        has not answered in that many seconds, and whichever finishes first wins.
        The endpoint's slow responses are independent of one another, so the
        second attempt usually returns in a fraction of a second while the first
        is still hanging. Both draw from the rate limiter, so hedging can never
        push the client past its budget.
        """
        if hedge_after is not None:
            return await self._hedged(url, hedge_after, priority=priority, headers=headers)
        return await self._get_once(url, priority=priority, headers=headers, **kwargs)

    async def _hedged(
        self,
        url: str,
        hedge_after: float,
        *,
        priority: Priority,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        first = asyncio.create_task(self._get_once(url, priority=priority, headers=headers))
        done, _ = await asyncio.wait({first}, timeout=hedge_after)
        if done:
            return first.result()

        self.hedges_sent += 1
        second = asyncio.create_task(self._get_once(url, priority=priority, headers=headers))
        pending = {first, second}
        failure: BaseException | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        if task is second:
                            self.hedges_won += 1
                        return task.result()
                    failure = task.exception()
            assert failure is not None
            raise failure
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()

    async def _get_once(
        self,
        url: str,
        *,
        priority: Priority = Priority.HIGH,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
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

    async def get_bytes(
        self,
        url: str,
        *,
        priority: Priority = Priority.HIGH,
        hedge_after: float | None = None,
    ) -> bytes:
        """Parsers take bytes, so this is the shape almost every caller wants."""
        return (await self.get(url, priority=priority, hedge_after=hedge_after)).content

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
