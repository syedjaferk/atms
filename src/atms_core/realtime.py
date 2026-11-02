"""Realtime gateway. Reads the bus, pushes incident deltas to connected dashboards. Holds no state of its own."""
from __future__ import annotations

import asyncio

from atms_contracts.models import Envelope

from .store import Store
from .views import incident_view


class RealtimeGateway:
    def __init__(self, store: Store):
        self.store = store
        self._clients: set[asyncio.Queue] = set()

    def connect(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def disconnect(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def on_event(self, ev: Envelope) -> None:
        inc = self.store.incidents.get(ev.incident_id) if ev.incident_id else None
        if inc is None or not self._clients:
            return
        message = {"incident_id": str(inc.id), "version": inc.version, "event_type": ev.type,
                   "incident": incident_view(inc).model_dump(mode="json")}
        for q in list(self._clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:          # a stuck client must not hold the gateway back, it will resync
                self._clients.discard(q)
