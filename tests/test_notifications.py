import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from atms_contracts.models import Agency, Severity
from atms_contracts.signing import sign_receipt, sign_webhook, verify_webhook
from atms_core.api import Principal, create_api
from atms_core.notifications import SEED_CONTACTS, NotifyConfig, mask, render
from atms_core.notify_providers import HttpSmsProvider, ProviderError, RecordingSms, SmsResult, WebhookSender
from atms_core.store import Contact
from atms_sim.agencies import create_app as create_agencies
from atms_sim.sms import Profile, create_app as create_sms

from conftest import T0, Rig, detection, run

DUTY = Contact("duty", "Duty officer NH48", "sms", "+910000000001", "duty_officer",
               ["incident.verified", "incident.rejected", "incident.resolved", "incident.closed"],
               road_segment="MH/PUNE/NH48", min_severity=Severity.major)
SUP = Contact("sup", "Control room supervisor", "sms", "+910000000002", "supervisor", ["dispatch.escalation", "incident.closed"])
FAST = NotifyConfig(max_attempts=4, fallback_after=2, backoff_s=(0.0, 0.0, 0.0))


class Scripted:
    """An SMS provider that follows a script of outcomes and, like a real one, dedupes on the idempotency key."""

    def __init__(self, name, *script):
        self.name, self.script, self.calls, self.refs = name, list(script or ["ok"]), [], {}

    async def send(self, to, text, key):
        self.calls.append((to, text, key))
        if key in self.refs:
            return SmsResult(self.refs[key])
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if step == "transient":
            raise ProviderError(f"{self.name} busy")
        if step == "permanent":
            raise ProviderError(f"{self.name} refused", transient=False)
        self.refs[key] = f"{self.name}-{len(self.refs) + 1}"
        return SmsResult(self.refs[key])


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


def notices(r, channel="sms"):
    return sorted((n for n in r.store.notifications.values() if n.channel == channel), key=lambda n: n.created_at)


def test_a_verified_major_accident_notifies_the_right_people_and_nobody_else():
    async def go():
        r = Rig()
        await r.feed(detection())
        by = {n.contact_id: n for n in r.store.notifications.values()}
        assert set(by) == {"duty-nh48", "agency-police", "agency-ambulance", "agency-highway_patrol", "agency-tow"}
        assert "agency-fire" not in by and "supervisor" not in by           # fire is not part of a major, supervisors only escalate
        assert by["duty-nh48"].status == "sent" and by["agency-police"].status == "delivered"
    run(go())


def test_minor_severity_and_other_roads_do_not_wake_the_duty_officer():
    async def go():
        r = Rig(contacts=[DUTY, SUP])
        await r.feed(detection(severity=Severity.minor, confidence=0.7))
        r.service.verify(r.incident().id, operator_id="op")                      # minor is below the duty officer's threshold
        await r.system.settle()
        assert notices(r) == []
        r2 = Rig(contacts=[Contact("other", "NH4 duty", "sms", "+910000000009", "duty_officer", ["incident.verified"],
                                   road_segment="MH/PUNE/NH4")])
        await r2.feed(detection())
        assert notices(r2) == []
    run(go())


def test_messages_are_short_plain_ascii_and_carry_the_reference():
    async def go():
        r = Rig(contacts=[DUTY, SUP])
        await r.feed(detection())
        inc = r.incident()
        for kind in ("incident.verified", "incident.rejected", "incident.resolved", "incident.closed", "dispatch.escalation"):
            text = render(kind, inc, "sms", Agency.fire)
            assert len(text) <= 160 and text.isascii() and text.startswith("ATMS") and str(inc.id)[:8] in text
        body = json.loads(render("incident.verified", inc, "webhook", now=T0))
        assert body["incident"]["id"] == str(inc.id) and body["notice"] == "incident.verified" and body["sent_at"].startswith("2026")
    run(go())


def test_addresses_are_masked_everywhere_a_person_could_see_them():
    assert mask("+919876543210") == "+91*******210"
    assert mask("http://desk.example/v1/notice") == "desk.example/..."


def test_a_delivery_receipt_marks_delivered_and_a_repeat_changes_nothing():
    async def go():
        sms = [Scripted("primary"), Scripted("fallback")]
        r = Rig(sms=sms, contacts=[DUTY, SUP])
        await r.feed(detection())
        (n,) = notices(r)
        assert n.status == "sent" and n.provider == "primary" and n.attempts == 1
        r.system.notifications.record_receipt("primary", n.provider_ref, True)
        await r.system.settle()
        assert n.status == "delivered" and n.delivered_at
        before = len(r.system.bus.log)
        r.system.notifications.record_receipt("primary", n.provider_ref, True)       # the gateway repeats itself
        await r.system.settle()
        assert len(r.system.bus.log) == before and n.status == "delivered"
        types = r.event_types()
        assert types.index("notification.sent") < types.index("notification.delivered")
        sent = next(e for e in r.system.bus.log if e.type == "notification.sent")
        assert "+91" not in sent.model_dump_json() and sent.data.contact_id == "duty"      # no phone numbers on the bus
    run(go())


def test_two_transient_failures_on_the_primary_move_to_the_fallback_at_once():
    async def go():
        primary, fallback = Scripted("primary", "transient"), Scripted("fallback", "ok")
        r = Rig(sms=[primary, fallback], contacts=[DUTY], notify=FAST)
        await r.feed(detection())
        (n,) = notices(r)
        assert (len(primary.calls), len(fallback.calls)) == (2, 1)
        assert n.status == "sent" and n.provider == "fallback" and n.attempts == 3
        keys = {c[2] for c in primary.calls + fallback.calls}
        assert keys == {str(n.id)}                                  # one key throughout, so no provider can send twice
    run(go())


def test_backoff_waits_before_a_retry_on_the_same_provider():
    async def go():
        clock = Clock()
        primary = Scripted("primary", "transient", "ok")
        r = Rig(sms=[primary, Scripted("fallback")], contacts=[DUTY], clock=clock,
                notify=NotifyConfig(fallback_after=3, backoff_s=(5.0, 20.0)))
        await r.feed(detection())
        (n,) = notices(r)
        assert n.status == "pending" and n.attempts == 1 and len(primary.calls) == 1
        assert n.next_attempt_at == T0 + timedelta(seconds=5)
        await r.system.notifications.deliver_due()
        assert len(primary.calls) == 1                              # not yet
        clock.advance(5)
        await r.system.settle()
        assert n.status == "sent" and len(primary.calls) == 2
    run(go())


def test_a_provider_that_refuses_for_good_sends_the_notice_to_the_fallback_without_waiting():
    async def go():
        primary, fallback = Scripted("primary", "permanent"), Scripted("fallback")
        r = Rig(sms=[primary, fallback], contacts=[DUTY], notify=NotifyConfig(backoff_s=(60.0,)))
        await r.feed(detection())
        (n,) = notices(r)
        assert len(primary.calls) == 1 and len(fallback.calls) == 1 and n.status == "sent" and n.provider == "fallback"
    run(go())


def test_everything_down_fails_the_notice_raises_an_operator_task_and_a_retry_can_recover():
    async def go():
        primary, fallback = Scripted("primary", "transient"), Scripted("fallback", "transient")
        r = Rig(sms=[primary, fallback], contacts=[DUTY], notify=FAST)
        await r.feed(detection())
        (n,) = notices(r)
        assert n.status == "failed" and n.attempts == 4 and "busy" in n.last_error
        failed = [e for e in r.system.bus.log if e.type == "notification.failed"]
        assert len(failed) == 1 and failed[0].data.recipient == "Duty officer NH48" and failed[0].data.attempts == 4
        assert any(t.kind == "operator_task" and "Duty officer NH48" in t.summary for t in r.incident().timeline)

        primary.script, fallback.script = ["ok"], ["ok"]                      # the providers recover
        n2 = await r.system.notifications.retry(n.id)
        assert n2.status == "sent" and n2.attempts == 1
        await r.system.settle()
    run(go())


def test_undelivered_receipt_goes_to_the_fallback_then_fails_if_that_fails_too():
    async def go():
        primary, fallback = Scripted("primary"), Scripted("fallback")
        r = Rig(sms=[primary, fallback], contacts=[DUTY], notify=FAST)
        await r.feed(detection())
        (n,) = notices(r)
        r.system.notifications.record_receipt("primary", n.provider_ref, False, "handset unreachable")
        await r.system.settle()
        assert n.status == "sent" and n.provider == "fallback" and n.attempts == 3
        r.system.notifications.record_receipt("fallback", n.provider_ref, False, "handset unreachable")
        await r.system.settle()
        assert n.status == "failed" and n.last_error == "handset unreachable"
    run(go())


def test_a_text_that_is_never_confirmed_is_resent_once_through_the_fallback_then_given_up():
    async def go():
        clock = Clock()
        r = Rig(sms=[Scripted("primary"), Scripted("fallback")], contacts=[DUTY], clock=clock,
                notify=NotifyConfig(receipt_timeout_s=120, backoff_s=(0.0,)))
        await r.feed(detection())
        (n,) = notices(r)
        clock.advance(119)
        assert r.system.notifications.expire_unconfirmed() == 0
        clock.advance(2)
        assert r.system.notifications.expire_unconfirmed() == 1
        await r.system.settle()
        assert n.status == "sent" and n.provider == "fallback"
        clock.advance(121)
        r.system.notifications.expire_unconfirmed()
        await r.system.settle()
        assert n.status == "failed" and n.last_error == "no delivery receipt"
    run(go())


def test_a_redelivered_event_sends_nothing_again():
    async def go():
        sms = [Scripted("primary"), Scripted("fallback")]
        r = Rig(sms=sms, contacts=SEED_CONTACTS)
        await r.feed(detection())
        count = len(r.store.notifications)
        calls = len(sms[0].calls) + len(r.system.notifications.webhook.sent)
        for e in list(r.system.bus.log):
            r.system.bus.publish(e)
        await r.system.settle()
        for e in list(r.system.bus.log):                  # and past the bus's own dedupe, as a replayed consumer group would
            await r.system.notifications.on_event(e)
        assert len(r.store.notifications) == count
        assert len(sms[0].calls) + len(r.system.notifications.webhook.sent) == calls
    run(go())


def test_rejection_resolution_and_closure_each_notify_once():
    async def go():
        r = Rig(contacts=[DUTY, SUP])
        await r.feed(detection())
        r.service.reject(r.incident().id, "debris", "op")
        await r.system.settle()
        r.service.close(r.incident().id)
        await r.system.settle()
        kinds = [(n.contact_id, n.kind) for n in notices(r)]
        assert kinds == [("duty", "incident.verified"), ("duty", "incident.rejected"), ("duty", "incident.closed"), ("sup", "incident.closed")]
    run(go())


def test_an_agency_that_never_answers_escalates_to_the_supervisor_by_sms():
    async def go():
        from atms_core.orchestrator import DispatchConfig
        r = Rig(behaviors={"fire": ["timeout"]}, dispatch=DispatchConfig(max_attempts=3, timeouts_per_unit=2),
                contacts=[DUTY, SUP])
        await r.feed(detection(severity=Severity.critical, confidence=0.97))
        esc = [n for n in notices(r) if n.kind == "dispatch.escalation"]
        assert len(esc) == 1 and esc[0].contact_id == "sup" and "Call fire by phone" in esc[0].body
    run(go())


# ------------------------------------------------------------------ webhooks


def make_webhook(handler):
    return WebhookSender(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_webhooks_are_signed_and_the_agency_can_verify_them_but_a_tampered_body_fails():
    async def go():
        seen = []

        def handler(req):
            seen.append((req.read(), req.headers["x-atms-signature"]))
            return httpx.Response(200)
        r = Rig(webhook=make_webhook(handler), contacts=[c for c in SEED_CONTACTS if c.id == "agency-police"])
        await r.feed(detection())
        (body, header), = seen
        assert verify_webhook("dev-secret-police", header, body)
        assert not verify_webhook("dev-secret-police", header, body + b" ")                  # tampered
        assert not verify_webhook("dev-secret-fire", header, body)                            # wrong agency secret
        t = int(header.split(",")[0][2:])
        assert not verify_webhook("dev-secret-police", header, body, now=t + 301)             # replayed later
        assert json.loads(body)["notice"] == "incident.verified"
        (n,) = notices(r, "webhook")
        assert n.status == "delivered" and n.attempts == 1
    run(go())


def test_a_webhook_that_is_down_is_retried_then_fails_but_a_refusal_is_not_retried():
    async def go():
        calls = {"n": 0}

        def down(req):
            calls["n"] += 1
            return httpx.Response(503)
        r = Rig(webhook=make_webhook(down), contacts=[c for c in SEED_CONTACTS if c.id == "agency-police"], notify=FAST)
        await r.feed(detection())
        (n,) = notices(r, "webhook")
        assert n.status == "failed" and calls["n"] == 4 and "503" in n.last_error

        calls["n"] = 0
        r2 = Rig(webhook=make_webhook(lambda req: (calls.__setitem__("n", calls["n"] + 1), httpx.Response(400))[1]),
                 contacts=[c for c in SEED_CONTACTS if c.id == "agency-police"], notify=FAST)
        await r2.feed(detection())
        (n2,) = notices(r2, "webhook")
        assert n2.status == "failed" and calls["n"] == 1                                       # 400 will not get better
    run(go())


def test_a_webhook_contact_without_a_secret_fails_cleanly():
    async def go():
        c = Contact("nosecret", "Desk", "webhook", "http://x/notice", "agency", ["incident.verified"], agency=Agency.police)
        r = Rig(webhook=make_webhook(lambda req: httpx.Response(200)), contacts=[c])
        await r.feed(detection())
        (n,) = notices(r, "webhook")
        assert n.status == "failed" and "secret" in n.last_error
    run(go())


# ------------------------------------------------------------------ gateways and the receipt endpoint


TOKENS = {"ops": Principal("op1"), "police": Principal("police-app", Agency.police)}
OPS = {"Authorization": "Bearer ops"}
SECRET = "receipt-secret"


async def api_for(r):
    app = create_api(r.system, TOKENS, "s", run_bus=False)
    r.system.bus.start()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms"), app


def test_receipt_endpoint_needs_a_valid_signature_and_updates_the_notice():
    async def go():
        r = Rig(sms=[Scripted("primary"), Scripted("fallback")], contacts=[DUTY], receipt_secrets={"primary": SECRET})
        http, _ = await api_for(r)
        await r.feed(detection())
        (n,) = notices(r)
        body = json.dumps({"message_id": n.provider_ref, "status": "delivered"}).encode()
        assert (await http.post("/v1/notifications/receipts/primary", content=body)).status_code == 401
        assert (await http.post("/v1/notifications/receipts/primary", content=body, headers={"X-Signature": "00"})).status_code == 401
        assert (await http.post("/v1/notifications/receipts/unknown", content=body, headers={"X-Signature": sign_receipt(SECRET, body)})).status_code == 401
        ok = await http.post("/v1/notifications/receipts/primary", content=body, headers={"X-Signature": sign_receipt(SECRET, body)})
        assert ok.status_code == 200 and ok.json()["status"] == "delivered" and n.status == "delivered"
        ghost = json.dumps({"message_id": "nope", "status": "delivered"}).encode()
        assert (await http.post("/v1/notifications/receipts/primary", content=ghost, headers={"X-Signature": sign_receipt(SECRET, ghost)})).status_code == 404
        bad = json.dumps({"message_id": "x", "status": "maybe"}).encode()
        assert (await http.post("/v1/notifications/receipts/primary", content=bad, headers={"X-Signature": sign_receipt(SECRET, bad)})).status_code == 422
    run(go())


def test_list_and_retry_endpoints_are_operator_only_and_show_masked_addresses():
    async def go():
        from test_api import check
        r = Rig(sms=[Scripted("primary", "transient"), Scripted("fallback", "transient")], contacts=[DUTY], notify=FAST)
        http, _ = await api_for(r)
        await r.feed(detection())
        iid = str(r.incident().id)
        assert (await http.get(f"/v1/incidents/{iid}/notifications", headers={"Authorization": "Bearer police"})).status_code == 403
        listing = (await http.get(f"/v1/incidents/{iid}/notifications", headers=OPS)).json()
        (row,) = listing["items"]
        check("Notification", row)
        assert row["address"] == "+91*******001" and row["status"] == "failed" and row["contact"] == "Duty officer NH48"
        assert "+910000000001" not in json.dumps(listing)
        r.system.notifications.sms[0].script = ["ok"]
        r.system.notifications.sms[1].script = ["ok"]
        retried = await http.post(f"/v1/notifications/{row['id']}/retry", headers=OPS)
        assert retried.status_code == 200 and retried.json()["status"] == "sent"
        again = await http.post(f"/v1/notifications/{row['id']}/retry", headers=OPS)
        assert again.status_code == 409 and again.json()["current_state"] == "sent"
        assert (await http.get("/v1/incidents/00000000-0000-0000-0000-000000000000/notifications", headers=OPS)).status_code == 404
    run(go())


def test_sms_gateway_simulator_end_to_end_with_a_signed_receipt_posted_back_to_the_api():
    async def go():
        r = Rig(contacts=[DUTY], receipt_secrets={"primary": SECRET, "fallback": SECRET})
        api_client, api = await api_for(r)
        gateway = create_sms(seed=1, receipt_secret=SECRET, callback_client=api_client,
                             profiles={"primary": Profile(receipt_delay_s=(0.01, 0.02))})
        gw_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://sms")
        cb = "http://atms/v1/notifications/receipts"
        r.system.notifications.sms = [HttpSmsProvider("primary", gw_client, callback_url=f"{cb}/primary"),
                                      HttpSmsProvider("fallback", gw_client, callback_url=f"{cb}/fallback")]
        await r.feed(detection())
        (n,) = notices(r)
        assert n.status in ("sent", "delivered") and n.provider_ref == "primary-00001"
        for _ in range(100):
            if n.status == "delivered":
                break
            await asyncio.sleep(0.02)
        assert n.status == "delivered"                                                 # receipt came back signed and was accepted
        assert gateway.state.sim.messages[("primary", str(n.id))]["to"] == "+910000000001"
        assert len(gateway.state.sim.messages) == 1
    run(go())


def test_gateway_simulator_failure_modes_map_to_provider_errors():
    async def go():
        gateway = create_sms(seed=1, profiles={"p": Profile(timeout_sleep_s=0.5)})
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://sms")
        p = HttpSmsProvider("p", c, timeout_s=5)
        assert (await p.send("+910000000001", "hi", "k1")).provider_ref == "p-00001"
        assert (await p.send("+910000000001", "hi", "k1")).provider_ref == "p-00001"          # same key, same message
        with pytest.raises(ProviderError) as e:
            await p.send("12345", "hi", "k2")
        assert e.value.transient is False                                                       # not a phone number
        for behavior, transient in (("transient", True), ("permanent", False)):
            class H(HttpSmsProvider):
                async def send(self, to, text, key, _b=behavior):
                    resp = await self.client.post("/p/p/send", json={"to": to, "text": text, "idempotency_key": key},
                                                  headers={"X-Sim-Behavior": _b})
                    raise ProviderError("x", transient=resp.status_code >= 500)
            with pytest.raises(ProviderError) as e:
                await H("p", c).send("+910000000001", "hi", f"k-{behavior}")
            assert e.value.transient is transient
    run(go())


def test_agency_simulator_notice_endpoint_rejects_unsigned_and_stale_requests():
    async def go():
        app = create_agencies(seed=1)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agencies")
        sender = WebhookSender(c)
        await sender.send("/v1/agencies/police/notice", b'{"notice": "x"}', "dev-secret-police")
        assert app.state.sim.notices[0]["notice"] == "x"
        with pytest.raises(ProviderError):
            await sender.send("/v1/agencies/police/notice", b'{"notice": "x"}', "wrong-secret")
        stale = sign_webhook("dev-secret-police", b'{"notice": "y"}', now=0)
        resp = await c.post("/v1/agencies/police/notice", content=b'{"notice": "y"}', headers={"X-ATMS-Signature": stale})
        assert resp.status_code == 401 and len(app.state.sim.notices) == 1
    run(go())
