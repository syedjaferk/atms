import asyncio
import time

import httpx
import pytest

from atms_contracts.models import Agency
from atms_sim.agencies import Profile, create_app

BODY = {"incident_id": "i1", "idempotency_key": "k1", "severity": "major",
        "location": {"lat": 18.45, "lon": 73.85}}
FAST = {a: Profile(ack_delay_s=(0, 0), timeout_sleep_s=0.3) for a in Agency}


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim")


def run(coro):
    return asyncio.run(coro)


def test_ack_returns_a_unit_and_eta():
    async def go():
        async with client(create_app(1, FAST)) as c:
            return await c.post("/v1/agencies/ambulance/dispatch", json=BODY, headers={"X-Sim-Behavior": "ack"})
    r = run(go())
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True and body["unit_ref"].startswith("AMB-") and body["eta_s"] > 0


def test_decline_is_a_409_with_a_reason():
    async def go():
        async with client(create_app(1, FAST)) as c:
            return await c.post("/v1/agencies/tow/dispatch", json=BODY, headers={"X-Sim-Behavior": "decline"})
    r = run(go())
    assert r.status_code == 409 and r.json() == {"accepted": False, "reason": "no unit available"}


def test_timeout_makes_a_caller_with_a_short_timeout_give_up():
    async def go():
        async with client(create_app(1, FAST)) as c:
            return await asyncio.wait_for(
                c.post("/v1/agencies/police/dispatch", json=BODY, headers={"X-Sim-Behavior": "timeout"}), 0.05)
    with pytest.raises(asyncio.TimeoutError):
        run(go())


def test_same_idempotency_key_never_sends_a_second_unit():
    app = create_app(1, FAST)

    async def go():
        async with client(app) as c:
            first = await c.post("/v1/agencies/ambulance/dispatch", json=BODY, headers={"X-Sim-Behavior": "ack"})
            again = await c.post("/v1/agencies/ambulance/dispatch", json=BODY)
            other = await c.post("/v1/agencies/ambulance/dispatch", json={**BODY, "idempotency_key": "k2"},
                                 headers={"X-Sim-Behavior": "ack"})
            return first.json(), again.json(), other.json()
    first, again, other = run(go())
    assert first == again
    assert other["unit_ref"] != first["unit_ref"]
    assert [o for _, _, o in app.state.sim.calls] == ["ack", "replay", "ack"]


def test_same_seed_gives_the_same_outcomes():
    def outcomes(seed):
        app = create_app(seed, {a: Profile(ack_delay_s=(0, 0), p_decline=0.4, p_timeout=0.0) for a in Agency})

        async def go():
            async with client(app) as c:
                return [(await c.post("/v1/agencies/police/dispatch", json={**BODY, "idempotency_key": f"k{i}"})).status_code
                        for i in range(12)]
        return run(go())
    assert outcomes(7) == outcomes(7)
    assert set(outcomes(7)) == {200, 409}


def test_unknown_behavior_header_is_a_400():
    async def go():
        async with client(create_app(1, FAST)) as c:
            return await c.post("/v1/agencies/fire/dispatch", json=BODY, headers={"X-Sim-Behavior": "explode"})
    assert run(go()).status_code == 400


def test_unknown_agency_is_a_422():
    async def go():
        async with client(create_app(1, FAST)) as c:
            return await c.post("/v1/agencies/army/dispatch", json=BODY)
    assert run(go()).status_code == 422
