"""Everything the operator map draws, in one response, so the page needs one request to paint and one to refresh."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from atms_contracts.cameras import CAMERAS
from atms_forecast.segments import SEGMENTS
from atms_forecast.service import NoModel, StaleData

from .forecast_link import OPEN, incident_inputs
from .store import Store
from .traffic import VMS_LOCATIONS

LEVELS = ("free", "moderate", "heavy", "severe")


def road_pieces() -> dict[str, tuple[tuple[float, float], tuple[float, float]]]:
    """Each segment runs from halfway to the one before it to halfway to the one after. Ends are extended by half a gap."""
    pts = [(s.lat, s.lon) for s in SEGMENTS]
    out = {}
    for i, s in enumerate(SEGMENTS):
        before = pts[i - 1] if i else (2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])
        after = pts[i + 1] if i + 1 < len(pts) else (2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1])
        mid = lambda a, b: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        out[s.id] = (mid(before, pts[i]), mid(pts[i], after))
    return out


CACHE_S = 20.0


def road_state(forecast) -> dict[str, dict]:
    """Speed now and forecasts per segment. This is the slow part (a model call per segment), so it is cached for a few
    seconds and the API runs it in a worker thread. It only reads, so that is safe."""
    if forecast is None:
        return {}
    held = getattr(forecast, "_map_state", None)
    if held and time.monotonic() - held[0] < CACHE_S:
        return held[1]
    out = {}
    for seg in SEGMENTS:
        row = {"status": "no forecast", "speed_kmh": None, "now_level": None, "forecasts": []}
        try:
            f = forecast.forecast(seg.id, explain=False)
            row["status"] = "ok"
            row["speed_kmh"] = f["current_speed_kmh"]
            row["forecasts"] = [{"horizon_min": r["horizon_min"], "speed_kmh": r["speed_kmh"], "low_kmh": r["low_kmh"],
                                 "high_kmh": r["high_kmh"], "level": r["level"]} for r in f["forecasts"]]
            row["now_level"] = _level_now(f["current_speed_kmh"], seg.free_flow_kmh)
        except StaleData:
            row["status"] = "no recent data"          # drawn grey
        except NoModel:
            pass
        out[seg.id] = row
    forecast._map_state = (time.monotonic(), out)
    return out


def build_map(system, now: datetime | None = None, road: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    store: Store = system.store
    forecast = system.forecast
    pieces = road_pieces()
    road = road if road is not None else road_state(forecast)

    segments = []
    for seg in SEGMENTS:
        start, end = pieces[seg.id]
        row = {"id": seg.id, "road_segment": seg.road_segment, "free_flow_kmh": seg.free_flow_kmh, "lanes": seg.lanes,
               "from": {"lat": start[0], "lon": start[1]}, "to": {"lat": end[0], "lon": end[1]},
               "status": "no forecast", "speed_kmh": None, "now_level": None, "forecasts": []}
        row.update(road.get(seg.id, {}))
        segments.append(row)

    facilities = system.geo.all() if hasattr(system.geo, "all") else []
    busy: dict[str, list[str]] = {}
    incidents = []
    for inc in sorted(list(store.incidents.values()), key=lambda i: i.opened_at, reverse=True):
        if inc.state not in OPEN:
            continue
        info = incident_inputs(store, inc.id, now)
        assignments = []
        for a in store.for_incident(inc.id):
            if a.facility:
                busy.setdefault(a.facility, []).append(str(inc.id))
            assignments.append({"id": str(a.id), "agency": a.agency.value, "state": a.state.value, "unit_ref": a.unit_ref,
                                "facility": a.facility, "eta_s": a.eta_s,
                                "left_at": a.acked_at.isoformat() if a.acked_at else None})
        incidents.append({"id": str(inc.id), "state": inc.state.value, "severity": inc.severity.value,
                          "confidence": inc.confidence, "lat": inc.location.lat, "lon": inc.location.lon,
                          "segment": info["segment"] if info else None, "lanes_closed": len(inc.lanes),
                          "opened_at": inc.opened_at.isoformat(), "cameras": sorted(inc.cameras),
                          "assignments": assignments, "notices": notices(store, inc.id)})

    showing = {dev: text for plan in store.plans.values() if not plan.reverted for dev, text in plan.messages.items()}
    return {
        "generated_at": now.isoformat(),
        "forecast_model": getattr(forecast, "version", None),
        "segments": segments,
        "facilities": [{"id": f.id, "name": f.name, "agency": f.agency.value, "lat": f.location.lat, "lon": f.location.lon,
                        "busy_with": busy.get(f.id, [])} for f in facilities],
        "cameras": [{"id": c.site.camera_id, "road": c.site.road, "lat": c.location.lat, "lon": c.location.lon,
                     "heading_deg": c.heading_deg} for c in CAMERAS.values()],
        "vms": [{"id": dev, "lat": lat, "lon": lon, "text": showing.get(dev)} for dev, (lat, lon) in VMS_LOCATIONS.items()],
        "incidents": incidents,
    }


def notices(store: Store, incident_id, limit: int = 12) -> list[dict]:
    """The latest alerts sent about an incident and who they went to, newest first."""
    who = {c.id: c for c in store.contacts}
    rows = sorted((n for n in store.notifications.values() if n.incident_id == incident_id),
                  key=lambda n: n.created_at, reverse=True)[:limit]
    return [{"contact": n.contact_name, "role": getattr(who.get(n.contact_id), "role", None),
             "agency": getattr(getattr(who.get(n.contact_id), "agency", None), "value", None),
             "channel": n.channel, "kind": n.kind, "status": n.status, "at": n.created_at.isoformat()} for n in rows]


def _level_now(speed: float | None, free_flow: float) -> str | None:
    if speed is None:
        return None
    from atms_forecast.features import level

    return str(level(speed, free_flow))
