"""Forecast and traffic reading endpoints, mounted on the main API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .features import HORIZONS
from .service import ForecastService, NoModel, StaleData, UnknownSegment
from .segments import BY_ID
from .twin import DemandSpec, IncidentSpec, Responses, Scenario, WeatherSpec
from .whatif import WhatIfError

MAX_BATCH = 5000


class Reading(BaseModel):
    segment: str
    ts: datetime
    speed_kmh: float = Field(ge=0, le=200)
    flow_vph: float = Field(ge=0, le=20000)


class WeatherReading(BaseModel):
    ts: datetime
    rain_mm_h: float = Field(ge=0, le=300)
    visibility_km: float = Field(ge=0, le=50)
    kind: str = Field(default="observed", pattern="^(observed|forecast)$")


class ReadingBatch(BaseModel):
    items: list[Reading] = Field(max_length=MAX_BATCH)


class WeatherBatch(BaseModel):
    items: list[WeatherReading] = Field(max_length=MAX_BATCH)


class IncidentIn(BaseModel):
    segment: str
    at_min: float = Field(default=0, ge=0)
    duration_min: float = Field(gt=0, le=720)
    lanes_closed: int = Field(default=1, ge=0, le=3)


class DemandIn(BaseModel):
    at_min: float = Field(default=0, ge=0)
    duration_min: float = Field(gt=0, le=720)
    factor: float = Field(ge=0.1, le=3.0)
    segment: str | None = None


class WeatherIn(BaseModel):
    at_min: float = Field(default=0, ge=0)
    duration_min: float = Field(gt=0, le=720)
    rain_mm_h: float = Field(ge=0, le=300)


class ResponsesIn(BaseModel):
    clearance_speedup: float = Field(default=0, ge=0, le=0.8)
    shoulder_lane: bool = False
    diversion_share: float = Field(default=0, ge=0, le=0.5)
    diversion_delay_min: float = Field(default=10, ge=0, le=120)


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    incidents: list[IncidentIn] = []
    demand: list[DemandIn] = []
    weather: list[WeatherIn] = []
    responses: ResponsesIn = ResponsesIn()

    def to_scenario(self) -> Scenario:
        return Scenario(self.name, [IncidentSpec(**i.model_dump()) for i in self.incidents],
                        [DemandSpec(**d.model_dump()) for d in self.demand], [WeatherSpec(**w.model_dump()) for w in self.weather],
                        Responses(**self.responses.model_dump()))


class WhatIfIn(BaseModel):
    start: datetime | None = None
    horizon_min: int = Field(default=240, ge=30, le=720)
    scenarios: list[ScenarioIn] = Field(min_length=1, max_length=8)


def mount(app: FastAPI, svc: ForecastService, principal, operator, sensor, incident_for=None) -> None:
    def aware(ts: datetime) -> datetime:
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    @app.post("/v1/traffic/readings")
    async def readings(body: ReadingBatch, _p=Depends(sensor)):
        now = svc.clock()
        good, bad = [], []
        for r in body.items:
            ts = aware(r.ts)
            if r.segment not in BY_ID:
                bad.append({"segment": r.segment, "ts": r.ts.isoformat(), "reason": "unknown segment"})
            elif ts > now + timedelta(minutes=5) or ts < now - timedelta(days=7):
                bad.append({"segment": r.segment, "ts": r.ts.isoformat(), "reason": "timestamp outside the accepted window"})
            else:
                good.append({"segment": r.segment, "ts": ts, "speed_kmh": r.speed_kmh, "flow_vph": r.flow_vph})
        svc.store.add_observations(good)
        return {"accepted": len(good), "rejected": bad}

    @app.post("/v1/traffic/weather")
    async def weather(body: WeatherBatch, _p=Depends(sensor)):
        svc.store.add_weather([{"ts": aware(w.ts), "rain_mm_h": w.rain_mm_h, "visibility_km": w.visibility_km, "kind": w.kind}
                               for w in body.items])
        return {"accepted": len(body.items)}

    @app.post("/v1/whatif")
    async def whatif(body: WhatIfIn, _p=Depends(principal)):
        """Run made up situations on the traffic twin and compare them with a normal day."""
        if svc.whatif is None:
            raise HTTPException(503, "no what-if twin is loaded, train one with atms_forecast.train")
        start = aware(body.start) if body.start else None
        try:
            return svc.whatif.compare([s.to_scenario() for s in body.scenarios], body.horizon_min, start)
        except WhatIfError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/v1/incidents/{incident_id}/whatif")
    async def incident_whatif(incident_id: UUID, _p=Depends(operator)):
        """For an incident that is happening: what each way of handling it would cost, and which is best."""
        if svc.whatif is None:
            raise HTTPException(503, "no what-if twin is loaded, train one with atms_forecast.train")
        info = incident_for(incident_id) if incident_for else None
        if info is None:
            raise HTTPException(404, "no such incident")
        if info["segment"] is None:
            raise HTTPException(422, "this incident is not on a segment the twin covers")
        return svc.whatif.incident_options(info["segment"], info["lanes_closed"], info["severity"], info["elapsed_min"])

    @app.get("/v1/forecast/model")
    async def model_info(_p=Depends(principal)):
        if svc.model is None:
            raise HTTPException(503, "no model is loaded")
        return {"version": svc.version, "horizons_min": sorted(svc.model.boosters), "trained": svc.model.meta,
                "backtest": svc.backtest, "segments": list(svc.model.free_flow)}

    @app.get("/v1/forecast/accuracy")
    async def accuracy(hours: int = Query(default=24, ge=1, le=24 * 14), _p=Depends(principal)):
        return svc.accuracy(hours)

    @app.get("/v1/forecast/{segment}")
    async def one(segment: str, horizons: str = Query(default=",".join(map(str, HORIZONS))),
                  lanes_closed: int | None = Query(default=None, ge=0, le=3),
                  rain_mm_h: float | None = Query(default=None, ge=0, le=300),
                  _p=Depends(principal)):
        """With lanes_closed or rain_mm_h the forecast answers a what-if: the same model, one input changed."""
        try:
            hs = [int(x) for x in horizons.split(",") if x]
        except ValueError:
            raise HTTPException(422, "horizons must be minutes separated by commas")
        overrides = {k: v for k, v in (("lanes_closed", lanes_closed), ("rain_mm_h", rain_mm_h)) if v is not None}
        try:
            return svc.forecast(segment, hs, overrides or None)
        except UnknownSegment:
            raise HTTPException(404, "unknown segment")
        except NoModel as exc:
            raise HTTPException(503, str(exc))
        except StaleData as exc:
            raise HTTPException(503, f"readings too old to forecast: {exc}")

    @app.get("/v1/forecast")
    async def overview(_p=Depends(principal)):
        out = []
        for seg in svc.segments:
            try:
                f = svc.forecast(seg.id, explain=False)
            except (StaleData, NoModel):
                out.append({"segment": seg.id, "status": "no recent data"})
                continue
            worst = max(f["forecasts"], key=lambda r: ("free", "moderate", "heavy", "severe").index(r["level"]))
            out.append({"segment": seg.id, "status": "ok", "current_speed_kmh": f["current_speed_kmh"],
                        "worst_level": worst["level"], "at_horizon_min": worst["horizon_min"],
                        "incident": f["incident"]})
        return {"items": out, "model_version": svc.version}
