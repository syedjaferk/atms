"""Persistence contract for the Store, plus the in-memory implementation.

The Store keeps the working set in memory (one writer process) and writes every change through
this interface in one transaction per operation. Postgres is in pg.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from atms_contracts.models import Envelope

    from .store import Assignment, Contact, Incident, Notification, Plan


class StaleWrite(Exception):
    """A newer version of the row is already stored. Another writer is active."""


@dataclass
class Batch:
    incidents: list["Incident"] = field(default_factory=list)
    timeline: dict[UUID, list] = field(default_factory=dict)           # new entries only
    evidence: dict[UUID, list[dict]] = field(default_factory=dict)     # new entries only
    assignments: list["Assignment"] = field(default_factory=list)
    detections: list[tuple[UUID, UUID, str]] = field(default_factory=list)   # event_id, incident_id, camera_id
    low_confidence: list[UUID] = field(default_factory=list)
    reports: list[tuple[str, UUID, dict]] = field(default_factory=list)
    plans: list["Plan"] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    events: list["Envelope"] = field(default_factory=list)   # to be relayed to the bus once committed
    notifications: list["Notification"] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.incidents or self.assignments or self.detections or self.low_confidence
                    or self.reports or self.plans or self.audit or self.events or self.notifications)


@dataclass
class Loaded:
    incidents: list["Incident"] = field(default_factory=list)
    detections: list[tuple[UUID, UUID, str]] = field(default_factory=list)   # in arrival order
    assignments: list["Assignment"] = field(default_factory=list)
    plans: list["Plan"] = field(default_factory=list)
    reports: dict[str, dict] = field(default_factory=dict)
    low_confidence: list[UUID] = field(default_factory=list)
    notifications: list["Notification"] = field(default_factory=list)
    contacts: list["Contact"] = field(default_factory=list)


class Repository(Protocol):
    def write(self, batch: Batch) -> None: ...
    def load(self, closed_within_days: int) -> Loaded: ...
    def idempotency_get(self, key: tuple[str, str, str, str]) -> tuple[int, dict] | None: ...
    def idempotency_put(self, key: tuple[str, str, str, str], status: int, body: dict) -> None: ...
    def outbox_pending(self, limit: int) -> list[tuple[int, "Envelope"]]: ...
    def outbox_mark_sent(self, ids: list[int]) -> None: ...
    def outbox_purge(self, older_than_days: int) -> int: ...


class MemoryRepo:
    """Nothing is kept beyond the process. The default for tests and quick runs."""

    def __init__(self) -> None:
        self._idem: dict[tuple, tuple[int, dict]] = {}
        self._outbox: list[tuple[int, "Envelope"]] = []
        self._next_id = 1

    def write(self, batch: Batch) -> None:
        for ev in batch.events:
            self._outbox.append((self._next_id, ev))
            self._next_id += 1

    def load(self, closed_within_days: int) -> Loaded:
        return Loaded()

    def idempotency_get(self, key):
        return self._idem.get(key)

    def idempotency_put(self, key, status, body):
        self._idem.setdefault(key, (status, body))

    def outbox_pending(self, limit):
        return self._outbox[:limit]

    def outbox_mark_sent(self, ids):
        done = set(ids)
        self._outbox = [row for row in self._outbox if row[0] not in done]

    def outbox_purge(self, older_than_days):
        return 0
