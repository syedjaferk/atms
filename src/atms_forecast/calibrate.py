"""Fit the twin to readings and check it on days it has not seen."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from .features import regular
from .segments import LOCAL_TZ, SEGMENTS, STEP_MIN
from .twin import (STEP_H, DemandProfile, Params, Twin, initial_queue, simulate, SPEED_FLOOR_KMH)

PER_DAY = 24 * 60 // STEP_MIN
NAMES = ["cap_per_lane", "x0", "k", "n", "alpha_down", "alpha_up", "rain_cap", "rain_ff", "inc_eff", "spill", "queue_gain", "discharge"]
LOW = [1500, 0.7, 0.2, 2.0, 0.1, 0.03, 0.0, 0.0, 0.6, 0.0, 0.0, 0.7]
HIGH = [2600, 1.3, 1.5, 9.0, 0.9, 0.5, 0.03, 0.03, 1.0, 1.0, 2.0, 1.0]
SCALE_BOUNDS = (0.6, 1.5)


@dataclass
class Days:
    """Every complete local day as arrays [days, 288, segments]."""
    speed: np.ndarray
    flow: np.ndarray
    rain: np.ndarray            # [days, 288]
    lanes_closed: np.ndarray
    dow: np.ndarray             # [days]
    dates: list


def to_days(df: pd.DataFrame, order: list[str] | None = None) -> Days:
    order = order or [s.id for s in SEGMENTS]
    grids = {s: regular(df, s) for s in order if (df.segment == s).any()}
    first = max(g.index.min() for g in grids.values()).tz_convert(LOCAL_TZ).ceil("D")
    last = min(g.index.max() for g in grids.values()).tz_convert(LOCAL_TZ).floor("D")
    days = pd.date_range(first, last, freq="D", inclusive="left", tz=LOCAL_TZ)
    S = len(order)
    sp = np.full((len(days), PER_DAY, S), np.nan)
    fl = np.full_like(sp, np.nan)
    lanes = np.zeros_like(sp)
    rain = np.zeros((len(days), PER_DAY))
    for si, sid in enumerate(order):
        g = grids[sid]
        for di, d in enumerate(days):
            idx = pd.date_range(d, periods=PER_DAY, freq=f"{STEP_MIN}min").tz_convert("UTC")
            part = g.reindex(idx)
            sp[di, :, si], fl[di, :, si] = part.speed_kmh.to_numpy(), part.flow_vph.to_numpy()
            lanes[di, :, si] = part.incident_lanes_closed.fillna(0).to_numpy()
            if si == 0:
                rain[di] = part.rain_mm_h.fillna(0).to_numpy()
    return Days(sp, fl, rain, lanes, np.array([d.dayofweek for d in days]), list(days))


def demand_profile(days: Days, free_flow: np.ndarray) -> DemandProfile:
    """Typical arrivals per quarter hour from readings taken when traffic was flowing and nothing was wrong."""
    S = days.speed.shape[2]
    ratio = days.speed / free_flow
    clean = (days.lanes_closed == 0) & (ratio > 0.85) & (days.rain[:, :, None] < 1.0)
    table = np.zeros((7, 96, S))
    overall = np.nanmedian(np.where(clean, days.flow, np.nan), axis=(0, 1))
    for d in range(7):
        sel = days.dow == d
        if not sel.any():
            table[d] = table[(d - 1) % 7] if d else overall
            continue
        f = np.where(clean[sel], days.flow[sel], np.nan).reshape(-1, PER_DAY // 96 * 96 // 96 * 96 // 96, 3, S) \
            if False else np.where(clean[sel], days.flow[sel], np.nan)
        q = f.reshape(f.shape[0], 96, 3, S)
        table[d] = np.nanmean(q, axis=(0, 2))
    table = np.where(np.isnan(table), overall, table)
    k = np.ones(5) / 5                                              # a little smoothing along the day
    for d in range(7):
        for s in range(S):
            pad = np.pad(table[d, :, s], 2, mode="wrap")
            table[d, :, s] = np.convolve(pad, k, mode="valid")
    return DemandProfile(table)


def typical_speed(days: Days) -> np.ndarray:
    S = days.speed.shape[2]
    out = np.zeros((7, 96, S))
    mean_all = np.nanmean(days.speed, axis=(0, 1))
    for d in range(7):
        sel = days.dow == d
        if sel.any():
            out[d] = np.nanmean(days.speed[sel].reshape(-1, 96, 3, S), axis=(0, 2))
        else:
            out[d] = mean_all
    return np.where(np.isnan(out), mean_all, out)


def _arrays(days: Days, profile: DemandProfile):
    demand = profile.table[days.dow]                                   # [days, 96, S]
    return np.repeat(demand, 3, axis=1)                                 # to 5 minute steps


def _unpack(theta: np.ndarray, S: int) -> Params:
    kw = dict(zip(NAMES, map(float, theta[: len(NAMES)])))
    return Params(seg_scale=[float(v) for v in theta[len(NAMES): len(NAMES) + S]], **kw)


def refine_demand(p: Params, profile: DemandProfile, days: Days, free_flow: np.ndarray, lanes: np.ndarray,
                  rounds: int = 12, eta: float = 1.6) -> DemandProfile:
    """Nudge typical arrivals, quarter hour by quarter hour, until a simulated typical day (with the real incidents and
    rain of each day) lines up with the speeds actually seen. Standard demand calibration: the flow readings alone
    understate demand wherever the road was already congested."""
    table = profile.table.copy()
    S = table.shape[2]
    obs = days.speed
    mask = ~np.isnan(obs)
    init = np.ones((len(days.dow), S))
    for _ in range(rounds):
        sim, _f, _q = simulate(p, np.repeat(table[days.dow], 3, axis=1), days.rain, days.lanes_closed, lanes, free_flow, init)
        diff = np.where(mask, sim - obs, 0.0).reshape(len(days.dow), 96, 3, S)
        cnt = mask.reshape(len(days.dow), 96, 3, S)
        for d in range(7):
            sel = days.dow == d
            if not sel.any():
                continue
            n = cnt[sel].sum(axis=(0, 2))
            err = diff[sel].sum(axis=(0, 2)) / np.maximum(n, 1)                 # positive: simulated road is too fast
            table[d] *= np.clip(np.exp(eta * err / free_flow), 0.85, 1.18)       # more arrivals to slow it down
        table = np.clip(table, profile.table * 0.4, profile.table * 3.0)
    return DemandProfile(table)


def fit(df: pd.DataFrame, max_nfev: int = 60, seed: int = 0, refine: bool = True) -> tuple[Twin, dict]:
    segs = SEGMENTS
    free_flow = np.array([s.free_flow_kmh for s in segs])
    lanes = np.array([s.lanes for s in segs], dtype=float)
    days = to_days(df)
    profile = demand_profile(days, free_flow)
    demand = _arrays(days, profile)
    obs = days.speed
    mask = ~np.isnan(obs)
    S = len(segs)
    d0 = Params()
    theta0 = np.array([getattr(d0, n) for n in NAMES] + [1.0] * S)
    low = np.array(LOW + [SCALE_BOUNDS[0]] * S)
    high = np.array(HIGH + [SCALE_BOUNDS[1]] * S)
    init = np.ones((len(days.dow), S))
    rng = np.random.default_rng(seed)
    sample = rng.random(mask.shape) < 0.5                                # half the points are plenty and twice as fast

    def residual(theta):
        p = _unpack(theta, S)
        sp, _fl, _q = simulate(p, demand, days.rain, days.lanes_closed, lanes, free_flow, init)
        return (sp - np.where(mask, obs, sp))[mask & sample]

    sol = least_squares(residual, np.clip(theta0, low + 1e-9, high - 1e-9), bounds=(low, high), loss="soft_l1",
                        f_scale=4.0, max_nfev=max_nfev, x_scale="jac")
    params = _unpack(sol.x, S)
    if refine:
        profile = refine_demand(params, profile, days, free_flow, lanes)
    twin = Twin(params, profile, typical_speed(days))
    info = {"days": len(days.dow), "iterations": int(sol.nfev), "cost": float(sol.cost),
            "params": params.to_json(), "trained_through": str(days.dates[-1])}
    return twin, info


# ------------------------------------------------------------------ checking it on days it never saw


def validate(twin: Twin, df: pd.DataFrame, starts=(7, 12, 16, 17.5), lead_h=(1, 2, 4)) -> dict:
    """Start from the road as it was, replay the real incidents and rain, and compare with what happened.
    Against the two obvious guesses: the road stays as it is, and the typical day."""
    S = len(twin.ids)
    days = to_days(df)
    demand = _arrays(days, twin.demand)
    typ = np.repeat(twin.typical_speed[days.dow], 3, axis=1)                   # [days, 288, S]
    ff = twin.free_flow
    out: dict = {"days": len(days.dow), "from_start_state": {}, "whole_day_from_midnight": {}}
    for h in lead_h:
        out["from_start_state"][str(h)] = {"mae_twin": [], "mae_keep_current": [], "mae_typical": [], "mae_twin_incident": [], "mae_typical_incident": []}
    for start_h in starts:
        i0 = int(start_h * 60 // STEP_MIN)
        steps = int(max(lead_h) * 60 // STEP_MIN)
        if i0 + steps >= PER_DAY:
            continue
        sl = slice(i0, i0 + steps)
        speed0 = np.nan_to_num(days.speed[:, i0, :], nan=0.0)
        ok = (days.speed[:, i0, :] > 0).all(axis=1)
        init = np.clip(speed0 / ff, 0.03, 1.0)
        q0 = initial_queue(twin.p, init, demand[:, i0, :] * np.asarray(twin.p.seg_scale), twin.lanes)
        sp, _f, _q = simulate(twin.p, demand[:, sl], days.rain[:, sl], days.lanes_closed[:, sl], twin.lanes, ff, init, None, q0)
        obs = days.speed[:, sl]
        for h in lead_h:
            k = int(h * 60 // STEP_MIN) - 1
            o = obs[ok, k, :]
            valid = ~np.isnan(o)
            inc = (days.lanes_closed[ok, i0 + k, :] > 0) & valid
            e = lambda pred: np.abs(pred[ok, k, :] - np.where(valid, o, pred[ok, k, :]))[valid]
            cur = np.repeat(days.speed[:, i0:i0 + 1, :], steps, axis=1)
            r = out["from_start_state"][str(h)]
            r["mae_twin"].append(e(sp)), r["mae_keep_current"].append(e(cur)), r["mae_typical"].append(e(typ[:, sl]))
            if inc.any():
                r["mae_twin_incident"].append(np.abs(sp[ok, k, :] - np.where(inc, o, sp[ok, k, :]))[inc])
                r["mae_typical_incident"].append(np.abs(typ[:, sl][ok, k, :] - np.where(inc, o, typ[:, sl][ok, k, :]))[inc])
    for h, r in out["from_start_state"].items():
        for key in list(r):
            r[key] = round(float(np.concatenate(r[key]).mean()), 2) if r[key] else None
    init = np.ones((len(days.dow), S))
    sp, _f, _q = simulate(twin.p, demand, days.rain, days.lanes_closed, twin.lanes, ff, init)
    valid = ~np.isnan(days.speed)
    inc = (days.lanes_closed > 0) & valid
    out["whole_day_from_midnight"] = {
        "mae_twin": round(float(np.abs(sp - np.where(valid, days.speed, sp))[valid].mean()), 2),
        "mae_typical": round(float(np.abs(typ - np.where(valid, days.speed, typ))[valid].mean()), 2),
        "mae_twin_during_incidents": round(float(np.abs(sp - np.where(inc, days.speed, sp))[inc].mean()), 2) if inc.any() else None,
        "mae_typical_during_incidents": round(float(np.abs(typ - np.where(inc, days.speed, typ))[inc].mean()), 2) if inc.any() else None,
    }
    return out


def save(twin: Twin, info: dict, report: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps({"params": twin.p.to_json(), "demand": twin.demand.to_json(),
                                      "typical_speed": twin.typical_speed.round(1).tolist(), "fit": info,
                                      "validation": report}))


def load(path: str | Path) -> tuple[Twin, dict]:
    d = json.loads(Path(path).read_text())
    return Twin(Params.from_json(d["params"]), DemandProfile.from_json(d["demand"]), np.asarray(d["typical_speed"])), \
        {"fit": d["fit"], "validation": d["validation"]}
