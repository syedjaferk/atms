"""Facilities and ETA. Seed data is illustrative. PostGIS and a routing engine replace this later."""
from __future__ import annotations

import math
from dataclasses import dataclass

from atms_contracts.models import Agency, Location

EARTH_M = 6_371_000
ROAD_FACTOR = 1.3          # road distance over straight line
AVG_SPEED_MS = 50 / 3.6    # emergency vehicle average, 50 km/h


def haversine_m(a: Location, b: Location) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


def bearing_diff_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    agency: Agency
    location: Location


@dataclass(frozen=True)
class Ranked:
    facility: Facility
    distance_m: float
    eta_s: int


SEED = [
    Facility("amb-bharati", "Bharati Hospital", Agency.ambulance, Location(lat=18.4637, lon=73.8586)),
    Facility("amb-sassoon", "Sassoon General Hospital", Agency.ambulance, Location(lat=18.5276, lon=73.8707)),
    Facility("amb-ruby", "Ruby Hall Clinic", Agency.ambulance, Location(lat=18.5314, lon=73.8774)),
    Facility("pol-katraj", "Katraj Police Station", Agency.police, Location(lat=18.4529, lon=73.8650)),
    Facility("pol-sahakar", "Sahakarnagar Police Station", Agency.police, Location(lat=18.4950, lon=73.8553)),
    Facility("hwy-katraj", "Highway Patrol Katraj", Agency.highway_patrol, Location(lat=18.4560, lon=73.8480)),
    Facility("hwy-warje", "Highway Patrol Warje", Agency.highway_patrol, Location(lat=18.4830, lon=73.8000)),
    Facility("fire-katraj", "Katraj Fire Station", Agency.fire, Location(lat=18.4478, lon=73.8590)),
    Facility("fire-kondhwa", "Kondhwa Fire Station", Agency.fire, Location(lat=18.4720, lon=73.8890)),
    Facility("tow-katraj", "Katraj Tow Depot", Agency.tow, Location(lat=18.4505, lon=73.8420)),
    Facility("tow-warje", "Warje Tow Depot", Agency.tow, Location(lat=18.4800, lon=73.8000)),
]


class FacilityService:
    def __init__(self, facilities: list[Facility] | None = None):
        self.facilities = facilities if facilities is not None else SEED

    def all(self) -> list[Facility]:
        return list(self.facilities)

    def nearest(self, agency: Agency, where: Location, n: int = 3) -> list[Ranked]:
        ranked = [
            Ranked(f, d, round(d * ROAD_FACTOR / AVG_SPEED_MS))
            for f in self.facilities if f.agency == agency
            for d in [haversine_m(where, f.location)]
        ]
        return sorted(ranked, key=lambda r: r.distance_m)[:n]
