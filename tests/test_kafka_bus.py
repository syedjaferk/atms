"""Needs Kafka from compose:

    docker compose up -d --wait kafka
    ATMS_KAFKA=localhost:9092 .venv/bin/pytest tests/test_kafka_bus.py
"""
import asyncio
import uuid

import pytest
from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient

from atms_contracts.models import IncidentResolved, IncidentResolvedData, IncidentVerified, IncidentVerifiedData
from atms_core.kafka_bus import DeliveryError, KafkaBus

from conftest import KAFKA, _buses, _topics, run

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not KAFKA, reason="ATMS_KAFKA not set")]


def new_bus(name=None, group=None, **kw) -> KafkaBus:
    topic = name or f"atms-kb-{uuid.uuid4().hex[:10]}"
    if topic not in _topics:
        _topics.append(topic)
    bus = KafkaBus(KAFKA, topic, topic + ".dlq", group_prefix=group or topic, partitions=3, retry_backoff_s=0.01, **kw)
    _buses.append(bus)
    return bus


def verified(incident=None) -> IncidentVerified:
    return IncidentVerified(producer="t", incident_id=incident or uuid.uuid4(), data=IncidentVerifiedData(verified_by="auto"))


def resolved(incident) -> IncidentResolved:
    return IncidentResolved(producer="t", incident_id=incident, data=IncidentResolvedData(resolved_by="op"))


async def until(cond, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition never became true"
        await asyncio.sleep(0.02)


def dlq_messages(bus: KafkaBus, want: int):
    c = Consumer({"bootstrap.servers": KAFKA, "group.id": f"dlq-{uuid.uuid4().hex}", "auto.offset.reset": "earliest"})
    c.subscribe([bus.dlq_topic])
    out, end = [], asyncio.get_event_loop_policy() and 0
    import time
    end = time.time() + 15
    while len(out) < want and time.time() < end:
        m = c.poll(0.5)
        if m and not m.error():
            out.append(m)
    c.close()
    return out


def test_events_for_one_incident_are_handled_in_publish_order_by_each_group():
    async def go():
        bus = new_bus()
        seen = {"a": [], "b": []}

        async def mk(name):
            async def h(ev):
                seen[name].append(ev.event_id)
            return h
        bus.subscribe("a", None, await mk("a"))
        bus.subscribe("b", None, await mk("b"))
        inc = uuid.uuid4()
        sent = [verified(inc) for _ in range(40)]
        for e in sent:
            bus.publish(e)
        await bus.idle()
        expected = [e.event_id for e in sent]
        assert seen["a"] == expected and seen["b"] == expected      # same order, and every group gets everything
        await bus.stop()
    run(go())


def test_a_group_only_receives_the_types_it_subscribed_to():
    async def go():
        bus = new_bus()
        got = []

        async def h(ev):
            got.append(ev.type)
        bus.subscribe("only-resolved", ["incident.resolved"], h)
        inc = uuid.uuid4()
        bus.publish(verified(inc))
        bus.publish(resolved(inc))
        bus.publish(verified(inc))
        await bus.idle()
        assert got == ["incident.resolved"]
        await bus.stop()
    run(go())


def test_a_restarted_group_resumes_after_its_last_finished_event_not_from_the_start():
    async def go():
        topic = f"atms-kb-{uuid.uuid4().hex[:10]}"
        first = new_bus(topic)
        got1 = []

        async def h1(ev):
            got1.append(ev.event_id)
        first.subscribe("svc", None, h1)
        inc = uuid.uuid4()
        sent = [verified(inc) for _ in range(5)]
        for e in sent:
            first.publish(e)
        await first.idle()
        await first.stop()                                          # clean stop stores and commits the offsets
        assert got1 == [e.event_id for e in sent]

        second = new_bus(topic)
        got2 = []

        async def h2(ev):
            got2.append(ev.event_id)
        second.subscribe("svc", None, h2)
        later = verified(inc)
        second.publish(later)
        await second.idle()
        assert got2 == [later.event_id]                              # the first five are not handled again
        await second.stop()
    run(go())


def test_a_crash_before_an_event_finishes_means_that_event_is_delivered_again_after_restart():
    async def go():
        topic = f"atms-kb-{uuid.uuid4().hex[:10]}"
        bus = new_bus(topic)
        inc = uuid.uuid4()
        sent = [verified(inc) for _ in range(3)]
        started = asyncio.Event()
        done = []

        async def hang_on_third(ev):
            if ev.event_id == sent[2].event_id:
                started.set()
                await asyncio.sleep(3600)                           # the process dies in the middle of this one
            done.append(ev.event_id)
        bus.subscribe("svc", None, hang_on_third)
        bus.start()
        for e in sent:
            bus.publish(e)
        bus.flush()
        await asyncio.wait_for(started.wait(), 20)
        await bus.stop()                                            # cancelled mid handler, third offset never stored
        assert done == [sent[0].event_id, sent[1].event_id]

        again = new_bus(topic)
        redo = []

        async def h(ev):
            redo.append(ev.event_id)
        again.subscribe("svc", None, h)
        again.start()
        await again.ready()
        await until(lambda: len(redo) >= 1)
        await asyncio.sleep(0.5)
        assert redo == [sent[2].event_id]                           # exactly the unfinished one, once
        await again.stop()
    run(go())


def test_a_poison_event_goes_to_the_dead_letter_topic_and_the_group_carries_on():
    async def go():
        bus = new_bus()
        got = []
        attempts = {"n": 0}
        bad = verified()

        async def h(ev):
            if ev.event_id == bad.event_id:
                attempts["n"] += 1
                raise RuntimeError("cannot handle this")
            got.append(ev.event_id)
        bus.subscribe("svc", None, h)
        ok1, ok2 = verified(), verified()
        for e in (ok1, bad, ok2):
            bus.publish(e)
        await bus.idle()
        assert got == [ok1.event_id, ok2.event_id]                  # the partition was not stalled
        assert attempts["n"] == 3                                   # retried before giving up
        (m,) = dlq_messages(bus, 1)
        headers = {k: v.decode() for k, v in m.headers()}
        assert headers["group"] == "svc" and "cannot handle this" in headers["reason"]
        assert bus.dead and bus.dead[0][1].event_id == bad.event_id
        await bus.stop()
    run(go())


def test_garbage_on_the_topic_is_dead_lettered_not_fatal():
    async def go():
        bus = new_bus()
        got = []

        async def h(ev):
            got.append(ev.event_id)
        bus.subscribe("svc", None, h)
        raw = Producer({"bootstrap.servers": KAFKA})
        bus.ensure_topics()
        raw.produce(bus.topic, b"this is not an event", key="x")
        raw.flush(5)
        good = verified()
        bus.publish(good)
        await bus.idle(timeout_s=30)
        await until(lambda: got == [good.event_id])
        assert len(dlq_messages(bus, 1)) == 1
        await bus.stop()
    run(go())


def test_a_broadcast_group_sees_only_events_published_after_it_joined():
    async def go():
        bus = new_bus()
        normal, live = [], []

        async def hn(ev):
            normal.append(ev.event_id)

        async def hl(ev):
            live.append(ev.event_id)
        bus.subscribe("svc", None, hn)
        bus.subscribe("gateway", None, hl, broadcast=True)
        bus.start()
        await bus.ready()
        await asyncio.sleep(1.5)                  # a "from now" consumer needs a moment to learn where now is
        a = verified()
        bus.publish(a)
        await bus.idle()
        await until(lambda: live == [a.event_id])
        assert normal == [a.event_id]
        await bus.stop()
    run(go())


def test_flush_raises_when_kafka_cannot_confirm_so_the_outbox_keeps_the_event():
    bus = KafkaBus("localhost:1", "nowhere", partitions=1)
    bus.publish(verified())
    with pytest.raises(DeliveryError):
        bus.flush(1.0)
