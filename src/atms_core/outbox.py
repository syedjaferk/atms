"""Outbox relay. Moves committed events from the repository to the bus, at least once.

deliver() runs right after each commit, so the usual latency is one commit. sweep() runs in the
background and picks up anything a crash, a Kafka outage or a failed flush left behind.
"""
from __future__ import annotations

import asyncio
import logging

from .repo import Repository

log = logging.getLogger("atms.outbox")


class OutboxRelay:
    def __init__(self, repo: Repository, bus, batch: int = 200):
        self.repo, self.bus, self.batch = repo, bus, batch

    def deliver(self) -> int:
        """Publish pending events in order, flush, then mark them sent. Raises if the bus could not take them."""
        total = 0
        while True:
            rows = self.repo.outbox_pending(self.batch)
            if not rows:
                return total
            for _id, event in rows:
                self.bus.publish(event)
            flush = getattr(self.bus, "flush", None)
            if flush:
                flush()                       # only events the bus confirmed are marked sent
            self.repo.outbox_mark_sent([i for i, _ in rows])
            total += len(rows)

    def backlog(self, limit: int = 10000) -> int:
        return len(self.repo.outbox_pending(limit))

    def has_backlog(self) -> bool:
        return bool(self.repo.outbox_pending(1))

    async def sweep(self, interval_s: float = 1.0, purge_every: int = 3600, keep_days: int = 7) -> None:
        n = 0
        while True:
            try:
                self.deliver()
                n += 1
                if n % purge_every == 0:
                    self.repo.outbox_purge(keep_days)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("outbox sweep failed, will retry")
            await asyncio.sleep(interval_s)
