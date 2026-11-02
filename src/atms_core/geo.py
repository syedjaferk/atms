"""Facilities and ETA. Seed data is illustrative. PostGIS and a routing engine replace this later."""
from __future__ import annotations

import math
from dataclasses import dataclass

from atms_contracts import network
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


SEED = [Facility(st.id, st.name, st.agency, st.location) for st in network.load().stations]


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
