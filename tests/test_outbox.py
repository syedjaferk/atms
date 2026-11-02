"""Events are stored with the state that caused them and relayed after."""
from uuid import uuid4

import psycopg
import pytest

from atms_contracts.models import Agency, IncidentVerified, IncidentVerifiedData
from atms_core.bus import InMemoryBus
from atms_core.outbox import OutboxRelay
from atms_core.store import Assignment, Store

from conftest import DB_URL, T0, Rig, detection, new_schema, pg_parts, run
from atms_contracts.models import AssignmentState as A


def verified() -> IncidentVerified:
    return IncidentVerified(producer="t", incident_id=uuid4(), data=IncidentVerifiedData(verified_by="auto"))


class FlakyBus(InMemoryBus):
    def __init__(self, fail_on: int | None = None):
        super().__init__()
        self.down, self.calls, self.fail_on = False, 0, fail_on

    def publish(self, event, key=None):
        self.calls += 1
        if self.down or self.calls == self.fail_on:
            raise ConnectionError("bus down")
        super().publish(event, key)


def rig_store(bus):
    store = Store()
    store.relay = OutboxRelay(store.repo, bus)
    return store


def test_nothing_reaches_the_bus_until_the_state_is_committed():
    bus = InMemoryBus()
    store = rig_store(bus)
    store.emit(verified())
    assert bus.log == []
    store.commit()
    assert len(bus.log) == 1 and store.relay.backlog() == 0


def test_state_commits_even_when_the_bus_is_down_and_the_events_follow_in_order():
    bus = FlakyBus()
    store = rig_store(bus)
    first, second = verified(), verified()
    store.emit(first)
    store.emit(second)
    bus.down = True
    store.commit()                                   # must not raise, the state is safe
    assert bus.log == [] and store.relay.backlog() == 2
    with pytest.raises(ConnectionError):
        store.relay.deliver()                        # the sweeper keeps trying
    bus.down = False
    assert store.relay.deliver() == 2
    assert [e.event_id for e in bus.log] == [first.event_id, second.event_id]
    assert store.relay.deliver() == 0 and store.relay.backlog() == 0


def test_a_failure_part_way_redelivers_the_batch_so_nothing_is_lost_and_consumers_must_dedupe():
    bus = FlakyBus(fail_on=2)
    store = rig_store(bus)
    a, b = verified(), verified()
    store.emit(a)
    store.emit(b)
    store.commit()                                   # the second publish fails inside the relay
    assert store.relay.backlog() == 2                # neither is marked sent
    store.relay.deliver()
    ids = [e.event_id for e in bus.log]
    assert set(ids) == {a.event_id, b.event_id} and ids.count(a.event_id) == 2   # at least once, a repeats


def test_a_redelivered_event_changes_nothing_downstream():
    async def go():
        r = Rig()
        await r.feed(detection())
        inc = r.incident()
        snap = (inc.version, inc.state, len(inc.timeline), len(inc.evidence), len(r.store.assignments))
        for e in list(r.system.bus.log):
            r.system.bus.publish(e)                  # the whole history again, same event ids
        await r.system.settle()
        assert (inc.version, inc.state, len(inc.timeline), len(inc.evidence), len(r.store.assignments)) == snap
        assert len(r.store.incidents) == 1 and not r.system.bus.dead
    run(go())


pg = pytest.mark.skipif(not DB_URL, reason="ATMS_DB_URL not set")


def unsent(schema):
    with psycopg.connect(DB_URL, options=f"-c search_path={schema},public", autocommit=True) as c:
        return c.execute("select count(*) filter (where sent_at is null), count(*) from outbox").fetchone()


@pg
def test_a_failed_transaction_stores_no_events_either():
    schema = new_schema()
    repo, _ = pg_parts(schema)
    store = Store(repo)
    store.emit(verified())
    orphan = Assignment(uuid4(), uuid4(), Agency.police, A.requested, T0)       # its incident does not exist
    store.assignments[orphan.id] = orphan
    store.mark(orphan)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.commit()
    assert unsent(schema) == (0, 0)


@pg
def test_a_crash_between_commit_and_relay_loses_no_event_and_the_dispatch_still_happens():
    async def go():
        r = Rig()
        await r.feed(detection(confidence=0.7))                  # waits for an operator
        inc = r.incident()
        r.store.relay = None                                     # the process dies right after the commit
        r.service.verify(inc.id, operator_id="op1")
        assert unsent(r.schema)[0] == 1 and "incident.verified" not in r.event_types()

        await r.restart()                                        # new process, same database
        await r.system.start(sweep_interval_s=0.1)               # relays what the old one never sent
        await r.system.settle()
        assert unsent(r.schema)[0] == 0
        assert r.event_types().count("incident.verified") == 1
        assert r.incident().state.value == "dispatched" and len(r.store.assignments) == 4
        await r.system.stop()
    run(go())


@pg
def test_unanswered_dispatch_resumes_after_a_restart_without_a_second_unit():
    async def go():
        from atms_core.orchestrator import DispatchConfig
        r = Rig(behaviors={"ambulance": ["timeout"]}, timeout_s=0.2, dispatch=DispatchConfig(resume_grace_s=0.0))
        r.system.bus.start()
        r.system.ingest(detection())
        # wait until the ambulance request is in flight, then kill the process
        end = 3.0
        import asyncio
        t = asyncio.get_running_loop().time()
        while not any(e.type == "assignment.requested" and e.data.agency == Agency.ambulance for e in r.system.bus.log):
            assert asyncio.get_running_loop().time() - t < end
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        r.behaviors["ambulance"] = ["ack"]
        await r.restart()
        amb = [a for a in r.store.assignments.values() if a.agency == Agency.ambulance]
        assert len(amb) <= 1 and all(a.state in (A.requested, A.timed_out) for a in amb)   # on Kafka it may not be written yet
        await r.system.start(sweep_interval_s=0.1)               # resumes the unfinished request
        for _ in range(200):                                      # on Kafka the record can appear a moment after start
            await r.system.settle()
            amb = [a for a in r.store.assignments.values() if a.agency == Agency.ambulance]
            if amb and amb[0].state == A.acked:
                break
            await asyncio.sleep(0.05)
        assert len(amb) == 1 and amb[0].state == A.acked          # same assignment, answered, not duplicated
        await r.system.stop()
    run(go())
