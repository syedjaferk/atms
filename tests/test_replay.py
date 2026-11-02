"""Kafka delivers at least once, and a consumer group can be reset to the start. Replaying the whole
history on top of the stored state must change nothing and send nobody anywhere.

    ATMS_KAFKA=localhost:9092 ATMS_DB_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/pytest tests/test_replay.py
"""
import psycopg
import pytest

from atms_contracts.models import Agency, IncidentState as I, ResponderStatus as R, Severity
from atms_core.notify_providers import RecordingSms, RecordingWebhook
from conftest import DB_URL, KAFKA, Rig, detection, run, video

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not (DB_URL and KAFKA), reason="needs ATMS_DB_URL and ATMS_KAFKA")]


def new_rig() -> Rig:
    return Rig(sms=[RecordingSms("primary"), RecordingSms("fallback")], webhook=RecordingWebhook())


def snapshot(r: Rig):
    with psycopg.connect(DB_URL, options=f"-c search_path={r.schema},public", autocommit=True) as c:
        one = lambda sql: c.execute(sql).fetchall()
        return {
            "incident": one("select state, version, confidence, array_to_string(lanes, ','), plan_id, plan_reverted, report_ref from incident"),
            "assignment": one("select agency, state, unit_ref, eta_s, attempt from assignment order by agency"),
            "timeline": one("select kind, summary, actor from timeline_entry order by id"),
            "evidence": one("select kind, ref from evidence order by id"),
            "detection": one("select event_id, camera_id from detection order by seq"),
            "plan": one("select reverted, messages from traffic_plan"),
            "report": one("select count(*) from incident_report"),
            "audit": one("select count(*) from audit_log"),
            "outbox": one("select count(*), count(*) filter (where sent_at is null) from outbox"),
            "agency_calls": len(r.agency_app.state.sim.calls),
            "vms": dict(r.vms.showing),
            "notification": one("select contact_id, kind, status, attempts, provider, provider_ref from notification order by contact_id, kind"),
            "texts_sent": len(r.sms[0].sent) + len(r.sms[1].sent),
            "webhooks_sent": len(r.webhook.sent),
        }


async def full_lifecycle(r: Rig):
    await r.feed(detection(), video())
    inc, svc = r.incident(), r.service
    for a in list(r.store.assignments.values()):
        for status in (R.en_route, R.on_scene, R.done):
            svc.update_assignment(inc.id, a.id, status, 60 if status == R.en_route else None, a.agency)
    svc.resolve(inc.id, "police")
    await r.system.settle()
    assert inc.state == I.closed


def test_replaying_the_whole_topic_changes_nothing_and_calls_nobody():
    async def go():
        r = new_rig()
        await full_lifecycle(r)
        before = snapshot(r)
        assert before["outbox"][0][1] == 0 and before["agency_calls"] >= 4
        assert before["texts_sent"] >= 2 and before["webhooks_sent"] >= 4 and before["notification"]

        total = await r.replay_from_start()                      # fresh consumer groups read every event again
        assert total > 20
        assert snapshot(r) == before                              # same rows, same timeline, no new events
        assert r.system.bus.log == [] and not r.system.bus.dead   # the replaying services published nothing
    run(go())


def test_replay_in_the_middle_of_an_incident_does_not_dispatch_again_or_double_the_lane_plan():
    async def go():
        r = new_rig()
        await r.feed(detection(), video())
        assert r.incident().state == I.dispatched
        before = snapshot(r)
        await r.replay_from_start()
        assert snapshot(r) == before
        assert len(r.store.assignments) == 4 and len(r.store.incidents) == 1
    run(go())


def test_replay_of_a_rejected_incident_leaves_it_rejected_and_units_cancelled_once():
    async def go():
        r = new_rig()
        await r.feed(detection())
        r.service.reject(r.incident().id, "debris", "op1")
        await r.system.settle()
        before = snapshot(r)
        await r.replay_from_start()
        assert snapshot(r) == before
        assert all(a.state.value == "cancelled" for a in r.store.assignments.values())
    run(go())
