"""In-process pub/sub that fans alarm/site updates out to connected SSE clients.

The portal runs as a single uvicorn worker, so an in-memory bus is enough. Moving
to several workers means swapping this for Postgres LISTEN/NOTIFY or Redis.
"""

import asyncio
from dataclasses import dataclass, field


@dataclass(eq=False)
class Subscriber:
    tenant_id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))


class Bus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()

    def subscribe(self, tenant_id: int) -> Subscriber:
        sub = Subscriber(tenant_id)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, tenant_id: int, event: str, data: dict) -> None:
        for sub in list(self._subs):
            if sub.tenant_id != tenant_id:
                continue
            try:
                sub.queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # A stalled browser tab; drop it rather than block the pollers.
                self._subs.discard(sub)


bus = Bus()
