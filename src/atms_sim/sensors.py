"""Feeds road readings to the API, as sensors would. Replays synthetic traffic shifted so it ends at a chosen moment."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from atms_forecast.synth import generate


def window_ending_at(df: pd.DataFrame, now: datetime, minutes: int, source_end: pd.Timestamp) -> pd.DataFrame:
    """Rows from the last `minutes` before source_end, shifted in time so source_end becomes now."""
    w = df[(df.ts > source_end - pd.Timedelta(minutes=minutes)) & (df.ts <= source_end)].copy()
    w["ts"] = w.ts + (pd.Timestamp(now) - source_end)
    return w


def to_payload(w: pd.DataFrame) -> dict:
    return {"items": [{"segment": r.segment, "ts": r.ts.isoformat(), "speed_kmh": round(float(r.speed_kmh), 1),
                       "flow_vph": round(float(r.flow_vph), 0)} for r in w.itertuples()]}


def weather_payload(w: pd.DataFrame) -> dict:
    one = w.drop_duplicates("ts")
    return {"items": [{"ts": r.ts.isoformat(), "rain_mm_h": round(float(r.rain_mm_h), 2),
                       "visibility_km": round(float(r.visibility_km), 2), "kind": "observed"} for r in one.itertuples()]}


def push(api: str, token: str, df: pd.DataFrame, source_end: pd.Timestamp, minutes: int = 180, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    w = window_ending_at(df, now, minutes, source_end)
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=api, headers=h, timeout=30) as c:
        r1 = c.post("/v1/traffic/weather", json=weather_payload(w))
        r2 = c.post("/v1/traffic/readings", json=to_payload(w))
    r1.raise_for_status(), r2.raise_for_status()
    return r2.json()
