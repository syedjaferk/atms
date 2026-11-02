import json

from atms_contracts.topics import KAFKA_DLQ_TOPIC, KAFKA_EVENTS_TOPIC, event_topic, status_topic, to_routing_key
from atms_core.bridge import BusSink, RabbitBridge, convert
from atms_core.bus import InMemoryBus

from conftest import GNT001, GNT002, LOC1, detection, video

RK = to_routing_key(event_topic("MH", "PUNE", "NH48", "GNT001"))


def test_valid_detection_goes_to_the_events_topic_keyed_by_road_segment():
    ev = detection()
    rec = convert(RK, ev.model_dump_json().encode())
    assert rec.topic == KAFKA_EVENTS_TOPIC and rec.key == "MH/PUNE/NH48"
    assert rec.headers == {"event_type": "accident.detected", "event_id": str(ev.event_id)}


def test_video_ready_is_accepted_on_the_video_subtopic():
    rec = convert(to_routing_key(event_topic("MH", "PUNE", "NH48", "GNT001", "video")), video().model_dump_json().encode())
    assert rec.topic == KAFKA_EVENTS_TOPIC


def test_health_topics_are_dropped():
    assert convert(to_routing_key(status_topic("MH", "PUNE", "NH48", "GNT001")), b"online") is None


def test_garbage_and_unknown_and_invalid_go_to_the_dead_letter_topic_with_a_reason():
    for body, why in [(b"not json", "invalid event"), (b'{"type": "nope"}', "invalid event"),
                      (json.dumps({**json.loads(detection().model_dump_json()), "data": {}}).encode(), "invalid event")]:
        rec = convert(RK, body)
        assert rec.topic == KAFKA_DLQ_TOPIC and why in rec.headers["reason"] and rec.value == body


def test_foreign_routing_key_is_dead_lettered():
    rec = convert("other.thing", b"{}")
    assert rec.topic == KAFKA_DLQ_TOPIC and "bad topic" in rec.headers["reason"]


def test_a_camera_cannot_publish_as_another_camera():
    rec = convert(RK, detection(site=GNT002).model_dump_json().encode())     # topic says GNT001, event says GNT002
    assert rec.topic == KAFKA_DLQ_TOPIC and "camera mismatch" in rec.headers["reason"]


def test_site_must_match_the_topic_road():
    ev = detection()
    obj = json.loads(ev.model_dump_json())
    obj["data"]["site"]["road"] = "NH4"
    rec = convert(RK, json.dumps(obj).encode())
    assert rec.topic == KAFKA_DLQ_TOPIC and "site does not match" in rec.headers["reason"]


def test_bus_sink_publishes_only_valid_events():
    bus = InMemoryBus()
    sink = BusSink(bus)
    sink.send(convert(RK, detection().model_dump_json().encode()))
    sink.send(convert(RK, b"junk"))
    assert [e.type for e in bus.log] == ["accident.detected"]


class _Ch:
    def __init__(self):
        self.acked, self.nacked = [], []

    def basic_ack(self, tag):
        self.acked.append(tag)

    def basic_nack(self, tag, requeue):
        self.nacked.append((tag, requeue))


class _M:
    def __init__(self, rk, tag):
        self.routing_key, self.delivery_tag = rk, tag


def test_rabbit_bridge_acks_after_the_sink_confirms_and_requeues_when_it_fails():
    class Flaky:
        def __init__(self):
            self.fail, self.got = True, []

        def send(self, rec):
            if self.fail:
                raise RuntimeError("kafka down")
            self.got.append(rec)

    sink, ch = Flaky(), _Ch()
    bridge = RabbitBridge.__new__(RabbitBridge)       # no broker needed to test the delivery handling
    bridge.sink = sink
    body = detection().model_dump_json().encode()
    bridge.handle(ch, _M(RK, 1), None, body)
    assert ch.acked == [] and ch.nacked == [(1, True)]          # nothing lost, will be redelivered
    sink.fail = False
    bridge.handle(ch, _M(RK, 1), None, body)
    bridge.handle(ch, _M(RK, 2), None, b"junk")                 # a poison message is dead-lettered then acked
    assert ch.acked == [1, 2] and [r.topic for r in sink.got] == [KAFKA_EVENTS_TOPIC, KAFKA_DLQ_TOPIC]
