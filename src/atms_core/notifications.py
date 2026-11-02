"""Notification Service. Tells people and agencies what is happening, and keeps proof of delivery.

A notice is one row per (source event, contact). The row is stored before anything is sent, so a
redelivered event creates nothing new and a crash mid send is retried with the same idempotency key.
SMS goes to a primary provider and falls back to a second one. Webhooks are signed. Delivery receipts
come back through the API and become events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID, uuid4

from atms_contracts.models import (
    Agency, AssignmentDeclined, Envelope, IncidentClosed, IncidentRejected, IncidentResolved, IncidentVerified,
    NotificationChannel, NotificationDelivered, NotificationDeliveredData, NotificationFailed, NotificationFailedData,
    NotificationSent, NotificationSentData, Severity,
)

from .incident_service import Conflict, NotFound
from .notify_providers import ProviderError, SmsProvider, WebhookSender
from .orchestrator import AGENCIES_BY_SEVERITY
from .store import Contact, Incident, Notification, Store

log = logging.getLogger("atms.notify")
PRODUCER = "notification-service"
SEVERITY_ORDER = [Severity.minor, Severity.major, Severity.critical]

TYPES = ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed", "assignment.declined"]

# Fictional numbers and local URLs. Real contacts come from the contact table.
SEED_CONTACTS: list[Contact] = [
    Contact("duty-nh48", "Duty officer NH48", "sms", "+910000000001", "duty_officer",
            ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed"],
            road_segment="MH/PUNE/NH48", min_severity=Severity.major),
    Contact("supervisor", "Control room supervisor", "sms", "+910000000002", "supervisor",
            ["dispatch.escalation", "incident.closed"]),
    *[Contact(f"agency-{a.value}", f"{a.value.replace('_', ' ').title()} desk", "webhook",
              f"http://localhost:9100/v1/agencies/{a.value}/notice", "agency",
              ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed"],
              agency=a, secret=f"dev-secret-{a.value}") for a in Agency],
]


def network_contacts(agencies_url: str | None = None) -> list[Contact]:
    """Who gets told, built from the network. One duty officer per road, one desk per station, one supervisor.

    A station is only told about an incident when it is one of the nearest stations of its agency (see NearbyStations), so a
    crash in Chennai never pages Pune.
    """
    from atms_contracts import network

    base = (agencies_url or os.environ.get("ATMS_AGENCIES_URL", "http://localhost:9100")).rstrip("/")
    net = network.load()
    out = [Contact("supervisor", "Control room supervisor", "sms", "+910000000002", "supervisor", ["dispatch.escalation", "incident.closed"])]
    for i, a in enumerate(net.areas.values(), 1):
        out.append(Contact(f"duty-{a.id}", f"Duty officer {a.name}", "sms", f"+9100000010{i:02d}", "duty_officer",
                           ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed"],
                           road_segment=a.road_segment, min_severity=Severity.major))
    for st in net.stations:
        out.append(Contact(f"stn-{st.id}", st.name, "webhook", f"{base}/v1/agencies/{st.agency.value}/notice?station={st.id}", "agency",
                           ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed"],
                           agency=st.agency, secret=f"dev-secret-{st.agency.value}"))
    return out


@dataclass(frozen=True)
class NotifyConfig:
    max_attempts: int = 4
    fallback_after: int = 2                      # attempts on the primary before the fallback provider is used
    backoff_s: tuple[float, ...] = (2.0, 10.0, 30.0)
    receipt_timeout_s: float = 120.0


def mask(address: str) -> str:
    if address.startswith("http"):
        return address.split("/")[2] + "/..."
    return address[:3] + "*" * max(0, len(address) - 6) + address[-3:]


def short(incident_id: UUID) -> str:
    return str(incident_id)[:8]


def render(kind: str, inc: Incident, channel: str, agency: Agency | None = None, now: datetime | None = None) -> str:
    """Plain ASCII, one SMS segment. A webhook gets the same facts as JSON."""
    road, ref = inc.road_segment.split("/")[-1], short(inc.id)
    sev, lanes = inc.severity.value.upper(), " ".join(sorted(inc.lanes))
    text = {
        "incident.verified": f"ATMS {sev} accident on {road} ({inc.road_segment}). Lanes closed {lanes}. Ref {ref}",
        "incident.rejected": f"ATMS stand down. Incident {ref} on {road} was a false alarm",
        "incident.resolved": f"ATMS road clear on {road}. Incident {ref} resolved",
        "incident.closed": f"ATMS incident {ref} on {road} closed. Report ready",
        "dispatch.escalation": f"ATMS ACTION: no unit confirmed. Call {agency.value if agency else 'agency'} by phone for incident {ref} on {road}",
    }[kind]
    if channel == "sms":
        return text
    return json.dumps({
        "notice": kind, "message": text, "agency": agency.value if agency else None,
        "incident": {"id": str(inc.id), "state": inc.state.value, "severity": inc.severity.value,
                     "road_segment": inc.road_segment, "lanes": sorted(inc.lanes),
                     "location": {"lat": inc.location.lat, "lon": inc.location.lon}},
        "sent_at": (now or datetime.now(timezone.utc)).isoformat(),
    }, sort_keys=True)


class NotificationService:
    def __init__(self, store: Store, sms: list[SmsProvider], webhook: WebhookSender, config: NotifyConfig | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 receipt_secrets: dict[str, str] | None = None, geo=None, nearest_stations: int = 2):
        self.store, self.sms, self.webhook = store, sms, webhook
        self.geo, self.nearest_stations = geo, nearest_stations
        self.config, self.clock = config or NotifyConfig(), clock
        self.receipt_secrets = receipt_secrets or {}
        self._inflight: set[UUID] = set()
        self._by_ref: dict[tuple[str, str], UUID] = {}
        for n in store.notifications.values():
            if n.provider and n.provider_ref:
                self._by_ref[(n.provider, n.provider_ref)] = n.id

    # ------------------------------------------------------------ deciding who to tell

    def _agencies_in_play(self, inc: Incident) -> set[Agency]:
        return set(AGENCIES_BY_SEVERITY[inc.severity]) | {a.agency for a in self.store.for_incident(inc.id)}

    def recipients(self, kind: str, inc: Incident) -> list[Contact]:
        out = []
        for c in self.store.contacts:
            if (not c.enabled or kind not in c.events or c.road_segment not in (None, inc.road_segment)
                    or SEVERITY_ORDER.index(inc.severity) < SEVERITY_ORDER.index(c.min_severity)):
                continue
            if c.role == "agency" and c.agency not in self._agencies_in_play(inc):
                continue
            if c.id.startswith("stn-") and self.geo is not None and c.id[4:] not in self._nearby(inc, c.agency):
                continue
            out.append(c)
        return out

    def _nearby(self, inc: Incident, agency: Agency) -> set[str]:
        """Ids of the stations of this agency that are closest to the incident, plus any that were actually dispatched."""
        near = {r.facility.id for r in self.geo.nearest(agency, inc.location, self.nearest_stations)}
        return near | {a.facility for a in self.store.for_incident(inc.id) if a.facility and a.agency == agency}

    def _kind(self, ev: Envelope) -> tuple[str, Agency | None] | None:
        if isinstance(ev, IncidentVerified):
            return "incident.verified", None
        if isinstance(ev, IncidentRejected):
            return "incident.rejected", None
        if isinstance(ev, IncidentResolved):
            return "incident.resolved", None
        if isinstance(ev, IncidentClosed):
            return "incident.closed", None
        if isinstance(ev, AssignmentDeclined) and "call manually" in ev.data.reason:
            return "dispatch.escalation", ev.data.agency
        return None

    async def on_event(self, ev: Envelope) -> None:
        found = self._kind(ev)
        inc = self.store.incidents.get(ev.incident_id) if ev.incident_id else None
        if found is None or inc is None:
            return
        kind, agency = found
        now = self.clock()
        for c in self.recipients(kind, inc):
            self.store.add_notification(Notification(
                id=uuid4(), incident_id=inc.id, source_event_id=ev.event_id, contact_id=c.id, contact_name=c.name,
                channel=c.channel, address=c.address, kind=kind, body=render(kind, inc, c.channel, agency, now),
                status="pending", created_at=now, next_attempt_at=now))
        self.store.commit()                           # the notices exist before any is sent
        await self.deliver_due()

    # ------------------------------------------------------------ sending

    def _due(self) -> list[Notification]:
        now = self.clock()
        return [n for n in self.store.notifications.values()
                if n.status == "pending" and n.next_attempt_at <= now and n.id not in self._inflight]

    def quiet(self) -> bool:
        return not self._inflight and not self._due()

    async def deliver_due(self) -> int:
        due = self._due()
        if due:
            await asyncio.gather(*(self._attempt(n) for n in due))
        return len(due)

    def _provider(self, attempt: int) -> SmsProvider:
        use_fallback = attempt > self.config.fallback_after and len(self.sms) > 1
        return self.sms[1 if use_fallback else 0]

    async def _attempt(self, n: Notification) -> None:
        self._inflight.add(n.id)
        try:
            n.attempts += 1                           # counted first, so a crash mid send still counts
            self.store.mark(n)
            self.store.commit()
            try:
                if n.channel == "sms":
                    provider = self._provider(n.attempts)
                    n.provider = provider.name
                    res = await provider.send(n.address, n.body, str(n.id))     # same key on every retry
                    n.provider_ref, n.status, n.sent_at, n.last_error = res.provider_ref, "sent", self.clock(), None
                    self._by_ref[(provider.name, res.provider_ref)] = n.id
                    self._emit_sent(n)
                else:
                    contact = next((c for c in self.store.contacts if c.id == n.contact_id), None)
                    n.provider = "webhook"
                    await self.webhook.send(n.address, n.body.encode(), contact.secret if contact else None)
                    n.status, n.sent_at, n.delivered_at, n.last_error = "delivered", self.clock(), self.clock(), None
                    self._emit_sent(n)
                    self._emit_delivered(n)
            except ProviderError as exc:
                self._failed_attempt(n, exc)
            except Exception as exc:  # noqa: BLE001 - a bug in an adapter must not stop other notices
                log.exception("notification %s crashed", n.id)
                self._failed_attempt(n, ProviderError(f"internal error {type(exc).__name__}"))
            self.store.mark(n)
            self.store.commit()
        finally:
            self._inflight.discard(n.id)

    def _failed_attempt(self, n: Notification, exc: ProviderError) -> None:
        n.last_error = str(exc)
        cfg, now = self.config, self.clock()
        on_primary = n.channel == "sms" and n.attempts <= cfg.fallback_after and len(self.sms) > 1
        if not exc.transient:
            if on_primary:                            # the primary refused for good, go straight to the fallback
                n.attempts = max(n.attempts, cfg.fallback_after)
                n.status, n.next_attempt_at = "pending", now
            else:
                self._fail(n)
            return
        if n.attempts >= cfg.max_attempts:
            self._fail(n)
            return
        switching = n.channel == "sms" and n.attempts == cfg.fallback_after and len(self.sms) > 1
        wait = 0.0 if switching else cfg.backoff_s[min(n.attempts - 1, len(cfg.backoff_s) - 1)]
        n.status, n.next_attempt_at = "pending", now + timedelta(seconds=wait)

    def _fail(self, n: Notification) -> None:
        n.status = "failed"
        inc = self.store.incidents.get(n.incident_id)
        if inc:
            self.store.emit(NotificationFailed(
                producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id,
                data=NotificationFailedData(notification_id=n.id, contact_id=n.contact_id, recipient=n.contact_name,
                                            channel=NotificationChannel(n.channel), reason=n.last_error or "failed",
                                            attempts=max(1, n.attempts))))

    def _emit_sent(self, n: Notification) -> None:
        inc = self.store.incidents.get(n.incident_id)
        if inc:
            self.store.emit(NotificationSent(
                producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id,
                data=NotificationSentData(notification_id=n.id, contact_id=n.contact_id,
                                          channel=NotificationChannel(n.channel), provider=n.provider or "", attempt=n.attempts)))

    def _emit_delivered(self, n: Notification) -> None:
        inc = self.store.incidents.get(n.incident_id)
        if inc:
            self.store.emit(NotificationDelivered(
                producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id,
                data=NotificationDeliveredData(notification_id=n.id, contact_id=n.contact_id,
                                               channel=NotificationChannel(n.channel), provider=n.provider or "")))

    # ------------------------------------------------------------ receipts, timeouts, manual retry

    def record_receipt(self, provider: str, provider_ref: str, delivered: bool, reason: str | None = None) -> Notification:
        nid = self._by_ref.get((provider, provider_ref))
        n = self.store.notifications.get(nid) if nid else None
        if n is None:
            raise NotFound(provider_ref)
        if n.status != "sent":
            return n                                  # a repeat receipt, or one that arrives after we gave up
        if delivered:
            n.status, n.delivered_at, n.last_error = "delivered", self.clock(), None
            self._emit_delivered(n)
        else:
            self._failed_attempt(n, ProviderError(reason or "undelivered", transient=False))
        self.store.mark(n)
        self.store.commit()
        return n

    def expire_unconfirmed(self) -> int:
        """SMS the provider accepted but never confirmed. Resent through the fallback, then failed."""
        now, n_done = self.clock(), 0
        for n in list(self.store.notifications.values()):
            if (n.status == "sent" and n.channel == "sms" and n.sent_at
                    and now - n.sent_at >= timedelta(seconds=self.config.receipt_timeout_s)):
                self._failed_attempt(n, ProviderError("no delivery receipt", transient=False))
                self.store.mark(n)
                n_done += 1
        if n_done:
            self.store.commit()
        return n_done

    async def retry(self, notification_id: UUID) -> Notification:
        n = self.store.notifications.get(notification_id)
        if n is None:
            raise NotFound(str(notification_id))
        if n.status != "failed":
            raise Conflict(n.status, "only a failed notice can be retried")
        n.status, n.attempts, n.next_attempt_at = "pending", 0, self.clock()
        self.store.mark(n)
        self.store.commit()
        await self.deliver_due()
        return n

    async def sweep(self, interval_s: float = 1.0) -> None:
        while True:
            try:
                await self.deliver_due()
                self.expire_unconfirmed()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("notification sweep failed, will retry")
            await asyncio.sleep(interval_s)
