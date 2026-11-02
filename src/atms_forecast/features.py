"""Features for one forecast: what is known at time t, and the speed h minutes later.

Everything here uses only data at or before t, plus a weather forecast for t+h and the typical speed for the
target time of day. The typical-speed profile is fitted on training data only, so it cannot leak the test period.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .segments import LOCAL_TZ, SEGMENTS, STEP_MIN

HORIZONS = (15, 30, 60)
RAIN_FORECAST_ERROR = 0.35      # relative noise on the future rain feature, so training sees a realistic forecast
FEATURES = [
    "speed_now", "speed_lag5", "speed_lag10", "speed_lag15", "speed_lag30", "speed_lag60",
    "speed_mean30", "speed_std30", "speed_min30", "speed_mean60", "trend15", "flow_now", "flow_trend15",
    "ratio_now", "hour_sin", "hour_cos", "dow", "is_weekend",
    "rain_now", "visibility_now", "rain_at_target",
    "incident_lanes_closed", "incident_age_min", "upstream_incident_lanes", "downstream_incident_lanes",
    "hist_now", "hist_target", "hist_gap", "segment_code", "free_flow",
]
LABELS = {
    "speed_now": "current speed", "speed_lag5": "speed 5 min ago", "speed_lag10": "speed 10 min ago",
    "speed_lag15": "speed 15 min ago", "speed_lag30": "speed 30 min ago", "speed_lag60": "speed an hour ago",
    "speed_mean30": "average speed, last 30 min", "speed_std30": "speed variability, last 30 min",
    "speed_min30": "lowest speed, last 30 min", "speed_mean60": "average speed, last hour",
    "trend15": "speed trend, last 15 min", "flow_now": "traffic volume", "flow_trend15": "volume trend, last 15 min",
    "ratio_now": "current speed against free flow", "hour_sin": "time of day", "hour_cos": "time of day",
    "dow": "day of week", "is_weekend": "weekend", "rain_now": "rain now", "visibility_now": "visibility now",
    "rain_at_target": "rain forecast for that time", "incident_lanes_closed": "lanes closed by an incident",
    "incident_age_min": "time since the incident began", "upstream_incident_lanes": "incident upstream",
    "downstream_incident_lanes": "incident downstream", "hist_now": "typical speed now",
    "hist_target": "typical speed at that time of day", "hist_gap": "today against typical",
    "segment_code": "which road segment", "free_flow": "free flow speed",
}


def level(speed, free_flow):
    """free, moderate, heavy or severe from speed as a share of free flow."""
    r = np.asarray(speed, dtype=float) / np.asarray(free_flow, dtype=float)
    return np.select([r >= 0.8, r >= 0.6, r >= 0.4], ["free", "moderate", "heavy"], "severe")


def level_rank(lv) -> np.ndarray:
    return pd.Series(lv).map({"free": 0, "moderate": 1, "heavy": 2, "severe": 3}).to_numpy()


class HistProfile:
    """Typical speed by segment, day of week and quarter hour, from clean training data."""

    def __init__(self, table: dict[str, float], fallback: dict[str, float]):
        self.table, self.fallback = table, fallback

    @staticmethod
    def _bins(ts: pd.Series | pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
        local = pd.DatetimeIndex(ts).tz_convert(LOCAL_TZ)
        return local.dayofweek.to_numpy(), ((local.hour * 60 + local.minute) // 15).to_numpy()

    @classmethod
    def fit(cls, df: pd.DataFrame) -> "HistProfile":
        clean = df[(df.incident_lanes_closed == 0) & (df.rain_mm_h < 1.0)]
        dow, b = cls._bins(clean.ts)
        key = clean.segment.to_numpy() + "|" + dow.astype(str) + "|" + b.astype(str)
        means = clean.groupby(key).speed_kmh.mean()
        return cls(means.to_dict(), clean.groupby("segment").speed_kmh.mean().to_dict())

    def at(self, segment: pd.Series, ts) -> np.ndarray:
        dow, b = self._bins(ts)
        keys = segment.to_numpy() + "|" + dow.astype(str) + "|" + b.astype(str)
        fb = segment.map(self.fallback).to_numpy()
        return np.array([self.table.get(k, f) for k, f in zip(keys, fb)])

    def to_json(self) -> dict:
        return {"table": self.table, "fallback": self.fallback}

    @classmethod
    def from_json(cls, d: dict) -> "HistProfile":
        return cls(d["table"], d["fallback"])


def regular(df: pd.DataFrame, segment: str) -> pd.DataFrame:
    """One segment on a complete 5 minute grid. Short sensor gaps are carried forward, long ones stay empty."""
    g = df[df.segment == segment].drop_duplicates("ts").set_index("ts").sort_index()
    if g.empty:
        return g
    grid = pd.date_range(g.index.min(), g.index.max(), freq=f"{STEP_MIN}min", tz="UTC")
    g = g.reindex(grid)
    g[["speed_kmh", "flow_vph"]] = g[["speed_kmh", "flow_vph"]].ffill(limit=3)
    g[["rain_mm_h", "visibility_km"]] = g[["rain_mm_h", "visibility_km"]].ffill(limit=12)
    g[["incident_lanes_closed", "incident_age_min"]] = g[["incident_lanes_closed", "incident_age_min"]].ffill(limit=3).fillna(0)
    g["segment"] = segment
    return g


def _frame(g: pd.DataFrame, i: int, horizon: int, hist: HistProfile, free_flow: dict[str, float],
           neighbour_lanes: dict[str, pd.Series], seg_code: dict[str, int], rain_target: pd.Series | None,
           rng: np.random.Generator | None) -> pd.DataFrame:
    seg = g["segment"].iloc[0]
    s, f = g["speed_kmh"], g["flow_vph"]
    k = horizon // STEP_MIN
    local = g.index.tz_convert(LOCAL_TZ)
    hod = (local.hour + local.minute / 60).to_numpy()
    seg_series = pd.Series(seg, index=g.index)
    target_ts = g.index + pd.Timedelta(minutes=horizon)
    if rain_target is None:
        rain_target = g["rain_mm_h"].shift(-k)
        if rng is not None:                                    # training: a forecast is never perfect
            rain_target = rain_target * rng.lognormal(0, RAIN_FORECAST_ERROR, len(g))
    hist_now, hist_t = hist.at(seg_series, g.index), hist.at(seg_series, target_ts)
    out = pd.DataFrame({
        "speed_now": s, "speed_lag5": s.shift(1), "speed_lag10": s.shift(2), "speed_lag15": s.shift(3),
        "speed_lag30": s.shift(6), "speed_lag60": s.shift(12),
        "speed_mean30": s.rolling(6, min_periods=4).mean(), "speed_std30": s.rolling(6, min_periods=4).std(),
        "speed_min30": s.rolling(6, min_periods=4).min(), "speed_mean60": s.rolling(12, min_periods=8).mean(),
        "trend15": s - s.shift(3), "flow_now": f, "flow_trend15": f - f.shift(3),
        "ratio_now": s / free_flow[seg],
        "hour_sin": np.sin(2 * np.pi * hod / 24), "hour_cos": np.cos(2 * np.pi * hod / 24),
        "dow": local.dayofweek.to_numpy(), "is_weekend": (local.dayofweek >= 5).astype(int),
        "rain_now": g["rain_mm_h"], "visibility_now": g["visibility_km"], "rain_at_target": rain_target,
        "incident_lanes_closed": g["incident_lanes_closed"], "incident_age_min": g["incident_age_min"],
        "upstream_incident_lanes": neighbour_lanes.get(f"up:{seg}", pd.Series(0, index=g.index)).reindex(g.index).fillna(0),
        "downstream_incident_lanes": neighbour_lanes.get(f"down:{seg}", pd.Series(0, index=g.index)).reindex(g.index).fillna(0),
        "hist_now": hist_now, "hist_target": hist_t, "hist_gap": s - hist_now,
        "segment_code": seg_code[seg], "free_flow": free_flow[seg],
    }, index=g.index)
    out["segment"] = seg
    out["target"] = s.shift(-k)
    out["target_ts"] = target_ts
    return out


def neighbour_incidents(grids: dict[str, pd.DataFrame], order: list[str]) -> dict[str, pd.Series]:
    """Lanes closed on the segment just upstream and just downstream, aligned to each segment's grid."""
    out: dict[str, pd.Series] = {}
    for i, seg in enumerate(order):
        if i > 0 and order[i - 1] in grids:
            out[f"up:{seg}"] = grids[order[i - 1]]["incident_lanes_closed"]
        if i + 1 < len(order) and order[i + 1] in grids:
            out[f"down:{seg}"] = grids[order[i + 1]]["incident_lanes_closed"]
    return out


def build(df: pd.DataFrame, horizon: int, hist: HistProfile, free_flow: dict[str, float], seed: int | None = 0,
          order: list[str] | None = None, rain_target: dict[str, float] | None = None) -> pd.DataFrame:
    """Rows of features (and the target when known) for every time step of every segment in df."""
    order = order or [s.id for s in SEGMENTS]
    seg_code = {sid: i for i, sid in enumerate(order)}
    present = [s for s in order if (df.segment == s).any()]
    grids = {s: regular(df, s) for s in present}
    neigh = neighbour_incidents(grids, order)
    rng = np.random.default_rng(seed) if seed is not None else None
    frames = []
    for seg in present:
        rt = None
        if rain_target is not None and seg in rain_target:
            rt = pd.Series(rain_target[seg], index=grids[seg].index)
        frames.append(_frame(grids[seg], order.index(seg), horizon, hist, free_flow, neigh, seg_code, rt, rng))
    return pd.concat(frames)
