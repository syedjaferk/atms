"""In-process event bus with the semantics the design needs from Kafka.

Each subscriber group has its own ordered queue and worker, sees every matching event once
(duplicate event_ids are dropped), and a failing handler goes to `dead` instead of stopping
the group. A slow group never blocks another group.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from atms_contracts.models import Envelope

log = logging.getLogger("atms.bus")
Handler = Callable[[Envelope], Awaitable[None]]


@dataclass
class _Group:
    name: str
    types: frozenset[str] | None
    handler: Handler
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    seen: set = field(default_factory=set)
    task: asyncio.Task | None = None


class InMemoryBus:
    def __init__(self) -> None:
        self._groups: list[_Group] = []
        self._pending = 0
        self.log: list[Envelope] = []          # every event ever published, in order
        self.dead: list[tuple[str, Envelope, Exception]] = []

    def subscribe(self, group: str, types: Iterable[str] | None, handler: Handler, broadcast: bool = False) -> None:
        self._groups.append(_Group(group, frozenset(types) if types is not None else None, handler))

    def publish(self, event: Envelope, key: str | None = None) -> None:
        self.log.append(event)
        for g in self._groups:
            if g.types is None or event.type in g.types:
                self._pending += 1
                g.queue.put_nowait(event)

    def pending(self) -> bool:
        return self._pending > 0

    def start(self) -> None:
        for g in self._groups:
            if g.task is None:
                g.task = asyncio.get_running_loop().create_task(self._work(g))

    async def stop(self) -> None:
        for g in self._groups:
            if g.task:
                g.task.cancel()
        await asyncio.gather(*[g.task for g in self._groups if g.task], return_exceptions=True)
        for g in self._groups:
            g.task = None

    async def _work(self, g: _Group) -> None:
        while True:
            event = await g.queue.get()
            try:
                if event.event_id not in g.seen:
                    g.seen.add(event.event_id)
                    try:
                        await g.handler(event)
                    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the group
                        log.exception("handler %s failed on %s", g.name, event.type)
                        self.dead.append((g.name, event, exc))
            finally:
                self._pending -= 1

    async def idle(self, poll_s: float = 0.001) -> None:
        """Wait until every queued event has been handled."""
        self.start()
        while self._pending:
            await asyncio.sleep(poll_s)
