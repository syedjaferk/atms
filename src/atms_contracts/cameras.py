"""Where the roadside cameras are. Mock positions along the Pune stretch of NH48, each one looking at a road segment.

Both the edge tools (to place a detection) and the operator map (to draw the camera) read this one list.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Location, Site


@dataclass(frozen=True)
class Camera:
    site: Site
    location: Location
    heading_deg: float


def _cam(camera_id: str, lat: float, lon: float) -> Camera:
    return Camera(Site(state="MH", city="PUNE", road="NH48", camera_id=camera_id), Location(lat=lat, lon=lon), 350.0)


CAMERAS: dict[str, Camera] = {c.site.camera_id: c for c in (
    _cam("GNT001", 18.4575, 73.8508),
    _cam("GNT002", 18.4586, 73.8507),    # about 120 m further along the same carriageway, sees the same crash
    _cam("GNT003", 18.4440, 73.8508),
    _cam("GNT004", 18.4510, 73.8508),
    _cam("GNT005", 18.4660, 73.8508),
    _cam("GNT006", 18.4735, 73.8508),
    _cam("GNT007", 18.4805, 73.8508),
)}
