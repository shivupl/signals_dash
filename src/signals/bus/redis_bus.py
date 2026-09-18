"""Redis pub/sub bus: the worker publishes, the API subscribes.

Pub/sub is fire-and-forget. A message published while nobody is subscribed is
gone, and a client that was disconnected misses whatever went out meanwhile.
That is acceptable here for exactly one reason: the HTTP feed is the source of
truth and the UI refetches it on every (re)connect. If that refetch is ever
removed as "redundant", this has to become Redis Streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Final

import redis.asyncio as redis

from .base import BusMessage

log = logging.getLogger(__name__)

CHANNEL: Final[str] = "signals:events"


def encode(message: BusMessage) -> str:
    return json.dumps({"type": message.type, "data": message.data}, default=str)


def decode(raw: str | bytes) -> BusMessage | None:
    try:
        body = json.loads(raw)
        return BusMessage(type=body["type"], data=body.get("data") or {})
    except (ValueError, KeyError, TypeError):
        return None


class RedisPublisher:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url)

    async def publish(self, message: BusMessage) -> None:
        try:
            await self._redis.publish(CHANNEL, encode(message))
        except redis.RedisError as exc:
            # The event is already committed to Postgres. Losing the push costs a
            # few seconds of latency on one client; raising here would cost the
            # adapter its iteration.
            log.warning("publish failed, event is stored but not pushed: %s", exc)

    async def close(self) -> None:
        await self._redis.aclose()


class RedisSubscriber:
    def __init__(self, url: str) -> None:
        self._url = url

    async def listen(self) -> AsyncIterator[BusMessage]:
        """Yield messages forever, reconnecting when Redis drops."""
        while True:
            client = redis.from_url(self._url)
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(CHANNEL)
                async for raw in pubsub.listen():
                    if raw.get("type") != "message":
                        continue
                    message = decode(raw["data"])
                    if message is not None:
                        yield message
            except redis.RedisError as exc:
                log.warning("redis subscription dropped, retrying: %s", exc)
                await asyncio.sleep(2.0)
            finally:
                await pubsub.aclose()  # type: ignore[no-untyped-call]
                await client.aclose()
