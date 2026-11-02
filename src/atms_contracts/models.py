"""Event contracts. These models are the source of truth.

The JSON Schemas in contracts/events/ are generated from them by
`python -m atms_contracts.export`, and a test fails if the files drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    minor = "minor"
    major = "major"
    critical = "critical"


class Agency(str, Enum):
    police = "police"
    ambulance = "ambulance"
    highway_patrol = "highway_patrol"
    fire = "fire"
    tow = "tow"


class IncidentState(str, Enum):
    detected = "detected"
    verified = "verified"
    rejected = "rejected"
    dispatching = "dispatching"
    dispatched = "dispatched"
    in_progress = "in_progress"
    clearing = "clearing"
    resolved = "resolved"
    closed = "closed"


class AssignmentState(str, Enum):
    requested = "requested"
    acked = "acked"
    timed_out = "timed_out"
    declined = "declined"
    en_route = "en_route"
    on_scene = "on_scene"
    done = "done"
    cancelled = "cancelled"


class ResponderStatus(str, Enum):
    """Statuses a responder may report through the API."""

    en_route = "en_route"
    on_scene = "on_scene"
    done = "done"


class VideoStatus(str, Enum):
    uploading = "uploading"
    ready = "ready"
    failed = "failed"


class Location(Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Site(Strict):
    """Where a camera is. Segments never contain dots or slashes (see topics)."""

    state: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    city: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    road: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    camera_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------- payloads


class AccidentDetectedData(Strict):
    site: Site
    location: Location
    heading_deg: float = Field(ge=0, lt=360)
    confidence: float = Field(ge=0, le=1)
    severity: Severity
    vehicles_involved: int = Field(ge=1)
    affected_lanes: list[str] = Field(min_length=1)
    video_ref: str | None = None
    video_status: VideoStatus = VideoStatus.uploading
    model_version: str


class VideoReadyData(Strict):
    camera_id: str
    video_ref: str
    duration_s: float = Field(gt=0)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IncidentCreatedData(Strict):
    state: IncidentState
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    location: Location
    road_segment: str
    detection_event_ids: list[UUID] = Field(min_length=1)


class IncidentUpdatedData(Strict):
    """A later detection was merged into an open incident."""

    reason: str
    confidence: float = Field(ge=0, le=1)
    severity: Severity
    affected_lanes: list[str]
    detection_event_ids: list[UUID] = Field(min_length=1)


class IncidentVerifiedData(Strict):
    verified_by: Literal["auto", "operator"]
    operator_id: str | None = None


class IncidentRejectedData(Strict):
    rejected_by: Literal["auto", "operator"]
    reason: str
    operator_id: str | None = None


class AssignmentRequestedData(Strict):
    assignment_id: UUID
    agency: Agency
    unit_ref: str | None = None
    idempotency_key: str
    attempt: int = Field(ge=1)


class AssignmentAckedData(Strict):
    assignment_id: UUID
    agency: Agency
    unit_ref: str
    eta_s: int = Field(ge=0)


class AssignmentDeclinedData(Strict):
    assignment_id: UUID
    agency: Agency
    reason: str


class AssignmentCancelledData(Strict):
    assignment_id: UUID
    agency: Agency
    reason: str


class AssignmentTimedOutData(Strict):
    assignment_id: UUID
    agency: Agency
    attempt: int = Field(ge=1)


class ResponderStatusChangedData(Strict):
    assignment_id: UUID
    agency: Agency
    previous: AssignmentState
    status: ResponderStatus
    eta_s: int | None = Field(default=None, ge=0)
    incident_state: IncidentState


class TrafficPlanAppliedData(Strict):
    plan_id: UUID
    lanes_closed: list[str]
    vms_messages: dict[str, str]
    reroute_advisory: str | None = None
    failed_devices: list[str] = Field(default_factory=list)


class TrafficPlanRevertedData(Strict):
    plan_id: UUID


class IncidentResolvedData(Strict):
    resolved_by: str


class IncidentClosedData(Strict):
    report_ref: str


class CongestionForecastData(Strict):
    """Heavy or severe congestion is forecast on a road segment that was not heading for it a moment ago."""

    segment_id: str
    horizon_min: int = Field(ge=1)
    level: Literal["heavy", "severe"]
    predicted_speed_kmh: float = Field(ge=0)
    low_kmh: float
    high_kmh: float
    free_flow_kmh: float = Field(gt=0)
    model_version: str
    main_factor: str | None = None


class NotificationChannel(str, Enum):
    sms = "sms"
    webhook = "webhook"


class NotificationSentData(Strict):
    """Accepted by a provider. No phone numbers or URLs travel on the bus, only the contact id."""

    notification_id: UUID
    contact_id: str
    channel: NotificationChannel
    provider: str
    attempt: int = Field(ge=1)


class NotificationDeliveredData(Strict):
    notification_id: UUID
    contact_id: str
    channel: NotificationChannel
    provider: str


class NotificationFailedData(Strict):
    notification_id: UUID
    contact_id: str
    recipient: str            # a display label such as "Duty officer NH48", not an address
    channel: NotificationChannel
    reason: str
    attempts: int = Field(ge=1)


# ---------------------------------------------------------------- envelope


class Envelope(Strict):
    event_id: UUID = Field(default_factory=uuid4)
    type: str
    schema_version: int = SCHEMA_VERSION
    occurred_at: AwareDatetime = Field(default_factory=_now)
    producer: str
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    incident_id: UUID | None = None
    dedup_key: str | None = None
    data: BaseModel


class AccidentDetected(Envelope):
    type: Literal["accident.detected"] = "accident.detected"
    data: AccidentDetectedData


class VideoReady(Envelope):
    type: Literal["video.ready"] = "video.ready"
    data: VideoReadyData


class IncidentCreated(Envelope):
    type: Literal["incident.created"] = "incident.created"
    data: IncidentCreatedData


class IncidentUpdated(Envelope):
    type: Literal["incident.updated"] = "incident.updated"
    data: IncidentUpdatedData


class IncidentVerified(Envelope):
    type: Literal["incident.verified"] = "incident.verified"
    data: IncidentVerifiedData


class IncidentRejected(Envelope):
    type: Literal["incident.rejected"] = "incident.rejected"
    data: IncidentRejectedData


class AssignmentRequested(Envelope):
    type: Literal["assignment.requested"] = "assignment.requested"
    data: AssignmentRequestedData


class AssignmentAcked(Envelope):
    type: Literal["assignment.acked"] = "assignment.acked"
    data: AssignmentAckedData


class AssignmentDeclined(Envelope):
    type: Literal["assignment.declined"] = "assignment.declined"
    data: AssignmentDeclinedData


class AssignmentCancelled(Envelope):
    type: Literal["assignment.cancelled"] = "assignment.cancelled"
    data: AssignmentCancelledData


class AssignmentTimedOut(Envelope):
    type: Literal["assignment.timed_out"] = "assignment.timed_out"
    data: AssignmentTimedOutData


class ResponderStatusChanged(Envelope):
    type: Literal["responder.status.changed"] = "responder.status.changed"
    data: ResponderStatusChangedData


class TrafficPlanApplied(Envelope):
    type: Literal["traffic.plan.applied"] = "traffic.plan.applied"
    data: TrafficPlanAppliedData


class TrafficPlanReverted(Envelope):
    type: Literal["traffic.plan.reverted"] = "traffic.plan.reverted"
    data: TrafficPlanRevertedData


class IncidentResolved(Envelope):
    type: Literal["incident.resolved"] = "incident.resolved"
    data: IncidentResolvedData


class IncidentClosed(Envelope):
    type: Literal["incident.closed"] = "incident.closed"
    data: IncidentClosedData


class CongestionForecast(Envelope):
    type: Literal["congestion.forecast"] = "congestion.forecast"
    data: CongestionForecastData


class NotificationSent(Envelope):
    type: Literal["notification.sent"] = "notification.sent"
    data: NotificationSentData


class NotificationDelivered(Envelope):
    type: Literal["notification.delivered"] = "notification.delivered"
    data: NotificationDeliveredData


class NotificationFailed(Envelope):
    type: Literal["notification.failed"] = "notification.failed"
    data: NotificationFailedData


EVENT_TYPES: dict[str, type[Envelope]] = {
    cls.model_fields["type"].default: cls
    for cls in (
        AccidentDetected,
        VideoReady,
        IncidentCreated,
        IncidentUpdated,
        IncidentVerified,
        IncidentRejected,
        AssignmentRequested,
        AssignmentAcked,
        AssignmentDeclined,
        AssignmentTimedOut,
        AssignmentCancelled,
        ResponderStatusChanged,
        TrafficPlanApplied,
        TrafficPlanReverted,
        IncidentResolved,
        IncidentClosed,
        CongestionForecast,
        NotificationSent,
        NotificationDelivered,
        NotificationFailed,
    )
}


class UnknownEventType(ValueError):
    pass


def parse_event(raw: bytes | str | dict) -> Envelope:
    """Parse and validate any event. Raises UnknownEventType or pydantic.ValidationError."""
    import json

    obj = raw if isinstance(raw, dict) else json.loads(raw)
    cls = EVENT_TYPES.get(obj.get("type", ""))
    if cls is None:
        raise UnknownEventType(f"unknown event type {obj.get('type')!r}")
    return cls.model_validate(obj)


def make_dedup_key(camera_id: str, event_type: str, at: datetime, bucket_s: int = 10) -> str:
    """Same camera, same type, same time bucket means the same detection."""
    return f"{camera_id}:{event_type}:{int(at.timestamp() // bucket_s)}"
