"""Forecast service. Live features, forecasts with reasons, a log of what it predicted, and honest accuracy."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from atms_contracts.models import CongestionForecast, CongestionForecastData

from .features import FEATURES, HORIZONS, build, level_rank
from .model import CongestionModel
from .obs_store import ObservationStore
from .segments import BY_ID, SEGMENTS, STEP_MIN, Segment

log = logging.getLogger("atms.forecast")
WINDOW = timedelta(hours=3)
MAX_AGE = timedelta(minutes=15)
LEVEL_TEXT = {"free": "free flowing", "moderate": "moderate", "heavy": "heavy", "severe": "severe"}


class NoModel(Exception):
    pass


class StaleData(Exception):
    pass


class UnknownSegment(KeyError):
    pass


class ForecastService:
    def __init__(self, store: ObservationStore, model: CongestionModel | None = None, model_version: str = "none",
                 segments: list[Segment] | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 backtest: dict | None = None):
        self.store, self.model, self.version = store, model, model_version
        self.segments = segments or SEGMENTS
        self.order = [s.id for s in self.segments]
        self.clock = clock
        self.backtest = backtest
        self.incidents: Callable[[], dict[str, tuple[int, float]]] = lambda: {}
        self.emit: Callable | None = None
        self._level_state: dict[str, int] = {}
        self.whatif = None                       # a WhatIf service when a calibrated twin is loaded

    def set_model(self, model: CongestionModel, version: str, backtest: dict | None = None) -> None:
        self.model, self.version, self.backtest = model, version, backtest

    # ------------------------------------------------------------ live features

    def _window(self, segment: str, now: datetime) -> pd.DataFrame:
        frames = []
        for sid in self.order:
            df = self.store.recent(sid, now - WINDOW, now)
            if not df.empty:
                frames.append(df)
        if not frames:
            raise StaleData("no readings at all")
        df = pd.concat(frames, ignore_index=True)
        state = self.incidents()
        df["incident_lanes_closed"] = df.segment.map(lambda s: state.get(s, (0, 0.0))[0])
        df["incident_age_min"] = df.segment.map(lambda s: state.get(s, (0, 0.0))[1])
        return df

    def _features(self, segment: str, now: datetime, horizon: int, overrides: dict | None) -> tuple[pd.DataFrame, dict]:
        if segment not in BY_ID:
            raise UnknownSegment(segment)
        df = self._window(segment, now)
        mine = df[df.segment == segment]
        if mine.empty or now - mine.ts.max() > MAX_AGE:
            raise StaleData(f"latest reading for {segment} is {'missing' if mine.empty else str(now - mine.ts.max())} old")
        target = now + timedelta(minutes=horizon)
        rain_now = float(mine.rain_mm_h.iloc[-1])
        rain_target = self.store.weather_forecast(target)
        if rain_target is None:
            rain_target = rain_now                         # no weather forecast, assume it stays as it is
        o = overrides or {}
        if "rain_mm_h" in o:
            rain_target = float(o["rain_mm_h"])
            df.loc[df.segment == segment, "rain_mm_h"] = float(o["rain_mm_h"])
            rain_now = float(o["rain_mm_h"])
        if "lanes_closed" in o:
            df.loc[df.segment == segment, "incident_lanes_closed"] = int(o["lanes_closed"])
            df.loc[df.segment == segment, "incident_age_min"] = float(o.get("incident_age_min", 10.0 if o["lanes_closed"] else 0.0))
        X = build(df, horizon, self.model.hist, self.model.free_flow, seed=None, order=self.order,
                  rain_target={segment: rain_target})
        X = X[(X.segment == segment) & (X.index <= pd.Timestamp(now))]
        X = X.dropna(subset=FEATURES[:6]).iloc[[-1]] if len(X.dropna(subset=FEATURES[:6])) else X.iloc[0:0]
        if X.empty:
            raise StaleData(f"not enough recent readings for {segment} to build a forecast")
        info = {"rain_now_mm_h": round(rain_now, 1), "rain_at_target_mm_h": round(float(rain_target), 1),
                "data_age_s": int((now - mine.ts.max()).total_seconds()), "reading_at": mine.ts.max().isoformat()}
        return X, info

    # ------------------------------------------------------------ forecasting

    def forecast(self, segment: str, horizons=HORIZONS, overrides: dict | None = None, now: datetime | None = None,
                 explain: bool = True) -> dict:
        if self.model is None:
            raise NoModel("no model is loaded")
        now = now or self.clock()
        seg = BY_ID.get(segment)
        if seg is None:
            raise UnknownSegment(segment)
        out, info, speed_now = [], {}, None
        for h in horizons:
            if h not in self.model.boosters:
                continue
            X, info = self._features(segment, now, h, overrides)
            pred = self.model.predict(X, h).iloc[0]
            speed_now = float(X.speed_now.iloc[0])
            row = {
                "horizon_min": h, "target_ts": (now + timedelta(minutes=h)).isoformat(),
                "speed_kmh": round(float(pred.speed), 1), "low_kmh": round(float(pred.low), 1),
                "high_kmh": round(float(pred.high), 1), "level": str(pred.level),
                "baseline": {"keep_current_speed": round(speed_now, 1), "typical_for_time": round(float(X.hist_target.iloc[0]), 1)},
            }
            if explain:
                row["why"] = self.model.explain(X, h)[0]
                row["average_forecast_kmh"] = round(self.model.base_value(h), 1)
            out.append(row)
        state = self.incidents().get(segment, (0, 0.0))
        return {"segment": segment, "road_segment": seg.road_segment, "model_version": self.version,
                "as_of": now.isoformat(), "free_flow_kmh": round(self.model.free_flow[segment], 1),
                "current_speed_kmh": round(speed_now, 1) if speed_now is not None else None,
                "incident": {"lanes_closed": state[0], "minutes_since_start": state[1]} if state[0] else None,
                "what_if": overrides or None, "inputs": info, "forecasts": out}

    # ------------------------------------------------------------ the periodic job

    def run_cycle(self, now: datetime | None = None) -> list[dict]:
        """Forecast every segment with fresh data, record the forecasts, and announce new heavy congestion."""
        logs, alerts = self.compute_cycle(now)
        self.apply_cycle(logs, alerts)
        return alerts

    def compute_cycle(self, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
        """The slow half, models and explanations. Safe to run off the event loop because it writes nothing shared."""
        now = now or self.clock()
        alerts, logs = [], []
        for seg in self.segments:
            try:
                f = self.forecast(seg.id, now=now, explain=True)
            except (StaleData, NoModel):
                continue
            for r in f["forecasts"]:
                logs.append({"segment": seg.id, "made_at": now, "horizon_min": r["horizon_min"],
                             "target_ts": datetime.fromisoformat(r["target_ts"]), "predicted": r["speed_kmh"], "low": r["low_kmh"],
                             "high": r["high_kmh"], "level": r["level"], "speed_now": f["current_speed_kmh"],
                             "model_version": self.version})
            worst = max(f["forecasts"], key=lambda r: (level_rank([r["level"]])[0], -r["horizon_min"]))
            rank = int(level_rank([worst["level"]])[0])
            before = self._level_state.get(seg.id, 0)
            self._level_state[seg.id] = rank
            if rank >= 2 > before:
                alerts.append({"segment": seg.id, "horizon_min": worst["horizon_min"], "level": worst["level"],
                               "speed_kmh": worst["speed_kmh"], "low_kmh": worst["low_kmh"], "high_kmh": worst["high_kmh"],
                               "free_flow_kmh": f["free_flow_kmh"], "factor": (worst.get("why") or [{}])[0].get("factor")})
        return logs, alerts

    def apply_cycle(self, logs: list[dict], alerts: list[dict]) -> None:
        """The quick half, which records forecasts and puts events on the bus. Runs where the services run."""
        if logs:
            self.store.log_forecasts(logs)
        for a in alerts:
            self._announce(a)

    def _announce(self, a: dict) -> None:
        if self.emit is None:
            return
        self.emit(CongestionForecast(producer="forecast-service", data=CongestionForecastData(
            segment_id=a["segment"], horizon_min=a["horizon_min"], level=a["level"], predicted_speed_kmh=a["speed_kmh"],
            low_kmh=a["low_kmh"], high_kmh=a["high_kmh"], free_flow_kmh=a["free_flow_kmh"],
            model_version=self.version, main_factor=a["factor"])))

    # ------------------------------------------------------------ accuracy and drift

    def score_pending(self, now: datetime | None = None) -> int:
        """Compare past forecasts with what actually happened."""
        now = now or self.clock()
        done = 0
        for f in self.store.open_forecasts(now - timedelta(minutes=STEP_MIN)):
            actual = self.store.speed_at(f["segment"], f["target_ts"])
            if actual is not None:
                self.store.resolve_forecast(f["id"], actual, now)
                done += 1
        return done

    def accuracy(self, hours: int = 24, now: datetime | None = None, min_samples: int = 30, drift_factor: float = 1.5) -> dict:
        now = now or self.clock()
        df = self.store.resolved_since(now - timedelta(hours=hours))
        out: dict = {"window_hours": hours, "model_version": self.version, "horizons": {}, "drift": False, "reasons": []}
        if df.empty:
            return out
        for h, g in df.groupby("horizon_min"):
            model_mae, naive_mae = float(g.abs_error.mean()), float((g.actual - g.speed_now).abs().mean())
            expected = None
            if self.backtest and str(h) in self.backtest["horizons"]:
                expected = self.backtest["horizons"][str(h)]["mae"]["model"]
            row = {"samples": int(len(g)), "mae_model": round(model_mae, 2), "mae_keep_current_speed": round(naive_mae, 2),
                   "mae_expected_from_backtest": round(expected, 2) if expected else None,
                   "range_coverage": round(float(((g.actual >= g.low) & (g.actual <= g.high)).mean()), 3)}
            out["horizons"][str(h)] = row
            if len(g) >= min_samples:
                if expected and model_mae > expected * drift_factor:
                    out["drift"] = True
                    out["reasons"].append(f"{h} min: error {model_mae:.1f} km/h is more than {drift_factor}x the backtest {expected:.1f}")
                if model_mae > naive_mae:
                    out["drift"] = True
                    out["reasons"].append(f"{h} min: no better than carrying the current speed forward")
        return out

    async def sweep(self, interval_s: float = 60.0) -> None:
        while True:
            try:
                logs, alerts = await asyncio.to_thread(self.compute_cycle)     # seconds of work must not stall the API
                self.apply_cycle(logs, alerts)
                await asyncio.to_thread(self.score_pending)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("forecast cycle failed, will retry")
            await asyncio.sleep(interval_s)
