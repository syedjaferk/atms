"""Audit sink. Records every event with its trace id."""
from __future__ import annotations

from atms_contracts.models import Envelope

from .store import Store


class AuditSink:
    def __init__(self, store: Store):
        self.store = store

    async def on_event(self, ev: Envelope) -> None:
        self.store.record_audit({"event_id": str(ev.event_id), "type": ev.type,
                                 "incident_id": str(ev.incident_id) if ev.incident_id else None,
                                 "producer": ev.producer, "at": ev.occurred_at.isoformat(), "trace_id": ev.trace_id})
        self.store.commit()
