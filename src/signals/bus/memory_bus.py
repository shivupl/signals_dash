"""In-process bus. Used by tests, and by single-process dev runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .base import BusMessage


class MemoryBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []
        self._queues: list[asyncio.Queue[BusMessage]] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)
        for queue in self._queues:
            queue.put_nowait(message)

    async def listen(self) -> AsyncIterator[BusMessage]:
        queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._queues.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.remove(queue)

    def clear(self) -> None:
        self.published.clear()
