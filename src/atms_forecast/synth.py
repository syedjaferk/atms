"""Synthetic traffic, because no real sensor history exists yet.

The generator has real structure to learn: weekday and weekend demand, day to day variation, rain that
lowers speed and capacity, incidents that close lanes and push a queue back upstream, queues that form
quickly and clear slowly, sensor noise and gaps. A model can beat the naive baselines here only by
learning that structure, which is what makes the backtest meaningful. It says nothing about how the same
model would do on a real road until it is retrained on real data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .segments import LOCAL_TZ, SEGMENTS, STEP_MIN, Segment

CAPACITY_PER_LANE = 2000.0
PEAK_DEMAND_VPH = 6100.0


def demand_shape(hour: np.ndarray, weekend: np.ndarray) -> np.ndarray:
    g = lambda mu, sd: np.exp(-((hour - mu) / sd) ** 2)
    weekday = 0.28 + 0.62 * g(8.25, 1.2) + 0.72 * g(17.75, 1.5) + 0.20 * g(13.0, 2.5)
    weekend_ = 0.25 + 0.45 * g(12.5, 3.0) + 0.10 * g(19.0, 2.0)
    return np.where(weekend, weekend_, weekday)


def speed_ratio(x: np.ndarray) -> np.ndarray:
    """Share of free flow speed at volume over capacity x."""
    return 1.0 / (1.0 + 0.6 * (np.maximum(x, 0) / 0.95) ** 5)


def make_weather(n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    rain = np.zeros(n)
    per_day = 24 * 60 // STEP_MIN
    for day_start in range(0, n, per_day):
        if rng.random() < 0.55:
            start = day_start + int(rng.integers(0, per_day))
            length = int(rng.integers(12, 60))                    # 1 to 5 hours
            peak = rng.uniform(1.5, 14.0)
            shape = np.sin(np.linspace(0.1, np.pi - 0.1, length)) * peak
            end = min(n, start + length)
            rain[start:end] += shape[: end - start] * rng.uniform(0.7, 1.2, end - start)
    visibility = np.clip(10.0 - 0.55 * rain + rng.normal(0, 0.2, n), 0.5, 10.0)
    return rain, visibility


def make_incidents(n: int, hour: np.ndarray, demand: np.ndarray, rng: np.random.Generator, per_day: float = 0.6):
    """Returns lanes closed and minutes since start, per step, for one segment."""
    lanes = np.zeros(n, dtype=int)
    age = np.zeros(n)
    weights = demand / demand.sum()
    for _ in range(rng.poisson(per_day * n / (24 * 60 // STEP_MIN))):
        start = int(rng.choice(n, p=weights))
        dur = int(np.clip(rng.lognormal(np.log(9), 0.5), 4, 30))     # steps, median 45 min
        closed = int(rng.choice([1, 2], p=[0.75, 0.25]))
        end = min(n, start + dur)
        lanes[start:end] = np.maximum(lanes[start:end], closed)
        age[start:end] = np.maximum(age[start:end], np.arange(end - start) * STEP_MIN)
    return lanes, age


def generate(seed: int = 7, days: int = 56, start: str = "2026-06-01", segments: list[Segment] | None = None) -> pd.DataFrame:
    segments = segments or SEGMENTS
    rng = np.random.default_rng(seed)
    n = days * 24 * 60 // STEP_MIN
    local = pd.date_range(start, periods=n, freq=f"{STEP_MIN}min", tz=LOCAL_TZ)
    hour = (local.hour + local.minute / 60).to_numpy()
    weekend = (local.dayofweek >= 5)
    rain, visibility = make_weather(n, rng)
    day_factor = np.exp(rng.normal(0, 0.07, days))[(np.arange(n) * STEP_MIN // (24 * 60)).astype(int)]
    base = demand_shape(hour, weekend) * day_factor

    lanes_closed, age = {}, {}
    for s in segments:
        lanes_closed[s.id], age[s.id] = make_incidents(n, hour, base, rng)

    frames = []
    for i, s in enumerate(segments):
        scale = rng.uniform(0.88, 1.08)
        demand = base * PEAK_DEMAND_VPH * scale * rng.normal(1.0, 0.03, n)
        rain_cap = 1 - 0.012 * np.minimum(rain, 8)
        rain_ff = 1 - 0.015 * np.minimum(rain, 8)
        lane_share = (s.lanes - lanes_closed[s.id]) / s.lanes
        cap = s.lanes * CAPACITY_PER_LANE * rain_cap * np.where(lanes_closed[s.id] > 0, lane_share * 0.9, 1.0)
        # a queue from the next segment downstream spills back into this one, ten minutes late
        if i + 1 < len(segments):
            down = lanes_closed[segments[i + 1].id] > 0
            spill = np.roll(down, 2)
            spill[:2] = False
            cap = cap * np.where(spill, 0.78, 1.0)
        if i + 2 < len(segments):
            down2 = np.roll(lanes_closed[segments[i + 2].id] > 0, 4)
            down2[:4] = False
            cap = cap * np.where(down2, 0.92, 1.0)
        target = speed_ratio(demand / cap)
        ratio = np.empty(n)
        r = 1.0
        for t in range(n):                                   # queues form fast and clear slowly
            alpha = 0.40 if target[t] < r else 0.14
            r += alpha * (target[t] - r)
            ratio[t] = r
        speed = s.free_flow_kmh * rain_ff * ratio
        flow = np.where(demand > cap, cap * 0.88, demand)
        frames.append(pd.DataFrame({
            "ts": local.tz_convert("UTC"),
            "segment": s.id,
            "speed_kmh": np.clip(speed + rng.normal(0, 2.2, n), 3, 130),
            "flow_vph": np.maximum(0, flow * rng.normal(1.0, 0.04, n)),
            "rain_mm_h": rain,
            "visibility_km": visibility,
            "incident_lanes_closed": lanes_closed[s.id],
            "incident_age_min": age[s.id],
        }))
    df = pd.concat(frames, ignore_index=True)
    drop = rng.random(len(df)) < 0.015                      # sensors miss readings
    return df[~drop].sort_values(["segment", "ts"]).reset_index(drop=True)
