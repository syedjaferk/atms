"""What-if service. Runs scenarios on the twin from the road as it is now, or from a typical day, and compares them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from .calibrate import PER_DAY
from .segments import BY_ID, STEP_MIN
from .twin import (DemandSpec, IncidentSpec, Responses, Scenario, StartState, Twin, WeatherSpec, kpis, level_of)

MAX_HORIZON_MIN = 720
LIVE_WINDOW = timedelta(minutes=15)
SERIES_EVERY = 3                      # one point per quarter hour in the answer
EXPECTED_CLEARANCE_MIN = {"minor": 30, "major": 60, "critical": 90}


class WhatIfError(ValueError):
    pass


def _series(run: dict, free_flow: np.ndarray) -> dict:
    sp = run["speed"][::SERIES_EVERY]
    return {"minutes": [i * STEP_MIN * SERIES_EVERY for i in range(len(sp))], "segments": run["ids"],
            "speed_kmh": np.round(sp, 1).tolist(),
            "level": [[str(x) for x in row] for row in level_of(sp / free_flow)]}


def _last_disruption_end(sc: Scenario) -> float | None:
    ends = [i.at_min + i.duration_min * (1 - sc.responses.clearance_speedup) for i in sc.incidents]
    return max(ends) if ends else None


def validate_scenario(sc: Scenario, horizon_min: int) -> None:
    if not 30 <= horizon_min <= MAX_HORIZON_MIN:
        raise WhatIfError(f"horizon must be between 30 and {MAX_HORIZON_MIN} minutes")
    for i in sc.incidents:
        if i.segment not in BY_ID:
            raise WhatIfError(f"unknown segment {i.segment}")
        if not 0 <= i.lanes_closed <= 3 or i.duration_min <= 0 or i.at_min < 0:
            raise WhatIfError("incident needs lanes_closed 0 to 3, a positive duration and a start of zero or more")
    for d in sc.demand:
        if d.segment is not None and d.segment not in BY_ID:
            raise WhatIfError(f"unknown segment {d.segment}")
        if not 0.1 <= d.factor <= 3.0 or d.duration_min <= 0 or d.at_min < 0:
            raise WhatIfError("demand factor must be between 0.1 and 3")
    r = sc.responses
    if not 0 <= r.clearance_speedup <= 0.8 or not 0 <= r.diversion_share <= 0.5:
        raise WhatIfError("clearance_speedup is at most 0.8 and diversion_share at most 0.5")


class WhatIf:
    def __init__(self, twin: Twin, store, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 validation: dict | None = None):
        self.twin, self.store, self.clock, self.validation = twin, store, clock, validation

    # ------------------------------------------------------------ where the road is

    def start_state(self, start: datetime | None) -> tuple[StartState, dict]:
        now = self.clock()
        start = start or now
        if abs(start - now) <= LIVE_WINDOW:
            speeds, rain, level = {}, 0.0, 1.0
            for sid in self.twin.ids:
                df = self.store.recent(sid, now - timedelta(minutes=30), now)
                if not df.empty and now - df.ts.max() <= LIVE_WINDOW:
                    speeds[sid] = float(df.speed_kmh.iloc[-3:].mean())
            w = self.store.weather_now(now)
            if w:
                rain = float(w[0])
            if len(speeds) >= len(self.twin.ids) - 1:
                level = self._demand_level(now, speeds)
                return StartState(now, speeds, rain, level), {"mode": "live", "time": now.isoformat(), "speeds_kmh": {k: round(v, 1) for k, v in speeds.items()},
                                                               "rain_mm_h": rain, "demand_against_typical": round(level, 2)}
        return StartState(start), {"mode": "typical day", "time": start.isoformat(), "speeds_kmh": None, "rain_mm_h": 0.0,
                                   "demand_against_typical": 1.0}

    def _demand_level(self, now: datetime, speeds: dict[str, float]) -> float:
        """Today's traffic against a typical day, from segments that are flowing so volume is not capped by capacity."""
        ratios = []
        for k, sid in enumerate(self.twin.ids):
            if speeds.get(sid, 0) / self.twin.free_flow[k] < 0.85:
                continue
            df = self.store.recent(sid, now - timedelta(minutes=30), now)
            typical = self.twin.demand.at([now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))])[0][k]
            if not df.empty and typical > 300:
                ratios.append(float(df.flow_vph.mean()) / float(typical))
        return float(np.clip(np.median(ratios), 0.7, 1.3)) if ratios else 1.0

    # ------------------------------------------------------------ running

    def compare(self, scenarios: list[Scenario], horizon_min: int = 240, start: datetime | None = None) -> dict:
        for s in scenarios:
            validate_scenario(s, horizon_min)
        state, info = self.start_state(start)
        ff = self.twin.free_flow
        base_run = self.twin.run(Scenario("normal day"), state, horizon_min)
        base = kpis(base_run, ff)
        out = {"start": info, "horizon_min": horizon_min, "baseline": {"name": "normal day, nothing goes wrong", "kpis": base,
               "series": _series(base_run, ff)}, "scenarios": []}
        for sc in scenarios:
            run = self.twin.run(sc, state, horizon_min)
            k = kpis(run, ff, _last_disruption_end(sc))
            out["scenarios"].append({
                "name": sc.name, "kpis": k, "series": _series(run, ff),
                "vs_normal_day": {"extra_vehicle_hours_lost": round(k["vehicle_hours_lost"] - base["vehicle_hours_lost"], 1),
                                  "extra_delay_per_vehicle_min": round(k["avg_delay_per_vehicle_min"] - base["avg_delay_per_vehicle_min"], 2),
                                  "longer_worst_trip_min": round(k["worst_end_to_end_minutes"] - base["worst_end_to_end_minutes"], 1)},
            })
        ranked = sorted(out["scenarios"], key=lambda s: s["kpis"]["vehicle_hours_lost"])
        out["lowest_delay"] = ranked[0]["name"] if ranked else None
        out["model_check"] = self.validation
        return out

    # ------------------------------------------------------------ an incident that is happening now

    def incident_options(self, segment: str, lanes_closed: int, severity: str, elapsed_min: float) -> dict:
        """The decision an operator faces: how to handle this incident. Same road state, same incident, different responses."""
        remaining = max(15.0, EXPECTED_CLEARANCE_MIN.get(severity, 60) - elapsed_min)
        inc = lambda dur=remaining: [IncidentSpec(segment, 0, dur, max(1, lanes_closed))]
        opts = [
            Scenario("as things stand", inc()),
            Scenario("clear 30 percent faster", inc(), responses=Responses(clearance_speedup=0.3)),
            Scenario("open the shoulder lane", inc(), responses=Responses(shoulder_lane=True)),
            Scenario("advise 15 percent to divert", inc(), responses=Responses(diversion_share=0.15, diversion_delay_min=10)),
            Scenario("shoulder lane and diversion", inc(), responses=Responses(shoulder_lane=True, diversion_share=0.15, diversion_delay_min=10)),
            Scenario("everything, and clear 30 percent faster", inc(),
                     responses=Responses(clearance_speedup=0.3, shoulder_lane=True, diversion_share=0.15, diversion_delay_min=10)),
        ]
        horizon = int(min(MAX_HORIZON_MIN, max(180, remaining * 3 + 60)))
        res = self.compare(opts, horizon)
        asis = next(s for s in res["scenarios"] if s["name"] == "as things stand")
        worse = self.compare([Scenario("10 minutes longer", inc(remaining + 10))], horizon)["scenarios"][0]
        for s in res["scenarios"]:
            s["saves_vs_as_things_stand"] = {
                "vehicle_hours": round(asis["kpis"]["vehicle_hours_lost"] - s["kpis"]["vehicle_hours_lost"], 1),
                "worst_trip_min": round(asis["kpis"]["worst_end_to_end_minutes"] - s["kpis"]["worst_end_to_end_minutes"], 1),
                "queue_km": round(asis["kpis"]["longest_queue_km"] - s["kpis"]["longest_queue_km"], 2)}
        res["assumptions"] = {"segment": segment, "lanes_closed": max(1, lanes_closed), "expected_minutes_remaining": round(remaining),
                              "basis": f"typical clearance for a {severity} incident is {EXPECTED_CLEARANCE_MIN.get(severity, 60)} minutes, {round(elapsed_min)} have passed"}
        res["cost_of_delay"] = {"each_extra_10_min_closed_costs_vehicle_hours": round(worse["kpis"]["vehicle_hours_lost"] - asis["kpis"]["vehicle_hours_lost"], 1)}
        best = min(res["scenarios"], key=lambda s: s["kpis"]["vehicle_hours_lost"])
        res["recommendation"] = {"option": best["name"], "saves_vehicle_hours": best["saves_vs_as_things_stand"]["vehicle_hours"],
                                 "note": "The twin assumes the shoulder can be opened and that advised drivers do divert. Check both on site."}
        return res
