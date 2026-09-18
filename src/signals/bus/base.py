"""The publish side of the pipeline.

Redis arrives in step 8. The interface exists now so the pipeline is written
against it from the start, and so tests can assert on what was published without
a broker running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MessageType = Literal["event.new", "event.updated", "system.status"]


@dataclass(frozen=True, slots=True)
class BusMessage:
    type: MessageType
    #: Always the full serialised event, never a patch. A client filtering at
    #: min_score never received a sub-threshold event, so a partial update would
    #: be unapplicable -- it has to be able to insert on an update.
    data: dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    async def publish(self, message: BusMessage) -> None: ...


class Subscriber(Protocol):
    def listen(self) -> AsyncIterator[BusMessage]: ...
