"""Working state. Only IncidentService (and Traffic for its plans) change it.

Every change is marked, and `commit()` writes the marked changes through the repository in one
transaction. If the write fails the marks stay, the next commit retries, and the error reaches the caller.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
from uuid import UUID

from atms_contracts.models import AssignmentState, Agency, Envelope, IncidentState, Location, Severity

from .repo import Batch, MemoryRepo, Repository


log = logging.getLogger("atms.store")


@dataclass
class TimelineEntry:
    at: datetime
    kind: str
    summary: str
    actor: str | None = None


@dataclass
class Incident:
    id: UUID
    state: IncidentState
    severity: Severity
    confidence: float
    location: Location
    heading_deg: float
    road_segment: str
    opened_at: datetime
    last_detection_at: datetime
    trace_id: str
    lanes: list[str]
    version: int = 1
    resolved_at: datetime | None = None
    detection_ids: list[UUID] = field(default_factory=list)
    cameras: set[str] = field(default_factory=set)
    evidence: list[dict] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    plan_id: UUID | None = None
    plan_reverted: bool = False
    report_ref: str | None = None


@dataclass
class Assignment:
    id: UUID
    incident_id: UUID
    agency: Agency
    state: AssignmentState
    requested_at: datetime
    attempt: int = 1
    facility: str | None = None
    unit_ref: str | None = None
    eta_s: int | None = None
    acked_at: datetime | None = None


@dataclass
class Contact:
    id: str
    name: str
    channel: str                       # sms or webhook
    address: str                       # phone number or URL
    role: str                          # duty_officer, supervisor, agency
    events: list[str]                  # notice kinds this contact wants
    agency: Agency | None = None
    road_segment: str | None = None    # None means every road
    min_severity: Severity = Severity.minor
    secret: str | None = None          # webhook signing secret
    enabled: bool = True


@dataclass
class Notification:
    id: UUID
    incident_id: UUID
    source_event_id: UUID
    contact_id: str
    contact_name: str
    channel: str
    address: str
    kind: str
    body: str                          # text for sms, JSON for a webhook. Fixed at creation so retries send the same thing
    status: str                        # pending, sent, delivered, failed
    created_at: datetime
    next_attempt_at: datetime
    attempts: int = 0
    provider: str | None = None
    provider_ref: str | None = None
    last_error: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None


@dataclass
class Plan:
    id: UUID
    incident_id: UUID
    lanes: list[str]
    messages: dict[str, str]
    reverted: bool = False


class Store:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo: Repository = repo or MemoryRepo()
        self.relay = None                       # OutboxRelay, set by build_system
        self.incidents: dict[UUID, Incident] = {}
        self.assignments: dict[UUID, Assignment] = {}
        self.detections: dict[UUID, tuple[UUID, str]] = {}   # event_id -> (incident_id, camera_id), arrival order
        self.low_confidence: list[UUID] = []                  # event ids below the incident threshold
        self.low_confidence_ids: set[UUID] = set()
        self.reports: dict[str, dict] = {}
        self.plans: dict[UUID, Plan] = {}                     # by incident id
        self.contacts: list[Contact] = []
        self.notifications: dict[UUID, Notification] = {}
        self._notif_keys: set[tuple[UUID, str]] = set()       # (source event, contact), one notice per pair
        self._dirty_notif: dict[UUID, Notification] = {}
        self.audit: deque[dict] = deque(maxlen=5000)          # recent only, the full log is in the repository
        self._batch = Batch()
        self._dirty_inc: dict[UUID, Incident] = {}
        self._dirty_asg: dict[UUID, Assignment] = {}
        self._dirty_plan: dict[UUID, Plan] = {}
        self._saved_timeline: dict[UUID, int] = {}
        self._saved_evidence: dict[UUID, int] = {}

    # ------------------------------------------------------------ reading

    def for_incident(self, incident_id: UUID) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.incident_id == incident_id]

    # ------------------------------------------------------------ marking changes

    def mark(self, obj: Incident | Assignment | Notification) -> None:
        if isinstance(obj, Incident):
            self._dirty_inc[obj.id] = obj
        elif isinstance(obj, Notification):
            self._dirty_notif[obj.id] = obj
        else:
            self._dirty_asg[obj.id] = obj

    def add_notification(self, n: Notification) -> bool:
        """False if this event already produced a notice for this contact, which makes a redelivered event harmless."""
        key = (n.source_event_id, n.contact_id)
        if key in self._notif_keys:
            return False
        self._notif_keys.add(key)
        self.notifications[n.id] = n
        self.mark(n)
        return True

    def record_detection(self, event_id: UUID, incident_id: UUID, camera_id: str) -> None:
        self.detections[event_id] = (incident_id, camera_id)
        self._batch.detections.append((event_id, incident_id, camera_id))

    def record_low_confidence(self, event_id: UUID) -> None:
        self.low_confidence.append(event_id)
        self.low_confidence_ids.add(event_id)
        self._batch.low_confidence.append(event_id)

    def record_report(self, ref: str, incident_id: UUID, body: dict) -> None:
        self.reports[ref] = body
        self._batch.reports.append((ref, incident_id, body))

    def record_plan(self, plan: Plan) -> None:
        self.plans[plan.incident_id] = plan
        self._dirty_plan[plan.id] = plan

    def emit(self, event: Envelope) -> None:
        """Queue an event. It is stored with the next commit and relayed to the bus after that."""
        self._batch.events.append(event)

    def record_audit(self, rec: dict) -> None:
        self.audit.append(rec)
        self._batch.audit.append(rec)

    # ------------------------------------------------------------ writing through

    def commit(self) -> None:
        b = self._batch
        b.incidents = list(self._dirty_inc.values())
        b.assignments = list(self._dirty_asg.values())
        b.plans = list(self._dirty_plan.values())
        b.notifications = list(self._dirty_notif.values())
        for inc in b.incidents:
            new_tl = inc.timeline[self._saved_timeline.get(inc.id, 0):]
            new_ev = inc.evidence[self._saved_evidence.get(inc.id, 0):]
            if new_tl:
                b.timeline[inc.id] = new_tl
            if new_ev:
                b.evidence[inc.id] = new_ev
        if b.empty():
            return
        self.repo.write(b)            # raises on failure, marks and batch stay for the next try
        for inc in b.incidents:
            self._saved_timeline[inc.id] = len(inc.timeline)
            self._saved_evidence[inc.id] = len(inc.evidence)
        self._batch = Batch()
        self._dirty_inc, self._dirty_asg, self._dirty_plan, self._dirty_notif = {}, {}, {}, {}
        if self.relay is not None and b.events:
            try:
                self.relay.deliver()
            except Exception:  # noqa: BLE001 - the state is committed, a later sweep delivers the rest
                log.exception("relay failed, events stay in the outbox")

    def load(self, closed_within_days: int = 30) -> None:
        """Fill the working set from the repository: every open incident and recently closed ones."""
        st = self.repo.load(closed_within_days)
        for inc in st.incidents:
            self.incidents[inc.id] = inc
            self._saved_timeline[inc.id] = len(inc.timeline)
            self._saved_evidence[inc.id] = len(inc.evidence)
        for event_id, incident_id, camera in st.detections:
            inc = self.incidents.get(incident_id)
            if inc is not None:
                self.detections[event_id] = (incident_id, camera)
                inc.detection_ids.append(event_id)
                inc.cameras.add(camera)
        for a in st.assignments:
            if a.incident_id in self.incidents:
                self.assignments[a.id] = a
        for plan in st.plans:
            if plan.incident_id in self.incidents:
                self.plans[plan.incident_id] = plan
        for n in st.notifications:
            self.notifications[n.id] = n
            self._notif_keys.add((n.source_event_id, n.contact_id))
        self.contacts = list(st.contacts)
        self.reports.update(st.reports)
        self.low_confidence.extend(st.low_confidence)
        self.low_confidence_ids.update(st.low_confidence)
