"""Connects incidents to the forecaster: which segments have an open incident, and how bad."""
from __future__ import annotations

from datetime import datetime, timezone

from atms_contracts.models import IncidentState

from atms_forecast.segments import SEGMENTS

from .geo import haversine_m
from .store import Store

OPEN = {IncidentState.detected, IncidentState.verified, IncidentState.dispatching, IncidentState.dispatched,
        IncidentState.in_progress, IncidentState.clearing}
MAX_DISTANCE_M = 1500.0


def open_incidents_by_segment(store: Store, now: datetime | None = None) -> dict[str, tuple[int, float]]:
    """segment id to (lanes closed, minutes since the incident began). An unverified detection counts: it is a warning."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, tuple[int, float]] = {}
    for inc in store.incidents.values():
        if inc.state not in OPEN:
            continue
        seg = min(SEGMENTS, key=lambda s: haversine_m(inc.location, _loc(s)))
        if haversine_m(inc.location, _loc(seg)) > MAX_DISTANCE_M:
            continue
        lanes = min(len(inc.lanes), 3)
        age = max(0.0, (now - inc.opened_at).total_seconds() / 60)
        if seg.id not in out or lanes > out[seg.id][0]:
            out[seg.id] = (lanes, age)
    return out


def _loc(seg):
    from atms_contracts.models import Location

    return Location(lat=seg.lat, lon=seg.lon)


def incident_inputs(store: Store, incident_id, now: datetime | None = None) -> dict | None:
    """What the twin needs to know about one incident: where, how many lanes, how severe, how long it has gone on."""
    now = now or datetime.now(timezone.utc)
    inc = store.incidents.get(incident_id)
    if inc is None:
        return None
    seg = min(SEGMENTS, key=lambda s: haversine_m(inc.location, _loc(s)))
    near = haversine_m(inc.location, _loc(seg)) <= MAX_DISTANCE_M
    return {"segment": seg.id if near else None, "lanes_closed": min(len(inc.lanes), 3), "severity": inc.severity.value,
            "elapsed_min": max(0.0, (now - inc.opened_at).total_seconds() / 60), "state": inc.state.value}
