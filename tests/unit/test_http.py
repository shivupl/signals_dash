"""HTTP client behaviour, against a mock transport -- no network."""

from __future__ import annotations

import httpx
import pytest

from signals.errors import PermanentSourceError, RateLimited, TransientSourceError
from signals.http import SEC_RATE, SourceClient
from signals.ratelimit import Priority

UA = "Signals/0.1 (test@example.com)"


def transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


class TestUserAgent:
    def test_blank_user_agent_is_rejected_at_construction(self) -> None:
        """Fail here, not with an unexplained 403 mid-session."""
        with pytest.raises(ValueError, match="403"):
            SourceClient("   ")

    async def test_user_agent_is_sent_on_every_request(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["User-Agent"])
            return httpx.Response(200, content=b"ok")

        async with SourceClient(UA, transport=transport(handler)) as client:
            await client.get_bytes("https://www.sec.gov/a")
            await client.get_bytes("https://www.sec.gov/b")

        assert seen == [UA, UA]


class TestStatusHandling:
    async def test_success_returns_bytes(self) -> None:
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(200, content=b"<feed/>"))
        ) as client:
            assert await client.get_bytes("https://www.sec.gov/x") == b"<feed/>"

    async def test_sec_403_is_permanent_and_names_the_cause(self) -> None:
        """The most common EDGAR failure, so the message has to say what to fix."""
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(403))
        ) as client:
            with pytest.raises(PermanentSourceError, match="SEC_USER_AGENT"):
                await client.get_bytes("https://www.sec.gov/x")

    async def test_sec_403_trips_the_breaker(self) -> None:
        """Continuing to poll after a block is how it becomes a 10-minute ban."""
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(403))
        ) as client:
            with pytest.raises(PermanentSourceError):
                await client.get_bytes("https://www.sec.gov/x")
            assert client.limiter_for("www.sec.gov").is_open is True
            # Subsequent calls are refused locally, without touching the network.
            with pytest.raises(TransientSourceError, match="circuit breaker"):
                await client.get_bytes("https://www.sec.gov/y")

    async def test_429_carries_retry_after(self) -> None:
        async with SourceClient(
            UA,
            transport=transport(lambda r: httpx.Response(429, headers={"Retry-After": "30"})),
        ) as client:
            with pytest.raises(RateLimited) as caught:
                await client.get_bytes("https://www.sec.gov/x")
            assert caught.value.retry_after == 30.0

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
    async def test_server_errors_are_transient(self, status: int) -> None:
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(status))
        ) as client:
            with pytest.raises(TransientSourceError):
                await client.get_bytes("https://www.sec.gov/x")

    async def test_404_is_permanent(self) -> None:
        """Retrying a missing document forever is just noise."""
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(404))
        ) as client:
            with pytest.raises(PermanentSourceError):
                await client.get_bytes("https://www.sec.gov/missing")

    async def test_timeout_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        async with SourceClient(UA, transport=transport(handler)) as client:
            with pytest.raises(TransientSourceError, match="timeout"):
                await client.get_bytes("https://www.sec.gov/x")

    async def test_a_success_clears_an_earlier_breaker(self) -> None:
        responses = [httpx.Response(503), httpx.Response(200, content=b"ok")]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        async with SourceClient(UA, transport=transport(handler)) as client:
            with pytest.raises(TransientSourceError):
                await client.get_bytes("https://www.sec.gov/x")
            assert await client.get_bytes("https://www.sec.gov/x") == b"ok"
            assert client.limiter_for("www.sec.gov").is_open is False


class TestLimiters:
    async def test_sec_hosts_share_the_sec_rate(self) -> None:
        """www.sec.gov and data.sec.gov are one client to SEC, but the ceiling is
        per host here; what matters is neither gets the laxer default."""
        async with SourceClient(UA) as client:
            assert client.limiter_for("www.sec.gov")._bucket.rate == SEC_RATE
            assert client.limiter_for("data.sec.gov")._bucket.rate == SEC_RATE

    async def test_other_hosts_get_the_conservative_default(self) -> None:
        async with SourceClient(UA) as client:
            assert client.limiter_for("www.nasdaqtrader.com")._bucket.rate < SEC_RATE

    async def test_limiter_is_reused_per_host(self) -> None:
        async with SourceClient(UA) as client:
            assert client.limiter_for("www.sec.gov") is client.limiter_for("www.sec.gov")

    async def test_priority_is_accepted_end_to_end(self) -> None:
        async with SourceClient(
            UA, transport=transport(lambda r: httpx.Response(200, content=b"ok"))
        ) as client:
            assert await client.get_bytes("https://www.sec.gov/x", priority=Priority.LOW) == b"ok"
