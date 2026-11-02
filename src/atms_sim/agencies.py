"""Fake agency dispatch systems. The Integration Hub adapters talk to these until real ones exist.

POST /v1/agencies/{agency}/dispatch
  200 {accepted: true, unit_ref, eta_s}     unit assigned
  409 {accepted: false, reason}             agency declines
  no answer within the caller's timeout     agency is slow (the handler sleeps)

A repeated idempotency_key returns the first answer, so a retry never sends a second unit.
Force one outcome with the header X-Sim-Behavior: ack | decline | timeout.
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from atms_contracts.models import Agency, Location, Severity
from atms_contracts.signing import verify_webhook


@dataclass
class Profile:
    p_decline: float = 0.05
    p_timeout: float = 0.05
    ack_delay_s: tuple[float, float] = (0.2, 1.5)
    eta_s: tuple[int, int] = (180, 600)
    timeout_sleep_s: float = 90.0


DEFAULT_PROFILES: dict[Agency, Profile] = {
    Agency.police: Profile(eta_s=(120, 420)),
    Agency.ambulance: Profile(eta_s=(240, 720), p_timeout=0.08),
    Agency.highway_patrol: Profile(eta_s=(150, 480)),
    Agency.fire: Profile(eta_s=(300, 900)),
    Agency.tow: Profile(p_decline=0.1, eta_s=(600, 1200)),
}


class DispatchRequest(BaseModel):
    incident_id: str
    idempotency_key: str
    severity: Severity
    location: Location
    facility: str | None = None


class CancelRequest(BaseModel):
    incident_id: str
    idempotency_key: str


@dataclass
class _State:
    rng: random.Random
    profiles: dict[Agency, Profile]
    answers: dict[tuple[str, str], tuple[int, dict]] = field(default_factory=dict)
    unit_counter: int = 0
    notice_secrets: dict = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # agency, key, outcome
    notices: list[dict] = field(default_factory=list)                # verified webhook notices received


def create_app(seed: int | None = None, profiles: dict[Agency, Profile] | None = None) -> FastAPI:
    state = _State(random.Random(seed), {**DEFAULT_PROFILES, **(profiles or {})})
    app = FastAPI(title="ATMS agency simulator")
    app.state.sim = state

    @app.post("/v1/agencies/{agency}/dispatch")
    async def dispatch(agency: Agency, req: DispatchRequest,
                       x_sim_behavior: str | None = Header(default=None)):
        cached = state.answers.get((agency.value, req.idempotency_key))
        if cached:
            state.calls.append((agency.value, req.idempotency_key, "replay"))
            return JSONResponse(cached[1], status_code=cached[0])

        profile = state.profiles[agency]
        behavior = x_sim_behavior or _pick(state.rng, profile)
        if behavior not in ("ack", "decline", "timeout"):
            raise HTTPException(400, f"unknown X-Sim-Behavior {behavior!r}")

        if behavior == "timeout":
            state.calls.append((agency.value, req.idempotency_key, "timeout"))
            await asyncio.sleep(profile.timeout_sleep_s)
            raise HTTPException(504, "agency did not answer")

        await asyncio.sleep(state.rng.uniform(*profile.ack_delay_s))
        if behavior == "decline":
            status, body = 409, {"accepted": False, "reason": "no unit available"}
        else:
            state.unit_counter += 1
            status, body = 200, {
                "accepted": True,
                "unit_ref": f"{agency.value[:3].upper()}-{state.unit_counter:03d}",
                "eta_s": _eta(state.rng, profile, req),
            }
        state.answers[(agency.value, req.idempotency_key)] = (status, body)
        state.calls.append((agency.value, req.idempotency_key, behavior))
        return JSONResponse(body, status_code=status)

    @app.post("/v1/agencies/{agency}/notice")
    async def notice(agency: Agency, request: Request, x_atms_signature: str | None = Header(default=None),
                     x_sim_behavior: str | None = Header(default=None)):
        """Where an agency receives ATMS notices. Anything unsigned, tampered or replayed is refused."""
        raw = await request.body()
        if not verify_webhook(state.notice_secrets.get(agency, f"dev-secret-{agency.value}"), x_atms_signature, raw):
            raise HTTPException(401, "bad signature")
        if x_sim_behavior == "unavailable":
            raise HTTPException(503, "agency desk offline")
        if x_sim_behavior == "reject":
            raise HTTPException(400, "malformed")
        state.notices.append({"agency": agency.value, "station": request.query_params.get("station"), **json.loads(raw)})
        return {"received": True}

    @app.get("/v1/agencies/notices")
    async def received():
        """What the agency desks have been told so far. Only the simulator has this, a real agency keeps it to itself."""
        return {"items": state.notices[-200:]}

    @app.post("/v1/agencies/{agency}/cancel")
    async def cancel(agency: Agency, req: CancelRequest):
        state.calls.append((agency.value, req.idempotency_key, "cancel"))
        return {"cancelled": True}

    return app


def _eta(rng: random.Random, profile: Profile, req: DispatchRequest) -> int:
    """Time to scene. From the station's real distance when it is known, so a far station takes longer, else a random draw."""
    from atms_contracts import network
    from atms_core.geo import AVG_SPEED_MS, ROAD_FACTOR, haversine_m

    st = next((x for x in network.load().stations if x.id == req.facility), None)
    if st is None:
        return rng.randint(*profile.eta_s)
    drive = haversine_m(st.location, req.location) * ROAD_FACTOR / AVG_SPEED_MS
    return max(60, round(drive + rng.uniform(30, 120)))      # plus the time to get a crew moving


def _pick(rng: random.Random, p: Profile) -> str:
    roll = rng.random()
    if roll < p.p_timeout:
        return "timeout"
    if roll < p.p_timeout + p.p_decline:
        return "decline"
    return "ack"
