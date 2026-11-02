"""A traffic twin: a small physical model of the road that can be run forward under a made up situation.

The forecaster answers "what will happen" for the next hour from the data. The twin answers "what would happen
if" for hours ahead, including things the forecaster never saw: a planned closure, an event that adds traffic,
a faster clearance, opening the shoulder, diverting some drivers.

Each segment has a capacity and a queue. Demand above capacity builds a queue, a queue slows the segment, and
a slow segment cuts the capacity of the one behind it, so a queue grows backwards. Speed follows load through a
curve and relaxes toward it, quickly down and slowly up. All the numbers are fitted to readings (calibrate.py).

It is vectorised: many days or many scenarios run side by side.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .segments import LOCAL_TZ, SEGMENTS, STEP_MIN, Segment

STEP_H = STEP_MIN / 60
LENGTH_KM = 0.83
TAU_STEPS = 3
SPILL_DELAY = 2


@dataclass
class Params:
    cap_per_lane: float = 2000.0
    x0: float = 0.95
    k: float = 0.6
    n: float = 5.0
    alpha_down: float = 0.4
    alpha_up: float = 0.14
    rain_cap: float = 0.012
    rain_ff: float = 0.015
    inc_eff: float = 0.9
    spill: float = 0.3
    queue_gain: float = 1.0
    discharge: float = 0.88
    seg_scale: list[float] = field(default_factory=lambda: [1.0] * len(SEGMENTS))

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Params":
        return cls(**d)


def speed_ratio(x, p: Params):
    return 1.0 / (1.0 + p.k * (np.maximum(x, 0) / p.x0) ** p.n)


def load_for_ratio(ratio, p: Params):
    """Inverse of speed_ratio: the load that produces a given speed share."""
    r = np.clip(ratio, 0.02, 0.999)
    return p.x0 * ((1.0 / r - 1.0) / p.k) ** (1.0 / p.n)


def initial_queue(p: Params, ratio, d0, lanes):
    """A queue consistent with a starting speed: the load that produces it, less what is arriving."""
    x0 = load_for_ratio(ratio, p)
    q0 = np.maximum(0.0, x0 * lanes * p.cap_per_lane - d0) * (TAU_STEPS * STEP_H) / max(p.queue_gain, 0.05)
    return np.where(ratio > 0.85, 0.0, q0)


SPEED_FLOOR_KMH = 4.0                       # stop and go, never a standstill


def simulate(p: Params, demand, rain, lanes_closed, lanes, free_flow, init_ratio, extra_cap=None, init_queue=None):
    """demand, lanes_closed, extra_cap: [B, T, S]. rain: [B, T]. lanes, free_flow: [S]. init_ratio: [B, S].
    Returns speed_kmh, served_flow_vph, queue_veh, each [B, T, S]."""
    B, T, S = demand.shape
    scale = np.asarray(p.seg_scale)[:S]
    ratio = init_ratio.astype(float).copy()
    q = np.zeros((B, S)) if init_queue is None else init_queue.astype(float).copy()
    speed = np.empty((B, T, S))
    flow = np.empty((B, T, S))
    queue = np.empty((B, T, S))
    hist = [ratio.copy() for _ in range(SPILL_DELAY + 1)]
    tau_h = TAU_STEPS * STEP_H
    for t in range(T):
        r = np.minimum(rain[:, t], 8.0)[:, None]
        closed = lanes_closed[:, t, :]
        share = np.maximum((lanes - closed) / lanes, 0.08)
        cap = lanes * p.cap_per_lane * (1 - p.rain_cap * r) * np.where(closed > 0, share * p.inc_eff, 1.0)
        if extra_cap is not None:
            cap = cap + extra_cap[:, t, :]
        down = np.ones((B, S))
        down[:, :-1] = hist[-SPILL_DELAY][:, 1:]                       # the segment ahead, a moment ago
        cap = cap * (1 - p.spill * np.clip((0.9 - down) / 0.9, 0, 1))
        d = demand[:, t, :] * scale
        x = (d + p.queue_gain * q / tau_h) / cap
        target = speed_ratio(x, p)
        alpha = np.where(target < ratio, p.alpha_down, p.alpha_up)
        ratio = ratio + alpha * (target - ratio)
        out = np.where(x > 1.0, cap * p.discharge, np.minimum(d + q / STEP_H, cap))
        q = np.maximum(0.0, q + (d - out) * STEP_H)
        speed[:, t, :] = np.maximum(free_flow * (1 - p.rain_ff * r) * ratio, SPEED_FLOOR_KMH)
        flow[:, t, :] = out
        queue[:, t, :] = q
        hist.append(ratio.copy())
        hist.pop(0)
    return speed, flow, queue


# ------------------------------------------------------------------ scenarios


@dataclass
class IncidentSpec:
    segment: str
    at_min: float
    duration_min: float
    lanes_closed: int = 1


@dataclass
class DemandSpec:
    at_min: float
    duration_min: float
    factor: float
    segment: str | None = None          # None means every segment (an event, a holiday)


@dataclass
class WeatherSpec:
    at_min: float
    duration_min: float
    rain_mm_h: float


@dataclass
class Responses:
    clearance_speedup: float = 0.0      # 0.3 means incidents are cleared 30 percent sooner
    shoulder_lane: bool = False         # open the hard shoulder past the incident
    diversion_share: float = 0.0        # share of drivers who take another route after an advisory
    diversion_delay_min: float = 10.0   # how long after the incident the advisory takes effect


@dataclass
class Scenario:
    name: str = "scenario"
    incidents: list[IncidentSpec] = field(default_factory=list)
    demand: list[DemandSpec] = field(default_factory=list)
    weather: list[WeatherSpec] = field(default_factory=list)
    responses: Responses = field(default_factory=Responses)


class DemandProfile:
    """Typical arrivals per hour by segment, day of week and quarter hour."""

    def __init__(self, table: np.ndarray):
        self.table = table                                  # [7, 96, S]

    def at(self, local_times) -> np.ndarray:
        idx = [(t.weekday(), (t.hour * 60 + t.minute) // 15) for t in local_times]
        return np.stack([self.table[d, b] for d, b in idx])         # [T, S]

    def to_json(self) -> list:
        return self.table.round(1).tolist()

    @classmethod
    def from_json(cls, d) -> "DemandProfile":
        return cls(np.asarray(d, dtype=float))


@dataclass
class StartState:
    """Where the road is when the run begins. speeds None means a typical road at that time of day."""
    time: datetime
    speed_kmh: dict[str, float] | None = None
    rain_mm_h: float = 0.0
    demand_level: float = 1.0           # today's traffic against typical, from recent volume


class Twin:
    def __init__(self, params: Params, demand: DemandProfile, typical_speed: np.ndarray | None = None,
                 segments: list[Segment] | None = None):
        self.p, self.demand = params, demand
        self.segments = segments or SEGMENTS
        self.ids = [s.id for s in self.segments]
        self.lanes = np.array([s.lanes for s in self.segments], dtype=float)
        self.free_flow = np.array([s.free_flow_kmh for s in self.segments])
        self.typical_speed = typical_speed                   # [7, 96, S] used to start from a typical road

    # ------------------------------------------------------------ inputs

    def _times(self, state: StartState, horizon_min: int) -> list[datetime]:
        steps = horizon_min // STEP_MIN
        start = state.time.astimezone(__import__("zoneinfo").ZoneInfo(LOCAL_TZ))
        return [start + timedelta(minutes=STEP_MIN * i) for i in range(steps)]

    def _initial(self, state: StartState, local_first: datetime) -> tuple[np.ndarray, np.ndarray]:
        S = len(self.ids)
        if state.speed_kmh:
            speeds = np.array([state.speed_kmh.get(i, self.free_flow[k]) for k, i in enumerate(self.ids)])
        elif self.typical_speed is not None:
            speeds = self.typical_speed[local_first.weekday(), (local_first.hour * 60 + local_first.minute) // 15]
        else:
            speeds = self.free_flow.copy()
        ratio = np.clip(speeds / (self.free_flow * (1 - self.p.rain_ff * min(state.rain_mm_h, 8))), 0.03, 1.0)
        # a queue consistent with that speed: the load needed to produce it, less what is arriving
        d0 = self.demand.at([local_first])[0] * np.asarray(self.p.seg_scale)[:S] * state.demand_level
        return ratio, initial_queue(self.p, ratio, d0, self.lanes)

    def build(self, sc: Scenario, state: StartState, horizon_min: int):
        times = self._times(state, horizon_min)
        T, S = len(times), len(self.ids)
        demand = self.demand.at(times) * state.demand_level                     # [T, S]
        rain = np.zeros(T)
        decay = np.maximum(0.0, 1 - np.arange(T) * STEP_MIN / 60)               # current rain fades over an hour
        rain += state.rain_mm_h * decay
        closed = np.zeros((T, S))
        extra = np.zeros((T, S))
        for w in sc.weather:
            a, b = int(w.at_min // STEP_MIN), int((w.at_min + w.duration_min) // STEP_MIN)
            rain[a:b] = w.rain_mm_h
        for dmd in sc.demand:
            a, b = int(dmd.at_min // STEP_MIN), int((dmd.at_min + dmd.duration_min) // STEP_MIN)
            cols = range(S) if dmd.segment is None else [self.ids.index(dmd.segment)]
            for c in cols:
                demand[a:b, c] *= dmd.factor
        r = sc.responses
        for inc in sc.incidents:
            c = self.ids.index(inc.segment)
            dur = inc.duration_min * (1 - r.clearance_speedup)
            a, b = int(inc.at_min // STEP_MIN), max(int(inc.at_min // STEP_MIN) + 1, int((inc.at_min + dur) // STEP_MIN))
            closed[a:b, c] = np.maximum(closed[a:b, c], inc.lanes_closed)
            if r.shoulder_lane:
                extra[a:b, c] += 0.6 * self.p.cap_per_lane
            if r.diversion_share > 0:
                da = int((inc.at_min + r.diversion_delay_min) // STEP_MIN)
                db = b + 3
                for cc in (c, c - 1, c - 2):
                    if cc >= 0:
                        demand[da:db, cc] *= (1 - r.diversion_share)
        ratio0, q0 = self._initial(state, times[0])
        return (demand[None], rain[None], closed[None], extra[None], ratio0[None], q0[None], times)

    # ------------------------------------------------------------ running

    def run(self, sc: Scenario, state: StartState, horizon_min: int = 240) -> dict:
        demand, rain, closed, extra, r0, q0, times = self.build(sc, state, horizon_min)
        speed, flow, queue = simulate(self.p, demand, rain, closed, self.lanes, self.free_flow, r0, extra, q0)
        return {"scenario": sc.name, "times": times, "ids": self.ids, "speed": speed[0], "flow": flow[0], "queue": queue[0],
                "demand": demand[0] * np.asarray(self.p.seg_scale)[: len(self.ids)], "closed": closed[0]}


# ------------------------------------------------------------------ measuring a run


def level_of(ratio):
    return np.select([ratio >= 0.8, ratio >= 0.6, ratio >= 0.4], ["free", "moderate", "heavy"], "severe")


def kpis(run: dict, free_flow: np.ndarray, last_disruption_end_min: float | None = None) -> dict:
    sp, dem = run["speed"], run["demand"]
    ratio = sp / free_flow
    vehicles = dem * STEP_H
    delay_h = vehicles * LENGTH_KM * (1 / np.maximum(sp, 3) - 1 / free_flow)
    delay_h = np.maximum(delay_h, 0)
    slow = (sp < 40).sum(axis=1) * LENGTH_KM                       # queue length in km at each step
    heavy_steps = (ratio < 0.6).sum(axis=0) * STEP_MIN
    travel = (LENGTH_KM / np.maximum(sp, 3)).sum(axis=1) * 60       # end to end minutes
    free_travel = (LENGTH_KM / free_flow).sum() * 60
    out = {
        "vehicle_hours_lost": round(float(delay_h.sum()), 1),
        "vehicles": int(vehicles.sum()),
        "avg_delay_per_vehicle_min": round(float(delay_h.sum() / max(vehicles.sum(), 1) * 60), 2),
        "longest_queue_km": round(float(slow.max()), 2),
        "worst_end_to_end_minutes": round(float(travel.max()), 1),
        "free_flow_end_to_end_minutes": round(float(free_travel), 1),
        "segments_ever_heavy": int((heavy_steps > 0).sum()),
        "minutes_heavy_or_worse_by_segment": {i: int(m) for i, m in zip(run["ids"], heavy_steps)} if "ids" in run else None,
        "minutes_heavy_or_worse_total": int(heavy_steps.sum()),
    }
    if last_disruption_end_min is not None:
        step0 = int(last_disruption_end_min // STEP_MIN)
        ok = (ratio >= 0.8).all(axis=1)
        later = np.where(ok[step0:])[0]
        out["minutes_to_recover_after_clearance"] = int(later[0] * STEP_MIN) if len(later) else None
    return out
