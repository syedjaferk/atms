"""Incident Service. The only writer of incident and assignment state.

Commands (verify, reject, resolve, close, update_assignment) come from the API.
Facts reported by other services arrive as events through `on_event` and are materialised here.
Every state change emits an event.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from atms_contracts.models import (
    AccidentDetected, Agency, AssignmentAcked, AssignmentCancelled, AssignmentDeclined, AssignmentRequested,
    AssignmentState, AssignmentTimedOut, Envelope, IncidentClosed, IncidentClosedData, IncidentCreated,
    IncidentCreatedData, IncidentRejected, IncidentRejectedData, IncidentResolved, IncidentResolvedData,
    IncidentState, IncidentUpdated, IncidentUpdatedData, IncidentVerified, IncidentVerifiedData,
    NotificationFailed, ResponderStatus, ResponderStatusChanged, ResponderStatusChangedData, Severity,
    TrafficPlanApplied, TrafficPlanReverted,
)

from .bus import InMemoryBus
from .geo import bearing_diff_deg, haversine_m
from .state import ASSIGNMENT_TRANSITIONS, CLOSED_FOR_MERGE, INCIDENT_TRANSITIONS, SETTLED
from .store import Assignment, Incident, Store, TimelineEntry

PRODUCER = "incident-service"
SEVERITY_ORDER = [Severity.minor, Severity.major, Severity.critical]


@dataclass(frozen=True)
class Policy:
    auto_verify_confidence: float = 0.90
    auto_verify_min_severity: Severity = Severity.major
    review_confidence: float = 0.60       # below this a detection is logged, no incident
    merge_radius_m: float = 300.0
    merge_window_s: float = 600.0
    merge_max_heading_diff_deg: float = 90.0


class Conflict(Exception):
    def __init__(self, current_state: str, message: str = "transition not allowed"):
        super().__init__(message)
        self.current_state, self.message = current_state, message


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


def persisted(fn):
    """Write the marked changes through the repository when the operation ends, even if it raised."""
    if __import__("inspect").iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(self, *a, **k):
            try:
                return await fn(self, *a, **k)
            finally:
                self.store.commit()
        return awrapper

    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        try:
            return fn(self, *a, **k)
        finally:
            self.store.commit()
    return wrapper


def _max_severity(a: Severity, b: Severity) -> Severity:
    return a if SEVERITY_ORDER.index(a) >= SEVERITY_ORDER.index(b) else b


class IncidentService:
    def __init__(self, store: Store, bus: InMemoryBus, policy: Policy | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.store, self.bus, self.policy, self.clock = store, bus, policy or Policy(), clock

    # ------------------------------------------------------------ helpers

    def _get(self, incident_id: UUID) -> Incident:
        inc = self.store.incidents.get(incident_id)
        if inc is None:
            raise NotFound(str(incident_id))
        self.store.mark(inc)
        return inc

    def _emit(self, cls, inc: Incident, data) -> Envelope:
        event = cls(producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id, data=data)
        self.store.emit(event)
        return event

    def _move(self, inc: Incident, to: IncidentState) -> None:
        if to not in INCIDENT_TRANSITIONS[inc.state]:
            raise Conflict(inc.state.value, f"cannot go from {inc.state.value} to {to.value}")
        inc.state = to
        inc.version += 1

    def _note(self, inc: Incident, kind: str, summary: str, actor: str | None = None) -> None:
        inc.timeline.append(TimelineEntry(self.clock(), kind, summary, actor))

    # ------------------------------------------------------------ detections (called by the correlator)

    @persisted
    def create_from_detection(self, ev: AccidentDetected) -> Incident:
        d = ev.data
        inc = Incident(
            id=uuid4(), state=IncidentState.detected, severity=d.severity, confidence=d.confidence,
            location=d.location, heading_deg=d.heading_deg,
            road_segment=f"{d.site.state}/{d.site.city}/{d.site.road}",
            opened_at=ev.occurred_at, last_detection_at=ev.occurred_at, trace_id=ev.trace_id,
            lanes=list(d.affected_lanes), detection_ids=[ev.event_id], cameras={d.site.camera_id},
        )
        self.store.incidents[inc.id] = inc
        self.store.mark(inc)
        self.store.record_detection(ev.event_id, inc.id, d.site.camera_id)
        self._note(inc, "incident.created", "incident created")
        self._emit(IncidentCreated, inc, IncidentCreatedData(
            state=inc.state, severity=inc.severity, confidence=inc.confidence, location=inc.location,
            road_segment=inc.road_segment, detection_event_ids=[ev.event_id]))
        self._maybe_auto_verify(inc)
        return inc

    @persisted
    def attach_detection(self, inc: Incident, ev: AccidentDetected) -> None:
        d = ev.data
        if ev.event_id in self.store.detections:
            return
        self.store.mark(inc)
        self.store.record_detection(ev.event_id, inc.id, d.site.camera_id)
        inc.detection_ids.append(ev.event_id)
        inc.cameras.add(d.site.camera_id)
        inc.confidence = max(inc.confidence, d.confidence)
        inc.severity = _max_severity(inc.severity, d.severity)
        inc.lanes = sorted(set(inc.lanes) | set(d.affected_lanes))
        inc.last_detection_at = max(inc.last_detection_at, ev.occurred_at)
        inc.version += 1
        self._note(inc, "incident.updated", "more evidence merged")
        self._emit(IncidentUpdated, inc, IncidentUpdatedData(
            reason=f"detection from {d.site.camera_id}", confidence=inc.confidence, severity=inc.severity,
            affected_lanes=inc.lanes, detection_event_ids=inc.detection_ids))
        self._maybe_auto_verify(inc)

    @persisted
    def attach_video(self, inc: Incident, camera_id: str, video_ref: str, sha256: str) -> None:
        if any(e["ref"] == video_ref for e in inc.evidence):
            return                                            # redelivery
        self.store.mark(inc)
        inc.evidence.append({"kind": "clip", "ref": video_ref, "camera_id": camera_id, "sha256": sha256})
        inc.version += 1

    def _maybe_auto_verify(self, inc: Incident) -> None:
        p = self.policy
        if (inc.state == IncidentState.detected and inc.confidence >= p.auto_verify_confidence
                and SEVERITY_ORDER.index(inc.severity) >= SEVERITY_ORDER.index(p.auto_verify_min_severity)):
            self.verify(inc.id, by="auto")

    # ------------------------------------------------------------ commands

    @persisted
    def verify(self, incident_id: UUID, by: str = "operator", operator_id: str | None = None) -> Incident:
        inc = self._get(incident_id)
        self._move(inc, IncidentState.verified)
        self._note(inc, "verified", f"verified by {by}", operator_id)
        self._emit(IncidentVerified, inc, IncidentVerifiedData(verified_by=by, operator_id=operator_id))
        return inc

    @persisted
    def reject(self, incident_id: UUID, reason: str, operator_id: str | None = None, by: str = "operator") -> Incident:
        inc = self._get(incident_id)
        self._move(inc, IncidentState.rejected)
        self._note(inc, "rejected", reason, operator_id)
        self._emit(IncidentRejected, inc, IncidentRejectedData(rejected_by=by, reason=reason, operator_id=operator_id))
        return inc

    @persisted
    def resolve(self, incident_id: UUID, by: str) -> Incident:
        inc = self._get(incident_id)
        self._move(inc, IncidentState.resolved)
        inc.resolved_at = self.clock()
        self._note(inc, "resolved", "road clear confirmed", by)
        self._emit(IncidentResolved, inc, IncidentResolvedData(resolved_by=by))
        if inc.plan_id is None:                 # no lane plan to revert, nothing to wait for
            self._close(inc)
        return inc

    @persisted
    def close(self, incident_id: UUID) -> Incident:
        inc = self._get(incident_id)
        if inc.state == IncidentState.resolved and not inc.plan_reverted and inc.plan_id is not None:
            raise Conflict(inc.state.value, "lane plan has not been reverted yet")
        self._close(inc)
        return inc

    @persisted
    def add_note(self, incident_id: UUID, text: str, actor: str) -> None:
        inc = self._get(incident_id)
        self._note(inc, "note", text, actor)
        inc.version += 1

    def _close(self, inc: Incident) -> None:
        self._move(inc, IncidentState.closed)
        ref = f"reports/{inc.id}.json"
        report = {
            "incident_id": str(inc.id), "road_segment": inc.road_segment, "severity": inc.severity.value,
            "confidence": inc.confidence, "opened_at": inc.opened_at.isoformat(),
            "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
            "cameras": sorted(inc.cameras), "evidence": inc.evidence,
            "assignments": [{"agency": a.agency.value, "unit": a.unit_ref, "state": a.state.value, "eta_s": a.eta_s}
                            for a in self.store.for_incident(inc.id)],
            "timeline": [{"at": t.at.isoformat(), "kind": t.kind, "summary": t.summary, "actor": t.actor}
                         for t in inc.timeline],
        }
        self.store.record_report(ref, inc.id, report)
        inc.report_ref = ref
        self._note(inc, "incident.closed", "incident closed")
        self._emit(IncidentClosed, inc, IncidentClosedData(report_ref=ref))

    @persisted
    def update_assignment(self, incident_id: UUID, assignment_id: UUID, status: ResponderStatus,
                          eta_s: int | None, caller_agency: Agency | None) -> Assignment:
        """caller_agency None means an operator, who may update any assignment."""
        inc = self._get(incident_id)
        a = self.store.assignments.get(assignment_id)
        if a is None or a.incident_id != incident_id:
            raise NotFound(str(assignment_id))
        if caller_agency is not None and caller_agency != a.agency:
            raise Forbidden("assignment belongs to another agency")
        target = AssignmentState(status.value)
        if a.state == target:
            return a                                    # repeat of the same update
        if target not in ASSIGNMENT_TRANSITIONS[a.state]:
            raise Conflict(a.state.value, f"assignment is {a.state.value}, cannot go to {target.value}")
        previous = a.state
        a.state = target
        self.store.mark(a)
        if eta_s is not None:
            a.eta_s = eta_s
        self._after_assignment_change(inc, a, target)
        self._emit(ResponderStatusChanged, inc, ResponderStatusChangedData(
            assignment_id=a.id, agency=a.agency, previous=previous, status=status, eta_s=a.eta_s,
            incident_state=inc.state))
        return a

    def _after_assignment_change(self, inc: Incident, a: Assignment, target: AssignmentState) -> None:
        self._note(inc, "assignment", f"{a.agency.value} {a.unit_ref or ''} is {target.value}".replace("  ", " "))
        if target == AssignmentState.on_scene and inc.state == IncidentState.dispatched:
            self._move(inc, IncidentState.in_progress)
        elif target == AssignmentState.done and inc.state == IncidentState.in_progress:
            mine = self.store.for_incident(inc.id)
            if all(x.state in SETTLED for x in mine) and any(x.state == AssignmentState.done for x in mine):
                self._move(inc, IncidentState.clearing)
        inc.version += 1

    # ------------------------------------------------------------ facts from other services

    @persisted
    async def on_event(self, ev: Envelope) -> None:
        inc = self.store.incidents.get(ev.incident_id) if ev.incident_id else None
        if inc is None:
            return
        self.store.mark(inc)
        if isinstance(ev, AssignmentRequested):
            self._on_requested(inc, ev)
        elif isinstance(ev, AssignmentAcked):
            self._on_acked(inc, ev)
        elif isinstance(ev, AssignmentDeclined):
            self._set_assignment(inc, ev.data.assignment_id, AssignmentState.declined, f"{ev.data.agency.value} declined: {ev.data.reason}")
        elif isinstance(ev, AssignmentTimedOut):
            self._set_assignment(inc, ev.data.assignment_id, AssignmentState.timed_out, f"{ev.data.agency.value} did not answer")
        elif isinstance(ev, AssignmentCancelled):
            self._set_assignment(inc, ev.data.assignment_id, AssignmentState.cancelled, f"{ev.data.agency.value} cancelled: {ev.data.reason}")
        elif isinstance(ev, NotificationFailed):
            self._note(inc, "operator_task", f"could not notify {ev.data.recipient} by {ev.data.channel.value}: {ev.data.reason}")
            inc.version += 1
        elif isinstance(ev, TrafficPlanApplied):
            if inc.plan_id == ev.data.plan_id:
                return                                        # redelivery
            inc.plan_id = ev.data.plan_id
            self._note(inc, "traffic", f"lanes closed {','.join(ev.data.lanes_closed)}")
            if ev.data.failed_devices:
                self._note(inc, "operator_task", f"VMS not confirmed: {','.join(ev.data.failed_devices)}")
            inc.version += 1
        elif isinstance(ev, TrafficPlanReverted):
            if inc.plan_reverted:
                return                                        # redelivery
            inc.plan_reverted = True
            self._note(inc, "traffic", "lane plan reverted")
            inc.version += 1
            if inc.state == IncidentState.resolved:
                self._close(inc)

    def _on_requested(self, inc: Incident, ev: AssignmentRequested) -> None:
        d = ev.data
        a = self.store.assignments.get(d.assignment_id)
        if a is None:
            a = self.store.assignments[d.assignment_id] = Assignment(
                id=d.assignment_id, incident_id=inc.id, agency=d.agency, state=AssignmentState.requested,
                requested_at=ev.occurred_at, attempt=d.attempt, facility=d.unit_ref)
            self.store.mark(a)
        elif a.state in (AssignmentState.timed_out, AssignmentState.declined):
            a.state, a.attempt, a.facility = AssignmentState.requested, d.attempt, d.unit_ref
            self.store.mark(a)
        else:
            return                                            # redelivery, nothing to change
        if inc.state == IncidentState.verified:
            self._move(inc, IncidentState.dispatching)
        inc.version += 1

    def _on_acked(self, inc: Incident, ev: AssignmentAcked) -> None:
        a = self.store.assignments.get(ev.data.assignment_id)
        if a is None or a.state != AssignmentState.requested:
            return                                      # cancelled meanwhile, or a duplicate
        a.state, a.unit_ref, a.eta_s, a.acked_at = AssignmentState.acked, ev.data.unit_ref, ev.data.eta_s, ev.occurred_at
        self.store.mark(a)
        self._note(inc, "assignment", f"{a.agency.value} unit {a.unit_ref} accepted, ETA {a.eta_s}s")
        if inc.state == IncidentState.dispatching:
            self._move(inc, IncidentState.dispatched)
        inc.version += 1

    def _set_assignment(self, inc: Incident, aid: UUID, to: AssignmentState, summary: str) -> None:
        a = self.store.assignments.get(aid)
        if a is None or to not in ASSIGNMENT_TRANSITIONS[a.state]:
            return
        a.state = to
        self.store.mark(a)
        self._note(inc, "assignment", summary)
        inc.version += 1
