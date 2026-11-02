"""Response shapes for the public API. They mirror contracts/openapi.yaml and a test checks that."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from atms_contracts.models import Agency, AssignmentState, IncidentState, Location, Severity

from .notifications import mask
from .store import Assignment, Incident, Notification, Store


class IncidentView(BaseModel):
    id: UUID
    state: IncidentState
    severity: Severity
    confidence: float
    location: Location
    road_segment: str
    version: int
    opened_at: datetime
    resolved_at: datetime | None = None


class AssignmentView(BaseModel):
    id: UUID
    incident_id: UUID
    agency: Agency
    unit_ref: str | None = None
    state: AssignmentState
    eta_s: int | None = None
    requested_at: datetime
    acked_at: datetime | None = None


class TimelineView(BaseModel):
    at: datetime
    kind: str
    summary: str
    actor: str | None = None


class IncidentDetailView(IncidentView):
    assignments: list[AssignmentView]
    timeline: list[TimelineView]


def incident_view(i: Incident) -> IncidentView:
    return IncidentView(id=i.id, state=i.state, severity=i.severity, confidence=i.confidence, location=i.location,
                        road_segment=i.road_segment, version=i.version, opened_at=i.opened_at, resolved_at=i.resolved_at)


def assignment_view(a: Assignment) -> AssignmentView:
    return AssignmentView(id=a.id, incident_id=a.incident_id, agency=a.agency, unit_ref=a.unit_ref, state=a.state,
                          eta_s=a.eta_s, requested_at=a.requested_at, acked_at=a.acked_at)


def detail_view(i: Incident, store: Store) -> IncidentDetailView:
    return IncidentDetailView(
        **incident_view(i).model_dump(),
        assignments=[assignment_view(a) for a in store.for_incident(i.id)],
        timeline=[TimelineView(at=t.at, kind=t.kind, summary=t.summary, actor=t.actor) for t in i.timeline])


class NotificationView(BaseModel):
    id: UUID
    incident_id: UUID
    contact: str
    channel: str
    address: str            # masked
    kind: str
    status: str
    attempts: int
    provider: str | None = None
    last_error: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None


def notification_view(n: Notification) -> NotificationView:
    return NotificationView(id=n.id, incident_id=n.incident_id, contact=n.contact_name, channel=n.channel,
                            address=mask(n.address), kind=n.kind, status=n.status, attempts=n.attempts,
                            provider=n.provider, last_error=n.last_error, created_at=n.created_at,
                            sent_at=n.sent_at, delivered_at=n.delivered_at)
