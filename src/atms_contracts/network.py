"""The network the system watches. One JSON file lists the areas, the cameras in each area and the stations that answer there.

Everything that needs a place (the edge tools, the core, the map, the video catalog) reads it from here, so a new city is a data
change and not a code change. Positions are illustrative.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from .models import Agency, Location, Site


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    state: str
    city: str
    road: str
    road_name: str
    center: Location
    corridor: tuple[tuple[float, float], ...]     # the cameras in order along the road, drawn as the road where no sensors exist

    @property
    def road_segment(self) -> str:
        return f"{self.state}/{self.city}/{self.road}"


@dataclass(frozen=True)
class Camera:
    site: Site
    location: Location
    heading_deg: float
    name: str = ""
    area: str = ""


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    agency: Agency
    location: Location
    area: str


@dataclass(frozen=True)
class Network:
    areas: dict[str, Area]
    cameras: dict[str, Camera]
    stations: list[Station]

    def cameras_in(self, area: str) -> list[Camera]:
        return [c for c in self.cameras.values() if c.area == area]


@lru_cache(maxsize=1)
def load() -> Network:
    raw = json.loads(resources.files("atms_contracts").joinpath("data/network.json").read_text())
    cameras: dict[str, Camera] = {}
    areas: dict[str, Area] = {}
    for a in raw["areas"]:
        mine = [c for c in raw["cameras"] if c["area"] == a["id"]]
        if not mine:
            raise ValueError(f"area {a['id']} has no camera")
        lat = sum(c["lat"] for c in mine) / len(mine)
        lon = sum(c["lon"] for c in mine) / len(mine)
        areas[a["id"]] = Area(a["id"], a["name"], a["state"], a["city"], a["road"], a["road_name"], Location(lat=lat, lon=lon),
                              tuple((c["lat"], c["lon"]) for c in sorted(mine, key=lambda c: -c["lat"])))
    for c in raw["cameras"]:
        a = areas[c["area"]]
        cameras[c["id"]] = Camera(Site(state=a.state, city=a.city, road=a.road, camera_id=c["id"]),
                                  Location(lat=c["lat"], lon=c["lon"]), float(c["heading"]), c["name"], a.id)
    stations = [Station(f["id"], f["name"], Agency(f["agency"]), Location(lat=f["lat"], lon=f["lon"]), f["area"]) for f in raw["facilities"]]
    for s in stations:
        if s.area not in areas:
            raise ValueError(f"station {s.id} is in unknown area {s.area}")
    return Network(areas, cameras, stations)
