from datetime import timedelta
from uuid import uuid4

import pytest

from atms_contracts.models import Agency, AssignmentState as A, IncidentState as I, ResponderStatus as R, Severity
from atms_core.incident_service import Conflict, Forbidden, NotFound
from atms_core.orchestrator import AGENCIES_BY_SEVERITY, DispatchConfig

from conftest import GNT002, LOC2, T0, Rig, detection, run, video


def test_auto_verified_major_incident_is_dispatched_to_every_agency():
    async def go():
        r = Rig()
        await r.feed(detection(), video())
        inc = r.incident()
        assert inc.state == I.dispatched
        assert {a for a in r.assignments()} == set(AGENCIES_BY_SEVERITY[Severity.major])
        assert all(a.state == A.acked and a.unit_ref and a.eta_s for a in r.assignments().values())
        assert inc.evidence and inc.evidence[0]["camera_id"] == "GNT001"      # video attached
        assert not r.system.bus.dead
        # the lane plan went out on every board
        assert set(r.vms.showing) == {"VMS-NH48-01", "VMS-NH48-02", "VMS-NH48-03"}
        assert "LANE_1 LANE_2" in next(iter(r.vms.showing.values()))
        return r
    r = run(go())
    types = r.event_types()
    assert types.index("incident.created") < types.index("incident.verified") < types.index("assignment.requested")
    assert "traffic.plan.applied" in types


def test_critical_severity_adds_fire_and_minor_only_police_and_tow():
    async def go(sev):
        r = Rig()
        await r.feed(detection(severity=sev, confidence=0.97 if sev == Severity.critical else 0.75))
        return r
    crit = run(go(Severity.critical))
    assert Agency.fire in crit.assignments()
    minor = run(go(Severity.minor))
    # a minor detection is below the auto-verify severity, so it waits for an operator
    assert minor.incident().state == I.detected and not minor.assignments()


def test_middle_confidence_waits_for_an_operator_then_dispatches():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.74))
        inc = r.incident()
        assert inc.state == I.detected and not r.assignments() and not r.vms.showing
        r.service.verify(inc.id, operator_id="op1")
        await r.system.settle()
        assert inc.state == I.dispatched and r.vms.showing
    run(go())


def test_low_confidence_is_logged_and_makes_no_incident():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.45))
        assert not r.store.incidents and len(r.store.low_confidence) == 1
    run(go())


def test_second_camera_merges_into_the_same_incident():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.74))
        await r.feed(detection(site=GNT002, loc=LOC2, confidence=0.95, at=T0 + timedelta(seconds=4), lanes=("LANE_3",)))
        inc = r.incident()
        assert inc.cameras == {"GNT001", "GNT002"} and len(inc.detection_ids) == 2
        assert inc.confidence == 0.95 and inc.lanes == ["LANE_1", "LANE_2", "LANE_3"]
        assert inc.state == I.dispatched          # the merged confidence crossed the auto-verify line
        assert "incident.updated" in r.event_types()
        assert r.event_types().count("incident.created") == 1
    run(go())


def test_same_event_delivered_twice_changes_nothing():
    async def go():
        r = Rig()
        ev = detection()
        await r.feed(ev)
        def downstream():
            return len([e for e in r.system.bus.log if not e.producer.startswith("edge/")])
        before = (r.incident().version, downstream())
        await r.feed(ev)                                          # the bus delivers it again
        assert (r.incident().version, downstream()) == before and len(r.store.incidents) == 1
    run(go())


def test_far_or_opposite_or_late_detections_are_separate_incidents():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.7))
        await r.feed(detection(site=GNT002, loc=LOC2, confidence=0.7, heading=170.0, at=T0 + timedelta(seconds=3)))   # other carriageway
        await r.feed(detection(confidence=0.7, at=T0 + timedelta(minutes=30)))                                       # much later
        assert len(r.store.incidents) == 3
    run(go())


def test_responders_drive_the_incident_to_resolution_and_the_road_reopens():
    async def go():
        r = Rig()
        await r.feed(detection(), video())
        inc, svc = r.incident(), r.service
        a = r.assignments()
        # an out of order update is refused and changes nothing
        with pytest.raises(Conflict) as c:
            svc.update_assignment(inc.id, a[Agency.police].id, R.done, None, Agency.police)
        assert c.value.current_state == "acked"
        # an agency cannot touch another agency's assignment
        with pytest.raises(Forbidden):
            svc.update_assignment(inc.id, a[Agency.police].id, R.en_route, 60, Agency.ambulance)
        for agency, asg in a.items():
            svc.update_assignment(inc.id, asg.id, R.en_route, 120, agency)
        assert inc.state == I.dispatched
        svc.update_assignment(inc.id, a[Agency.police].id, R.on_scene, 0, Agency.police)
        assert inc.state == I.in_progress
        for agency, asg in a.items():
            if asg.state != A.on_scene:
                svc.update_assignment(inc.id, asg.id, R.on_scene, 0, agency)
            svc.update_assignment(inc.id, asg.id, R.done, None, agency)
        assert inc.state == I.clearing
        with pytest.raises(Conflict):
            svc.close(inc.id)
        svc.resolve(inc.id, "police")
        await r.system.settle()
        assert inc.state == I.closed and inc.report_ref
        assert not r.vms.showing                                  # boards cleared
        report = r.store.reports[inc.report_ref]
        assert {x["agency"] for x in report["assignments"]} == {a.value for a in a}
        assert not r.system.bus.dead
        return r
    r = run(go())
    types = r.event_types()
    assert types.index("incident.resolved") < types.index("traffic.plan.reverted") < types.index("incident.closed")
    assert "responder.status.changed" in types


def test_repeating_the_same_status_is_accepted_without_a_new_event():
    async def go():
        r = Rig()
        await r.feed(detection())
        inc, a = r.incident(), r.assignments()[Agency.police]
        r.service.update_assignment(inc.id, a.id, R.en_route, 60, Agency.police)
        n = len(r.system.bus.log)
        r.service.update_assignment(inc.id, a.id, R.en_route, 60, Agency.police)
        assert len(r.system.bus.log) == n
        with pytest.raises(NotFound):
            r.service.update_assignment(inc.id, uuid4(), R.en_route, 60, None)
    run(go())


def test_reject_after_dispatch_cancels_units_and_reverts_the_plan():
    async def go():
        r = Rig()
        await r.feed(detection())
        inc = r.incident()
        r.service.reject(inc.id, "debris, not a crash", "op1")
        await r.system.settle()
        assert inc.state == I.rejected
        assert all(a.state == A.cancelled for a in r.assignments().values())
        assert not r.vms.showing
        cancels = [c for c in r.agency_app.state.sim.calls if c[2] == "cancel"]
        assert len(cancels) == len(AGENCIES_BY_SEVERITY[Severity.major])
        assert not r.system.bus.dead
        r.service.close(inc.id)                                   # operator closes the rejected incident
        assert inc.state == I.closed
    run(go())


def test_reject_while_dispatch_is_in_flight_stands_the_unit_down():
    async def go():
        r = Rig()
        # make ambulance slow but successful
        r.agency_app.state.sim.profiles[Agency.ambulance].ack_delay_s = (0.15, 0.15)
        r.hub.default_timeout_s = 2.0
        r.system.bus.start()
        r.system.ingest(detection())
        await _until(lambda: any(e.type == "assignment.requested" and e.data.agency == Agency.ambulance for e in r.system.bus.log))
        r.service.reject(r.incident().id, "false alarm", "op1")
        await r.system.settle()
        amb = r.assignments()[Agency.ambulance]
        assert amb.state == A.cancelled and not any(e.type == "assignment.acked" and e.data.agency == Agency.ambulance for e in r.system.bus.log)
        assert ("ambulance" in {c[0] for c in r.agency_app.state.sim.calls if c[2] == "cancel"})
    run(go())


async def _until(cond, timeout=3.0):
    import asyncio

    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition never became true"
        await asyncio.sleep(0.005)


def test_decline_moves_to_the_next_unit_with_a_new_key():
    async def go():
        r = Rig(behaviors={"tow": ["decline", "ack"]})
        await r.feed(detection())
        tow = r.assignments()[Agency.tow]
        assert tow.state == A.acked and tow.attempt == 2
        keys = [c[1] for c in r.agency_app.state.sim.calls if c[0] == "tow"]
        assert len(set(keys)) == 2 and keys[0].endswith(":0") and keys[1].endswith(":1")
    run(go())


def test_timeout_retries_the_same_unit_with_the_same_key_then_succeeds():
    async def go():
        r = Rig(behaviors={"ambulance": ["timeout", "ack"]})
        await r.feed(detection())
        amb = r.assignments()[Agency.ambulance]
        assert amb.state == A.acked and amb.attempt == 2
        assert "assignment.timed_out" in r.event_types()
        keys = [c[1] for c in r.agency_app.state.sim.calls if c[0] == "ambulance"]
        assert len(set(keys)) == 1                                # a retry never sends a second unit
    run(go())


def test_agency_that_never_answers_ends_declined_with_a_manual_call_note_and_incident_still_proceeds():
    async def go():
        r = Rig(behaviors={"fire": ["timeout"]}, dispatch=DispatchConfig(max_attempts=3, timeouts_per_unit=2))
        await r.feed(detection(severity=Severity.critical, confidence=0.97))
        fire = r.assignments()[Agency.fire]
        assert fire.state == A.declined
        declined = [e for e in r.system.bus.log if e.type == "assignment.declined" and e.data.agency == Agency.fire]
        assert declined and "call manually" in declined[-1].data.reason
        assert r.incident().state == I.dispatched                 # other agencies carried it
        assert not r.system.bus.dead
    run(go())


def test_dead_vms_board_is_reported_but_does_not_block_the_plan_or_dispatch():
    async def go():
        r = Rig()
        r.vms.down.add("VMS-NH48-02")
        await r.feed(detection())
        applied = next(e for e in r.system.bus.log if e.type == "traffic.plan.applied")
        assert applied.data.failed_devices == ["VMS-NH48-02"]
        assert set(applied.data.vms_messages) == {"VMS-NH48-01", "VMS-NH48-03"}
        assert r.incident().state == I.dispatched
        assert any(t.kind == "operator_task" for t in r.incident().timeline)
    run(go())


def test_invalid_operator_transitions_are_conflicts():
    async def go():
        r = Rig()
        await r.feed(detection())
        inc = r.incident()
        with pytest.raises(Conflict):
            r.service.verify(inc.id)                  # already verified
        with pytest.raises(Conflict):
            r.service.resolve(inc.id, "op")           # not clearing yet
        with pytest.raises(NotFound):
            r.service.verify(uuid4())
    run(go())


def test_every_event_carries_the_incident_trace_id():
    async def go():
        r = Rig()
        await r.feed(detection())
        return r
    r = run(go())
    inc = r.incident()
    mine = [e for e in r.system.bus.log if e.incident_id == inc.id]
    assert mine and {e.trace_id for e in mine} == {inc.trace_id}
    assert len(r.store.audit) == len(r.system.bus.log)
