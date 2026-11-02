import asyncio
import time
from pathlib import Path

import httpx
import jsonschema
import yaml

from atms_contracts.models import Agency, IncidentState as I
from atms_core.api import Principal, create_api, sign, verify_signature

from conftest import Rig, detection, run, video

OPS, POLICE, AMB = {"Authorization": "Bearer ops"}, {"Authorization": "Bearer police"}, {"Authorization": "Bearer amb"}
TOKENS = {"ops": Principal("op1"), "police": Principal("police-app", Agency.police), "amb": Principal("amb-app", Agency.ambulance)}
SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml").read_text())


def check(schema_name, body):
    """Validate a response against the OpenAPI component schema. OpenAPI 3.0 nullable becomes a JSON Schema null type."""
    def fix(node):
        if isinstance(node, dict):
            if node.pop("nullable", False) and "type" in node:
                node["type"] = [node["type"], "null"]
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
        return node
    components = fix(yaml.safe_load(yaml.safe_dump(SPEC["components"])))
    jsonschema.validate(body, {"$ref": f"#/components/schemas/{schema_name}", "components": components},
                        format_checker=jsonschema.FormatChecker())


async def api_rig(**kw):
    r = Rig(**kw)
    app = create_api(r.system, TOKENS, "secret", run_bus=False)
    r.system.bus.start()
    r.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms")
    return r


def test_auth_is_required_and_agencies_cannot_run_operator_commands():
    async def go():
        r = await api_rig()
        await r.feed(detection(confidence=0.7))
        iid = str(r.incident().id)
        assert (await r.http.get("/v1/incidents")).status_code == 401
        assert (await r.http.get("/v1/incidents", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await r.http.post(f"/v1/incidents/{iid}/verify", headers=POLICE)).status_code == 403
        assert (await r.http.get("/v1/incidents", headers=POLICE)).status_code == 200
    run(go())


def test_list_get_and_verify_match_the_openapi_schemas():
    async def go():
        r = await api_rig()
        await r.feed(detection(confidence=0.7), video())
        iid = str(r.incident().id)
        lst = (await r.http.get("/v1/incidents", headers=OPS)).json()
        check("Incident", lst["items"][0])
        assert (await r.http.get("/v1/incidents", params={"state": "dispatched"}, headers=OPS)).json()["items"] == []
        v = await r.http.post(f"/v1/incidents/{iid}/verify", headers=OPS)
        assert v.status_code == 200 and v.json()["state"] == "verified"
        check("Incident", v.json())
        again = await r.http.post(f"/v1/incidents/{iid}/verify", headers=OPS)
        assert again.status_code == 409 and again.json()["current_state"] != ""
        check("Conflict", again.json())
        await r.system.settle()
        d = (await r.http.get(f"/v1/incidents/{iid}", headers=OPS)).json()
        check("IncidentDetail", d)
        assert d["state"] == "dispatched" and len(d["assignments"]) == 4 and d["timeline"]
        assert (await r.http.get(f"/v1/incidents/{iid}", headers=OPS)).status_code == 200
        assert (await r.http.get("/v1/incidents/00000000-0000-0000-0000-000000000000", headers=OPS)).status_code == 404
    run(go())


def test_verify_with_an_idempotency_key_runs_once():
    async def go():
        r = await api_rig()
        await r.feed(detection(confidence=0.7))
        iid = str(r.incident().id)
        h = {**OPS, "Idempotency-Key": "k1"}
        a = await r.http.post(f"/v1/incidents/{iid}/verify", headers=h)
        b = await r.http.post(f"/v1/incidents/{iid}/verify", headers=h)
        assert a.status_code == b.status_code == 200 and a.json() == b.json()
        assert r.event_types().count("incident.verified") == 1
    run(go())


def test_responder_patch_full_cycle_over_http():
    async def go():
        r = await api_rig()
        await r.feed(detection(), video())
        inc = r.incident()
        iid = str(inc.id)
        police = r.assignments()[Agency.police]
        url = f"/v1/incidents/{iid}/assignments/{police.id}"
        bad = await r.http.patch(url, json={"status": "done"}, headers=POLICE)
        assert bad.status_code == 409 and bad.json()["current_state"] == "acked"
        assert (await r.http.patch(url, json={"status": "en_route", "eta_s": 90}, headers=AMB)).status_code == 403
        assert (await r.http.patch(url, json={"status": "bogus"}, headers=POLICE)).status_code == 422
        ok = await r.http.patch(url, json={"status": "en_route", "eta_s": 90, "note": "on the way"}, headers=POLICE)
        assert ok.status_code == 200 and ok.json()["state"] == "en_route" and ok.json()["eta_s"] == 90
        check("Assignment", ok.json())
        # drive everything else to done, then the road is confirmed clear by an assigned agency
        for a in r.store.for_incident(inc.id):
            token = {Agency.police: POLICE, Agency.ambulance: AMB}.get(a.agency, OPS)
            u = f"/v1/incidents/{iid}/assignments/{a.id}"
            for status in ("en_route", "on_scene", "done"):
                resp = await r.http.patch(u, json={"status": status}, headers=token)
                assert resp.status_code == 200, (a.agency, status, resp.text)
        assert inc.state == I.clearing
        assert (await r.http.post(f"/v1/incidents/{iid}/resolve", headers=AMB)).status_code == 200
        await r.system.settle()
        assert inc.state == I.closed and not r.vms.showing
        ev = (await r.http.get(f"/v1/incidents/{iid}/evidence", headers=OPS)).json()
        assert {i["kind"] for i in ev["items"]} == {"clip", "report"}
        assert any("a note" in t["summary"] or "on the way" in t["summary"] for t in
                   (await r.http.get(f"/v1/incidents/{iid}", headers=OPS)).json()["timeline"])
    run(go())


def test_an_unassigned_agency_cannot_resolve():
    async def go():
        r = await api_rig()
        await r.feed(detection(severity=__import__("atms_contracts.models", fromlist=["Severity"]).Severity.minor, confidence=0.97))
        # minor: police and tow only, so the ambulance app has no assignment here
        assert (await r.http.post(f"/v1/incidents/{r.incident().id}/resolve", headers=AMB)).status_code == 403
    run(go())


def test_reject_over_http_needs_a_reason_and_cancels_the_dispatch():
    async def go():
        r = await api_rig()
        await r.feed(detection())
        iid = str(r.incident().id)
        assert (await r.http.post(f"/v1/incidents/{iid}/reject", json={"reason": "x"}, headers=OPS)).status_code == 422
        resp = await r.http.post(f"/v1/incidents/{iid}/reject", json={"reason": "debris on road"}, headers=OPS)
        assert resp.status_code == 200 and resp.json()["state"] == "rejected"
        await r.system.settle()
        assert all(a.state.value == "cancelled" for a in r.store.for_incident(r.incident().id))
        assert (await r.http.post(f"/v1/incidents/{iid}/close", headers=OPS)).json()["state"] == "closed"
    run(go())


def test_notes_are_added_to_the_timeline():
    async def go():
        r = await api_rig()
        await r.feed(detection(confidence=0.7))
        iid = str(r.incident().id)
        assert (await r.http.post(f"/v1/incidents/{iid}/notes", json={"text": "camera 2 shows smoke"}, headers=OPS)).status_code == 201
        assert (await r.http.post(f"/v1/incidents/{iid}/notes", json={"text": ""}, headers=OPS)).status_code == 422
        d = (await r.http.get(f"/v1/incidents/{iid}", headers=OPS)).json()
        assert any(t["kind"] == "note" and t["actor"] == "op1" for t in d["timeline"])
    run(go())


def test_signed_evidence_urls_expire_and_cannot_be_forged():
    exp = int(time.time()) + 300
    sig = sign("/media/clips/a.mp4", exp, "secret")
    assert verify_signature("/media/clips/a.mp4", exp, sig, "secret")
    assert not verify_signature("/media/clips/b.mp4", exp, sig, "secret")
    assert not verify_signature("/media/clips/a.mp4", exp, sig, "other")
    assert not verify_signature("/media/clips/a.mp4", exp, sig, "secret", now=exp + 1)


def test_websocket_pushes_versioned_deltas_to_operators_only():
    from starlette.testclient import TestClient

    # the sync TestClient owns its own event loop, so build the whole rig inside it via the lifespan
    holder = {}

    def make():
        rig = Rig()
        holder["rig"] = rig
        return create_api(rig.system, TOKENS, "secret", run_bus=True)

    app = make()
    rig = holder["rig"]
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/v1/stream?token=police"):
                raise AssertionError("agency token must not be accepted")
        except Exception as exc:
            assert "4401" in repr(exc) or "disconnect" in repr(exc).lower()
        with client.websocket_connect("/v1/stream?token=ops") as ws:
            rig.system.ingest(detection(confidence=0.7))
            first = ws.receive_json()
            assert first["event_type"] == "incident.created" and first["incident"]["state"] == "detected"
            check("StreamMessage", first)
            v1 = first["version"]
            client.post(f"/v1/incidents/{first['incident_id']}/verify", headers=OPS)
            seen = [ws.receive_json() for _ in range(3)]
            assert all(m["version"] >= v1 for m in seen)
            versions = [m["version"] for m in seen]
            assert versions == sorted(versions)
