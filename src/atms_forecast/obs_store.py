"""Where traffic and weather readings and the forecast log live. Memory for tests, Postgres for real."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd


class ObservationStore(Protocol):
    def add_observations(self, rows: list[dict]) -> int: ...
    def add_weather(self, rows: list[dict]) -> int: ...
    def recent(self, segment: str, since: datetime, until: datetime | None = None) -> pd.DataFrame: ...
    def speed_at(self, segment: str, ts: datetime, tolerance_min: int = 3) -> float | None: ...
    def weather_now(self, now: datetime) -> tuple[float, float] | None: ...
    def weather_forecast(self, ts: datetime, tolerance_min: int = 90) -> float | None: ...
    def log_forecasts(self, rows: list[dict]) -> None: ...
    def open_forecasts(self, target_before: datetime, limit: int = 2000) -> list[dict]: ...
    def resolve_forecast(self, forecast_id, actual: float, at: datetime) -> None: ...
    def resolved_since(self, since: datetime) -> pd.DataFrame: ...
    def latest_ts(self, segment: str) -> datetime | None: ...


class MemoryObservationStore:
    def __init__(self) -> None:
        self.obs: dict[tuple[str, datetime], tuple[float, float]] = {}
        self.weather: dict[tuple[str, datetime], tuple[float, float]] = {}
        self.forecasts: dict[int, dict] = {}
        self._next = 1

    def add_observations(self, rows):
        for r in rows:
            self.obs[(r["segment"], r["ts"])] = (r["speed_kmh"], r["flow_vph"])
        return len(rows)

    def add_weather(self, rows):
        for r in rows:
            self.weather[(r.get("kind", "observed"), r["ts"])] = (r["rain_mm_h"], r["visibility_km"])
        return len(rows)

    def _observed_weather(self) -> pd.DataFrame:
        rows = [(ts, *v) for (k, ts), v in self.weather.items() if k == "observed"]
        return pd.DataFrame(rows, columns=["ts", "rain_mm_h", "visibility_km"]).sort_values("ts") if rows else \
            pd.DataFrame(columns=["ts", "rain_mm_h", "visibility_km"])

    def recent(self, segment, since, until=None):
        rows = [(ts, *v) for (s, ts), v in self.obs.items() if s == segment and ts >= since and (until is None or ts <= until)]
        df = pd.DataFrame(rows, columns=["ts", "speed_kmh", "flow_vph"]).sort_values("ts")
        if df.empty:
            return df.assign(segment=segment, rain_mm_h=0.0, visibility_km=10.0)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        w = self._observed_weather()
        if len(w):
            w["ts"] = pd.to_datetime(w["ts"], utc=True)
            df = pd.merge_asof(df, w, on="ts", direction="backward", tolerance=pd.Timedelta(hours=1))
        else:
            df["rain_mm_h"], df["visibility_km"] = 0.0, 10.0
        df[["rain_mm_h"]] = df[["rain_mm_h"]].fillna(0.0)
        df[["visibility_km"]] = df[["visibility_km"]].fillna(10.0)
        df["segment"] = segment
        return df.reset_index(drop=True)

    def speed_at(self, segment, ts, tolerance_min=3):
        best = None
        for (s, t), (speed, _f) in self.obs.items():
            if s == segment and abs((t - ts).total_seconds()) <= tolerance_min * 60:
                d = abs((t - ts).total_seconds())
                if best is None or d < best[0]:
                    best = (d, speed)
        return best[1] if best else None

    def weather_now(self, now):
        rows = [(ts, v) for (k, ts), v in self.weather.items() if k == "observed" and ts <= now]
        return max(rows)[1] if rows else None

    def weather_forecast(self, ts, tolerance_min=90):
        rows = [(abs((t - ts).total_seconds()), v[0]) for (k, t), v in self.weather.items()
                if k == "forecast" and abs((t - ts).total_seconds()) <= tolerance_min * 60]
        return min(rows)[1] if rows else None

    def log_forecasts(self, rows):
        for r in rows:
            if not any(f["segment"] == r["segment"] and f["made_at"] == r["made_at"] and f["horizon_min"] == r["horizon_min"]
                       for f in self.forecasts.values()):
                self.forecasts[self._next] = {**r, "id": self._next, "actual": None, "abs_error": None, "resolved_at": None}
                self._next += 1

    def open_forecasts(self, target_before, limit=2000):
        return [f for f in self.forecasts.values() if f["actual"] is None and f["target_ts"] <= target_before][:limit]

    def resolve_forecast(self, forecast_id, actual, at):
        f = self.forecasts[forecast_id]
        f.update(actual=actual, abs_error=abs(actual - f["predicted"]), resolved_at=at)

    def resolved_since(self, since):
        rows = [f for f in self.forecasts.values() if f["resolved_at"] is not None and f["resolved_at"] >= since]
        return pd.DataFrame(rows)

    def latest_ts(self, segment):
        ts = [t for (s, t) in self.obs if s == segment]
        return max(ts) if ts else None


class PostgresObservationStore:
    def __init__(self, repo):
        self.repo = repo                       # a PostgresRepo, used for its connection handling

    def _run(self, sql, params=(), many=False):
        def go(c):
            with c.transaction():
                return c.execute(sql, params)
        return self.repo._retry(go)

    def add_observations(self, rows):
        def go(c):
            with c.transaction():
                for r in rows:
                    c.execute("insert into traffic_observation(segment, ts, speed_kmh, flow_vph) values (%s,%s,%s,%s) "
                              "on conflict (segment, ts) do update set speed_kmh=excluded.speed_kmh, flow_vph=excluded.flow_vph",
                              (r["segment"], r["ts"], r["speed_kmh"], r["flow_vph"]))
        self.repo._retry(go)
        return len(rows)

    def add_weather(self, rows):
        def go(c):
            with c.transaction():
                for r in rows:
                    c.execute("insert into weather_observation(kind, ts, rain_mm_h, visibility_km) values (%s,%s,%s,%s) "
                              "on conflict (kind, ts) do update set rain_mm_h=excluded.rain_mm_h, visibility_km=excluded.visibility_km",
                              (r.get("kind", "observed"), r["ts"], r["rain_mm_h"], r["visibility_km"]))
        self.repo._retry(go)
        return len(rows)

    def recent(self, segment, since, until=None):
        rows = self.repo._retry(lambda c: c.execute(
            "select o.ts, o.speed_kmh, o.flow_vph, coalesce(w.rain_mm_h, 0) as rain_mm_h, coalesce(w.visibility_km, 10) as visibility_km "
            "from traffic_observation o left join lateral (select rain_mm_h, visibility_km from weather_observation "
            " where kind='observed' and ts <= o.ts and ts > o.ts - interval '1 hour' order by ts desc limit 1) w on true "
            "where o.segment=%s and o.ts >= %s and (%s::timestamptz is null or o.ts <= %s) order by o.ts",
            (segment, since, until, until)).fetchall())
        df = pd.DataFrame(rows, columns=["ts", "speed_kmh", "flow_vph", "rain_mm_h", "visibility_km"])
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df["segment"] = segment
        return df

    def speed_at(self, segment, ts, tolerance_min=3):
        row = self.repo._retry(lambda c: c.execute(
            "select speed_kmh from traffic_observation where segment=%s and ts between %s and %s "
            "order by abs(extract(epoch from (ts - %s))) limit 1",
            (segment, ts - timedelta(minutes=tolerance_min), ts + timedelta(minutes=tolerance_min), ts)).fetchone())
        return row["speed_kmh"] if row else None

    def weather_now(self, now):
        row = self.repo._retry(lambda c: c.execute(
            "select rain_mm_h, visibility_km from weather_observation where kind='observed' and ts <= %s order by ts desc limit 1",
            (now,)).fetchone())
        return (row["rain_mm_h"], row["visibility_km"]) if row else None

    def weather_forecast(self, ts, tolerance_min=90):
        row = self.repo._retry(lambda c: c.execute(
            "select rain_mm_h from weather_observation where kind='forecast' and ts between %s and %s "
            "order by abs(extract(epoch from (ts - %s))) limit 1",
            (ts - timedelta(minutes=tolerance_min), ts + timedelta(minutes=tolerance_min), ts)).fetchone())
        return row["rain_mm_h"] if row else None

    def log_forecasts(self, rows):
        def go(c):
            with c.transaction():
                for r in rows:
                    c.execute("insert into forecast_log(segment, made_at, horizon_min, target_ts, predicted, low, high, level, "
                              "speed_now, model_version) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                              (r["segment"], r["made_at"], r["horizon_min"], r["target_ts"], r["predicted"], r["low"], r["high"],
                               r["level"], r["speed_now"], r["model_version"]))
        self.repo._retry(go)

    def open_forecasts(self, target_before, limit=2000):
        return self.repo._retry(lambda c: c.execute(
            "select * from forecast_log where actual is null and target_ts <= %s order by target_ts limit %s",
            (target_before, limit)).fetchall())

    def resolve_forecast(self, forecast_id, actual, at):
        self._run("update forecast_log set actual=%s, abs_error=abs(%s - predicted), resolved_at=%s where id=%s",
                  (actual, actual, at, forecast_id))

    def resolved_since(self, since):
        rows = self.repo._retry(lambda c: c.execute(
            "select * from forecast_log where resolved_at is not null and resolved_at >= %s", (since,)).fetchall())
        return pd.DataFrame(rows)

    def latest_ts(self, segment):
        row = self.repo._retry(lambda c: c.execute("select max(ts) as t from traffic_observation where segment=%s", (segment,)).fetchone())
        return row["t"]
