"""Correlator. Decides whether a detection is a new incident, more evidence for an open one, or noise."""
from __future__ import annotations

from atms_contracts.models import AccidentDetected, Envelope, VideoReady

from .geo import bearing_diff_deg, haversine_m
from .incident_service import IncidentService
from .state import CLOSED_FOR_MERGE
from .store import Incident


class Correlator:
    def __init__(self, service: IncidentService):
        self.service = service
        self.store = service.store
        self.policy = service.policy

    def match(self, ev: AccidentDetected) -> Incident | None:
        d, p = ev.data, self.policy
        road = f"{d.site.state}/{d.site.city}/{d.site.road}"
        best: tuple[float, Incident] | None = None
        for inc in self.store.incidents.values():
            if inc.state in CLOSED_FOR_MERGE or inc.road_segment != road:
                continue
            if abs((ev.occurred_at - inc.last_detection_at).total_seconds()) > p.merge_window_s:
                continue
            if bearing_diff_deg(d.heading_deg, inc.heading_deg) > p.merge_max_heading_diff_deg:
                continue                                  # other carriageway
            dist = haversine_m(d.location, inc.location)
            if dist <= p.merge_radius_m and (best is None or dist < best[0]):
                best = (dist, inc)
        return best[1] if best else None

    async def on_event(self, ev: Envelope) -> None:
        if isinstance(ev, AccidentDetected):
            if ev.event_id in self.store.detections or ev.event_id in self.store.low_confidence_ids:
                return                                        # already handled, this is a redelivery
            inc = self.match(ev)
            if inc:
                self.service.attach_detection(inc, ev)
            elif ev.data.confidence < self.policy.review_confidence:
                self.store.record_low_confidence(ev.event_id)   # logged for model tuning, no incident
                self.store.commit()
            else:
                self.service.create_from_detection(ev)
        elif isinstance(ev, VideoReady):
            # the clip belongs to the incident that holds the newest detection from that camera
            camera = ev.data.camera_id
            owners = [(eid, iid) for eid, (iid, cam) in self.store.detections.items() if cam == camera]
            if owners:
                inc = self.store.incidents[owners[-1][1]]
                self.service.attach_video(inc, camera, ev.data.video_ref, ev.data.sha256)
