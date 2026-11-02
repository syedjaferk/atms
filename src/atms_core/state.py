"""Allowed transitions. The Incident Service is the only code that applies them."""
from atms_contracts.models import AssignmentState as A
from atms_contracts.models import IncidentState as I

INCIDENT_TRANSITIONS: dict[I, set[I]] = {
    I.detected: {I.verified, I.rejected},
    I.verified: {I.dispatching, I.rejected},
    I.dispatching: {I.dispatched, I.rejected},
    I.dispatched: {I.in_progress, I.rejected},
    I.in_progress: {I.clearing},
    I.clearing: {I.resolved},
    I.resolved: {I.closed},
    I.rejected: {I.closed},
    I.closed: set(),
}

ASSIGNMENT_TRANSITIONS: dict[A, set[A]] = {
    A.requested: {A.acked, A.declined, A.timed_out, A.cancelled},
    A.timed_out: {A.requested, A.declined, A.cancelled},
    A.declined: {A.requested, A.cancelled},
    A.acked: {A.en_route, A.cancelled},
    A.en_route: {A.on_scene, A.cancelled},
    A.on_scene: {A.done, A.cancelled},
    A.done: set(),
    A.cancelled: set(),
}

# an incident in one of these states takes no more detections
CLOSED_FOR_MERGE = {I.rejected, I.resolved, I.closed}
# assignment states that will not change again without new action
SETTLED = {A.done, A.cancelled, A.declined}
