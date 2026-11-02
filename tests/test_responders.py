import random

import httpx

from atms_contracts.models import ResponderStatus
from atms_sim.responders import plan_steps, run_responder


def test_plan_goes_en_route_then_on_scene_then_done():
    steps = plan_steps(300, random.Random(1))
    assert [s.status for s in steps] == [ResponderStatus.en_route, ResponderStatus.on_scene, ResponderStatus.done]
    assert steps[0].delay_s == 0 and steps[1].delay_s == 300 and steps[0].eta_s == 300


def test_speedup_shrinks_the_waits():
    steps = plan_steps(300, random.Random(1), speedup=60)
    assert steps[1].delay_s == 5


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://atms")


def test_responder_patches_each_step_with_an_idempotency_key():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers["idempotency-key"], req.read()))
        return httpx.Response(200, json={})

    waits = []
    resps = run_responder(make_client(handler), "inc1", "as1", plan_steps(60, random.Random(1)), sleep=waits.append)
    assert len(resps) == 3
    assert [s[1] for s in seen] == ["/v1/incidents/inc1/assignments/as1"] * 3
    assert [s[2] for s in seen] == ["as1:en_route", "as1:on_scene", "as1:done"]
    assert b'"eta_s":60' in seen[0][3].replace(b" ", b"")
    assert b"eta_s" not in seen[2][3]
    assert waits[0] == 0 and waits[1] == 60


def test_responder_stops_after_a_409_and_does_not_keep_pushing():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(409, json={"error": "out of order", "current_state": "done"})

    resps = run_responder(make_client(handler), "inc1", "as1", plan_steps(60, random.Random(1)), sleep=lambda s: None)
    assert len(calls) == 1 and resps[0].status_code == 409
