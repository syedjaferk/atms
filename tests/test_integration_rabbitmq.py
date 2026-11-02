"""Needs the compose stack. Run with:

    docker compose up -d --wait
    ATMS_BROKER_HOST=localhost .venv/bin/pytest -m integration
"""
import json
import os
import time

import pytest

from atms_contracts.models import parse_event
from atms_contracts.topics import BRIDGE_BINDING_KEY, to_routing_key
from atms_sim.edge import EdgeController, LocalClipStore
from atms_sim.outbox import Outbox
from atms_sim.publishers import MqttPublisher
from atms_sim.scenarios import SCENARIOS

HOST = os.environ.get("ATMS_BROKER_HOST")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not HOST, reason="ATMS_BROKER_HOST not set")]


def test_mqtt_publish_arrives_on_amqp_with_the_dotted_routing_key(tmp_path):
    import pika

    conn = pika.BlockingConnection(pika.ConnectionParameters(
        HOST, 5672, credentials=pika.PlainCredentials("atms", "atms-dev")))
    ch = conn.channel()
    q = ch.queue_declare("", exclusive=True).method.queue
    ch.queue_bind(q, "amq.topic", BRIDGE_BINDING_KEY)

    pub = MqttPublisher(HOST, 1883, "atms", "atms-dev", client_id="itest-GNT001")
    try:
        edge = EdgeController(Outbox(), pub, LocalClipStore(tmp_path))
        sent = edge.report(SCENARIOS["major_two_vehicle"])
        assert edge.run_until_empty(max_wait_s=10)
    finally:
        pub.close()

    got = {}
    deadline = time.time() + 10
    while len(got) < 2 and time.time() < deadline:
        method, _props, body = ch.basic_get(q, auto_ack=True)
        if method:
            got[method.routing_key] = body
        else:
            time.sleep(0.2)
    conn.close()

    accident_key = to_routing_key("atms/MH/PUNE/NH48/GNT001/event/accident")
    assert accident_key == "atms.MH.PUNE.NH48.GNT001.event.accident"
    assert set(got) == {accident_key, "atms.MH.PUNE.NH48.GNT001.event.video"}
    event = parse_event(got[accident_key])
    assert event.event_id == sent.event_id
    assert json.loads(got[accident_key])["data"]["confidence"] == 0.94


def test_full_path_mqtt_to_rabbitmq_to_bridge_to_kafka(tmp_path):
    """Edge simulator -> MQTT -> RabbitMQ -> RabbitBridge -> Kafka, then read it back from Kafka."""
    import uuid

    from confluent_kafka import Consumer

    from atms_contracts.topics import KAFKA_DLQ_TOPIC, KAFKA_EVENTS_TOPIC
    from atms_core.bridge import KafkaSink, RabbitBridge

    queue = f"atms.bridge.itest.{uuid.uuid4().hex[:8]}"
    bridge = RabbitBridge(KafkaSink(f"{HOST}:9092"), HOST, queue=queue)
    pub = MqttPublisher(HOST, 1883, "atms", "atms-dev", client_id="itest-bridge")
    try:
        edge = EdgeController(Outbox(), pub, LocalClipStore(tmp_path))
        sent = edge.report(SCENARIOS["major_two_vehicle"])
        assert edge.run_until_empty(max_wait_s=10)
        # a malformed message from a device goes to the dead-letter topic, not into the events topic
        pub.publish("atms/MH/PUNE/NH48/GNT001/event/accident", b"not json")
    finally:
        pub.close()
    bridge.run(inactivity_timeout_s=3)
    bridge._ch.queue_delete(queue)               # a durable test queue would otherwise pile up messages between runs
    bridge.close()

    consumer = Consumer({"bootstrap.servers": f"{HOST}:9092", "group.id": f"itest-{uuid.uuid4().hex}",
                         "auto.offset.reset": "earliest"})
    consumer.subscribe([KAFKA_EVENTS_TOPIC, KAFKA_DLQ_TOPIC])
    # the topics keep history from earlier runs, so read until both of this run's messages are found
    mine, dead = [], []
    end = time.time() + 60
    while time.time() < end and (not mine or not dead):
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        if msg.topic() == KAFKA_EVENTS_TOPIC:
            if json.loads(msg.value()).get("event_id") == str(sent.event_id):
                mine.append(msg)
        elif msg.value() == b"not json" and dict(msg.headers())["reason"].startswith(b"invalid event"):
            dead.append(msg)
    consumer.close()

    assert len(mine) == 1 and mine[0].key() == b"MH/PUNE/NH48"
    assert dead
