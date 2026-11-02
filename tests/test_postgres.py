"""Needs the Postgres from compose:

    docker compose up -d --wait postgres
    ATMS_DB_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/pytest tests/test_postgres.py
"""
import asyncio

import httpx
import psycopg
import pytest

from atms_contracts.models import Agency, AssignmentState as A, IncidentState as I, Location, ResponderStatus as R, Severity
from atms_core.api import Principal, create_api
from atms_core.geo import FacilityService, haversine_m
from atms_core.pg import PostgresFacilities, PostgresRepo, migrate
from atms_core.repo import Batch, StaleWrite
from atms_core.store import Assignment, Incident, Store

from conftest import DB_URL, LOC1, T0, Rig, detection, new_schema, pg_parts, run, video

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DB_URL, reason="ATMS_DB_URL not set")]


def q(schema, sql, *args):
    with psycopg.connect(DB_URL, options=f"-c search_path={schema},public", autocommit=True) as c:
        return c.execute(sql, args).fetchall()


def test_migrations_apply_once_and_are_safe_to_rerun():
    schema = new_schema()
    assert migrate(DB_URL, schema) == []
    assert q(schema, "select version from schema_migrations order by version") == [("0001_init",), ("0002_outbox",), ("0003_notifications",), ("0004_forecasting",)]
    tables = {r[0] for r in q(schema, "select table_name from information_schema.tables where table_schema=%s", schema)}
    assert {"incident", "detection", "assignment", "timeline_entry", "evidence", "traffic_plan", "incident_report",
            "audit_log", "idempotency_key", "facility", "low_confidence_detection", "outbox", "contact", "notification", "traffic_observation", "weather_observation", "forecast_log"} <= tables


def test_a_full_incident_is_in_the_database_not_just_in_memory():
    async def go():
        r = Rig()
        await r.feed(detection(), video())
        return r
    r = run(go())
    inc = r.incident()
    (row,) = q(r.schema, "select state, severity, confidence, version, plan_id, array_to_string(lanes, ',') from incident")
    assert row[:4] == ("dispatched", "major", 0.94, inc.version) and row[4] == inc.plan_id and row[5] == "LANE_1,LANE_2"
    assert q(r.schema, "select count(*) from assignment where state='acked'") == [(4,)]
    assert q(r.schema, "select count(*) from detection") == [(1,)]
    assert q(r.schema, "select kind, camera_id from evidence") == [("clip", "GNT001")]
    assert q(r.schema, "select count(*) from audit_log") == [(len(r.system.bus.log),)]
    (plan,) = q(r.schema, "select jsonb_object_keys(messages) from traffic_plan order by 1 limit 1")
    assert plan == ("VMS-NH48-01",)
    kinds = [k for (k,) in q(r.schema, "select kind from timeline_entry order by id")]
    assert "verified" in kinds and "traffic" in kinds and kinds.count("assignment") >= 4


def test_after_a_restart_the_incident_resumes_and_finishes_with_the_original_lane_plan_reverted():
    async def go():
        r = Rig()
        await r.feed(detection(), video())
        iid, before = r.incident().id, r.incident()
        snapshot = (before.state, before.version, before.confidence, sorted(before.cameras), before.plan_id,
                    [(t.kind, t.summary) for t in before.timeline], before.evidence)
        assigns = {a.agency: (a.id, a.state, a.unit_ref, a.eta_s) for a in r.store.assignments.values()}

        await r.restart()                                              # new process: nothing in memory but the database
        inc = r.incident()
        assert inc.id == iid
        assert (inc.state, inc.version, inc.confidence, sorted(inc.cameras), inc.plan_id,
                [(t.kind, t.summary) for t in inc.timeline], inc.evidence) == snapshot
        assert {a.agency: (a.id, a.state, a.unit_ref, a.eta_s) for a in r.store.assignments.values()} == assigns
        assert r.store.plans[iid].messages and not r.store.plans[iid].reverted

        # the same detection arriving again after the restart must not create a second incident
        await r.feed(detection())
        assert len(r.store.incidents) == 1

        # responders carry on exactly where they left off
        svc = r.service
        for a in list(r.store.assignments.values()):
            for status in (R.en_route, R.on_scene, R.done):
                svc.update_assignment(iid, a.id, status, None, a.agency)
        assert inc.state == I.clearing
        r.vms.showing.update({"VMS-NH48-01": "x"})               # boards still lit from before the restart
        svc.resolve(iid, "police")
        await r.system.settle()
        assert inc.state == I.closed and not r.vms.showing        # persisted plan was found and reverted

        await r.restart()                                              # restart again, a closed incident stays closed
        assert r.incident().state == I.closed and r.incident().report_ref in r.store.reports
        assert not r.system.bus.dead
    run(go())


def test_a_second_camera_after_restart_still_merges_into_the_open_incident():
    async def go():
        from datetime import timedelta

        from conftest import GNT002, LOC2
        r = Rig()
        await r.feed(detection(confidence=0.7))
        await r.restart()
        await r.feed(detection(site=GNT002, loc=LOC2, confidence=0.72, at=T0 + timedelta(seconds=5)))
        assert len(r.store.incidents) == 1 and r.incident().cameras == {"GNT001", "GNT002"}
    run(go())


def test_one_failed_write_rolls_back_the_whole_operation():
    schema = new_schema()
    repo, _ = pg_parts(schema)
    store = Store(repo)
    from uuid import uuid4
    inc = Incident(uuid4(), I.detected, Severity.major, 0.9, Location(lat=18.4, lon=73.8), 0.0, "MH/PUNE/NH48",
                   T0, T0, "t", ["LANE_1"])
    store.incidents[inc.id] = inc
    store.mark(inc)
    orphan = Assignment(uuid4(), uuid4(), Agency.police, A.requested, T0)      # its incident does not exist
    store.assignments[orphan.id] = orphan
    store.mark(orphan)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.commit()
    assert q(schema, "select count(*) from incident") == [(0,)]                # the incident write was undone too
    # the marks stay, so once the bad row is removed the next commit writes the incident
    store.assignments.pop(orphan.id)
    store._dirty_asg.pop(orphan.id)
    store.commit()
    assert q(schema, "select count(*) from incident") == [(1,)]


def test_database_outage_during_an_operation_is_raised_then_recovered_on_the_next_commit():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.7))
        inc = r.incident()
        real = r.store.repo.write
        r.store.repo.write = lambda b: (_ for _ in ()).throw(psycopg.errors.AdminShutdown("db down"))
        with pytest.raises(psycopg.errors.AdminShutdown):
            r.service.add_note(inc.id, "written while the database is down", "op1")
        r.store.repo.write = real
        r.service.add_note(inc.id, "second note", "op1")                        # the first one is retried with it
        notes = [s for (s,) in q(r.schema, "select summary from timeline_entry where kind='note' order by id")]
        assert notes == ["written while the database is down", "second note"]
    run(go())


def test_stale_writer_cannot_overwrite_a_newer_version():
    schema = new_schema()
    repo, _ = pg_parts(schema)
    from uuid import uuid4
    inc = Incident(uuid4(), I.detected, Severity.major, 0.9, Location(lat=18.4, lon=73.8), 0.0, "MH/PUNE/NH48",
                   T0, T0, "t", ["LANE_1"], version=5)
    repo.write(Batch(incidents=[inc]))
    old = Incident(inc.id, I.verified, Severity.major, 0.9, inc.location, 0.0, inc.road_segment, T0, T0, "t", ["LANE_1"], version=3)
    with pytest.raises(StaleWrite):
        repo.write(Batch(incidents=[old]))
    assert q(schema, "select state, version from incident") == [("detected", 5)]


def test_idempotency_key_survives_a_restart():
    async def go():
        from conftest import Rig
        from test_api import OPS, TOKENS

        r = Rig()
        await r.feed(detection(confidence=0.7))
        iid = str(r.incident().id)
        app = create_api(r.system, TOKENS, "s", run_bus=False)
        h = {**OPS, "Idempotency-Key": "once"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
            first = await c.post(f"/v1/incidents/{iid}/verify", headers=h)
        await r.restart()
        app2 = create_api(r.system, TOKENS, "s", run_bus=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://x") as c:
            again = await c.post(f"/v1/incidents/{iid}/verify", headers=h)     # client retries after the crash
        assert first.status_code == again.status_code == 200 and first.json() == again.json()
    run(go())


def test_postgis_ranks_facilities_like_the_straight_line_seed_and_within_half_a_percent():
    schema = new_schema()
    repo, geo = pg_parts(schema)
    seed = FacilityService()
    for agency in Agency:
        a, b = geo.nearest(agency, LOC1, 3), seed.nearest(agency, LOC1, 3)
        assert [x.facility.id for x in a] == [x.facility.id for x in b]
        for x, y in zip(a, b):
            assert abs(x.distance_m - y.distance_m) / y.distance_m < 0.005
            assert x.eta_s > 0
    nearest = geo.nearest(Agency.ambulance, LOC1, 1)[0]
    assert nearest.facility.name == "Bharati Hospital" and nearest.distance_m < 1500


def test_a_crash_in_the_middle_of_sending_resumes_with_the_same_idempotency_key():
    async def go():
        from test_notifications import DUTY, Scripted

        class Hang(Scripted):
            blocking = True

            async def send(self, to, text, key):
                if self.blocking:
                    self.calls.append((to, text, key))
                    await asyncio.sleep(3600)                    # the process dies while the gateway call is open
                return await super().send(to, text, key)

        hang, fallback = Hang("primary"), Scripted("fallback")
        r = Rig(sms=[hang, fallback], contacts=[DUTY])
        r.system.bus.start()
        r.system.ingest(detection())
        for _ in range(300):
            if hang.calls:
                break
            await asyncio.sleep(0.01)
        (row,) = q(r.schema, "select status, attempts from notification")
        assert row == ("pending", 1)                              # stored before sending, attempt already counted

        hang.blocking = False
        await r.restart()
        await r.system.start(sweep_interval_s=0.1)
        await r.system.settle()
        ((status, attempts, ref, nid),) = q(r.schema, "select status, attempts, provider_ref, id from notification")
        assert (status, attempts) == ("sent", 2) and ref
        assert {c[2] for c in hang.calls} == {str(nid)}           # the retry carried the same key, so no second text
        await r.system.stop()
    run(go())


def test_a_receipt_after_a_restart_still_finds_its_notice():
    async def go():
        from test_notifications import DUTY, Scripted

        r = Rig(sms=[Scripted("primary"), Scripted("fallback")], contacts=[DUTY])
        await r.feed(detection())
        (n,) = list(r.store.notifications.values())
        ref = n.provider_ref
        await r.restart()
        r.system.notifications.record_receipt("primary", ref, True)
        assert q(r.schema, "select status from notification") == [("delivered",)]
    run(go())


def test_contacts_come_from_the_database_when_it_has_them():
    async def go():
        from atms_core.pg import seed_contacts
        from test_notifications import DUTY, SUP

        r = Rig()
        seed_contacts(DB_URL, r.schema, [DUTY, SUP])
        await r.restart()
        assert [c.id for c in r.store.contacts] == ["duty", "sup"]
        await r.feed(detection())
        assert {n.contact_id for n in r.store.notifications.values()} == {"duty"}
    run(go())
