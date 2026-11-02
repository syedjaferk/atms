"""The road segments the forecaster knows about. Illustrative geometry for NH48 near Pune."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    id: str
    road_segment: str          # the incident road key, MH/PUNE/NH48
    lat: float
    lon: float
    free_flow_kmh: float
    lanes: int = 3


# Traffic flows from S01 towards S06. A queue from an incident in S03 grows back into S02 and S01.
SEGMENTS: list[Segment] = [
    Segment("NH48-S01", "MH/PUNE/NH48", 18.4425, 73.8508, 98.0),
    Segment("NH48-S02", "MH/PUNE/NH48", 18.4500, 73.8508, 96.0),
    Segment("NH48-S03", "MH/PUNE/NH48", 18.4575, 73.8508, 100.0),    # camera GNT001 looks at this one
    Segment("NH48-S04", "MH/PUNE/NH48", 18.4650, 73.8508, 94.0),
    Segment("NH48-S05", "MH/PUNE/NH48", 18.4725, 73.8508, 97.0),
    Segment("NH48-S06", "MH/PUNE/NH48", 18.4800, 73.8508, 95.0),
]
BY_ID = {s.id: s for s in SEGMENTS}
LOCAL_TZ = "Asia/Kolkata"
STEP_MIN = 5
