"""Wires the core services onto one bus. Production runs each as its own process on Kafka instead."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from atms_contracts.models import Envelope

from .audit import AuditSink
from .bus import InMemoryBus
from .correlator import Correlator
from .geo import Facility, FacilityService
from .hub import AgencyAdapter
from .incident_service import IncidentService, Policy
from .notifications import SEED_CONTACTS, TYPES as NOTIFY_TYPES, NotificationService, NotifyConfig
from .notify_providers import RecordingSms, RecordingWebhook, SmsProvider
from .orchestrator import DispatchConfig, DispatchOrchestrator
from .outbox import OutboxRelay
from .repo import Repository
from .store import Store
from .traffic import SimulatedVms, TrafficManagement, VmsClient

ASSIGNMENT_EVENTS = ["assignment.requested", "assignment.acked", "assignment.declined",
                     "assignment.timed_out", "assignment.cancelled"]


@dataclass
class System:
    bus: InMemoryBus
    relay: OutboxRelay
    store: Store
    service: IncidentService
    correlator: Correlator
    orchestrator: DispatchOrchestrator
    traffic: TrafficManagement
    audit: AuditSink
    geo: FacilityService
    notifications: NotificationService
    forecast: object | None = None
    _sweeper: asyncio.Task | None = field(default=None, repr=False)
    _notify_sweeper: asyncio.Task | None = field(default=None, repr=False)
    _resume_sweeper: asyncio.Task | None = field(default=None, repr=False)
    _forecast_sweeper: asyncio.Task | None = field(default=None, repr=False)

    def ingest(self, event: Envelope, key: str | None = None) -> None:
        """An event arriving from the edge. Normally the bridge writes these to Kafka and nothing calls this."""
        self.bus.publish(event, key)

    async def start(self, sweep_interval_s: float = 1.0) -> None:
        """Begin consuming, relay anything a previous run left in the outbox, resume unfinished dispatches."""
        self.bus.start()
        self.relay.deliver()
        loop = asyncio.get_running_loop()
        self._sweeper = loop.create_task(self.relay.sweep(sweep_interval_s))
        self._notify_sweeper = loop.create_task(self.notifications.sweep(sweep_interval_s))
        self._resume_sweeper = loop.create_task(self.orchestrator.sweep(sweep_interval_s))
        if self.forecast is not None:
            self._forecast_sweeper = loop.create_task(self.forecast.sweep(60.0))
        self.orchestrator.resume_open()

    async def stop(self) -> None:
        for t in (self._sweeper, self._notify_sweeper, self._resume_sweeper, self._forecast_sweeper):
            if t:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
        self._sweeper = self._notify_sweeper = self._resume_sweeper = self._forecast_sweeper = None
        await self.bus.stop()

    def quiet(self) -> bool:
        return (not self.bus.pending() and not self.orchestrator.tasks and not self.relay.has_backlog()
                and self.notifications.quiet())

    async def settle(self) -> None:
        """Wait until no event is waiting anywhere and no dispatch is running. For tests and shutdown."""
        while True:
            self.bus.start()
            try:
                self.relay.deliver()
            except Exception:  # noqa: BLE001 - a bus that is down means we are not settled yet
                await asyncio.sleep(0.05)
                continue
            await self.bus.idle()
            await self.orchestrator.idle()
            await self.notifications.deliver_due()          # retries that have come due
            if self.quiet():
                return


def build_system(hub: AgencyAdapter, vms: VmsClient | None = None, policy: Policy | None = None,
                 dispatch: DispatchConfig | None = None, facilities: list[Facility] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 repo: Repository | None = None, geo=None, load: bool = True, bus=None,
                 sms: list[SmsProvider] | None = None, webhook=None, notify: NotifyConfig | None = None,
                 contacts=None, receipt_secrets: dict[str, str] | None = None, forecast=None) -> System:
    """repo defaults to memory. With a repo the open incidents are loaded first, which is what a restart does."""
    bus, store = bus or InMemoryBus(), Store(repo)
    if load:
        store.load()
    geo = geo or FacilityService(facilities)
    if not store.contacts:
        store.contacts = list(contacts if contacts is not None else SEED_CONTACTS)
    service = IncidentService(store, bus, policy, clock)
    correlator = Correlator(service)
    orchestrator = DispatchOrchestrator(store, bus, geo, hub, dispatch)
    traffic = TrafficManagement(store, bus, vms or SimulatedVms())
    audit = AuditSink(store)
    notifications = NotificationService(store, sms or [RecordingSms("primary"), RecordingSms("fallback")],
                                        webhook or RecordingWebhook(), notify, clock, receipt_secrets)
    relay = OutboxRelay(store.repo, bus)
    store.relay = relay

    bus.subscribe("correlator", ["accident.detected", "video.ready"], correlator.on_event)
    bus.subscribe("incident-service", [*ASSIGNMENT_EVENTS, "traffic.plan.applied", "traffic.plan.reverted", "notification.failed"], service.on_event)
    bus.subscribe("dispatch-orchestrator", ["incident.verified", "incident.rejected"], orchestrator.on_event)
    bus.subscribe("traffic-management", ["incident.verified", "incident.rejected", "incident.resolved"], traffic.on_event)
    bus.subscribe("notification-service", NOTIFY_TYPES, notifications.on_event)
    bus.subscribe("audit", None, audit.on_event)
    if forecast is not None:
        from .forecast_link import open_incidents_by_segment

        forecast.incidents = lambda: open_incidents_by_segment(store, clock())
        forecast.emit = lambda ev: (store.emit(ev), store.commit())
    return System(bus, relay, store, service, correlator, orchestrator, traffic, audit, geo, notifications, forecast)
