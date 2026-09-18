"""The websocket: new and updated flags, pushed as they are stored.

One hub task holds the single bus subscription and fans each message out to
every connected client, rather than opening a Redis connection per browser tab.

The socket carries no snapshot and no history. The HTTP feed is the source of
truth: a client fetches it on connect and again on every reconnect, then applies
pushed messages on top. That is what makes lossy pub/sub safe underneath.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bus.base import BusMessage, Subscriber
from ..bus.redis_bus import encode

log = logging.getLogger(__name__)
router = APIRouter()


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self, subscriber: Subscriber) -> None:
        self._task = asyncio.create_task(self._pump(subscriber))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _pump(self, subscriber: Subscriber) -> None:
        async for message in subscriber.listen():
            await self.broadcast(message)

    async def broadcast(self, message: BusMessage) -> None:
        text = encode(message)
        for client in list(self._clients):
            try:
                await client.send_text(text)
            except Exception:  # noqa: BLE001 -- one dead tab must not stop the rest
                self._clients.discard(client)

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        try:
            await websocket.send_text(encode(BusMessage(type="system.status", data={"ok": True})))
            while True:
                # Inbound frames are ignored; this just notices the disconnect.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(websocket)


hub = Hub()


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await hub.serve(websocket)
