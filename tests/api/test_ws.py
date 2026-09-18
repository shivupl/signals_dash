"""The websocket hub and the Redis message codec."""

from __future__ import annotations

import asyncio

from signals.api.ws import Hub
from signals.bus.base import BusMessage
from signals.bus.memory_bus import MemoryBus
from signals.bus.redis_bus import decode, encode


class FakeSocket:
    def __init__(self, *, broken: bool = False) -> None:
        self.sent: list[str] = []
        self.broken = broken

    async def send_text(self, text: str) -> None:
        if self.broken:
            raise RuntimeError("tab closed")
        self.sent.append(text)


class TestCodec:
    def test_round_trips(self) -> None:
        message = BusMessage(type="event.new", data={"id": 7, "score": 95})
        assert decode(encode(message)) == message

    def test_garbage_is_dropped_not_raised(self) -> None:
        """One malformed message on the channel must not kill the subscription."""
        assert decode("not json") is None
        assert decode('{"no_type": 1}') is None


class TestHub:
    async def test_broadcast_reaches_every_client(self) -> None:
        hub = Hub()
        a, b = FakeSocket(), FakeSocket()
        hub._clients.update({a, b})  # type: ignore[arg-type]
        await hub.broadcast(BusMessage(type="event.new", data={"id": 1}))
        assert len(a.sent) == len(b.sent) == 1

    async def test_a_dead_client_is_dropped_and_the_rest_still_served(self) -> None:
        hub = Hub()
        dead, alive = FakeSocket(broken=True), FakeSocket()
        hub._clients.update({dead, alive})  # type: ignore[arg-type]
        await hub.broadcast(BusMessage(type="event.new", data={"id": 1}))
        assert len(alive.sent) == 1
        assert hub.client_count == 1

    async def test_messages_flow_from_the_bus_to_clients(self) -> None:
        hub, bus, client = Hub(), MemoryBus(), FakeSocket()
        hub._clients.add(client)  # type: ignore[arg-type]
        hub.start(bus)
        await asyncio.sleep(0.01)  # let the pump subscribe
        await bus.publish(BusMessage(type="event.updated", data={"id": 9, "score": 35}))
        await asyncio.sleep(0.01)
        await hub.stop()
        assert len(client.sent) == 1
        assert decode(client.sent[0]) == BusMessage(
            type="event.updated", data={"id": 9, "score": 35}
        )

    async def test_stop_is_safe_without_start(self) -> None:
        await Hub().stop()
