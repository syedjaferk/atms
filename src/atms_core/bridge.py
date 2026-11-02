"""Bridge. RabbitMQ (MQTT devices) to the event bus.

convert() is pure and decides what happens to one delivery. RabbitBridge moves deliveries
through it, acking RabbitMQ only after the sink confirms the write.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from atms_contracts.models import AccidentDetected, Envelope, UnknownEventType, VideoReady, parse_event
from atms_contracts.topics import (BRIDGE_BINDING_KEY, KAFKA_DLQ_TOPIC, KAFKA_EVENTS_TOPIC, InvalidTopic,
                                   from_routing_key, parse_topic)

log = logging.getLogger("atms.bridge")


@dataclass(frozen=True)
class Record:
    topic: str
    key: str
    value: bytes
    headers: dict[str, str]


class Sink(Protocol):
    def send(self, record: Record) -> None:
        """Return only when the write is confirmed. Raise on failure."""


def _camera_of(event: Envelope) -> str | None:
    if isinstance(event, AccidentDetected):
        return event.data.site.camera_id
    if isinstance(event, VideoReady):
        return event.data.camera_id
    return None


def convert(routing_key: str, body: bytes) -> Record | None:
    """None means drop silently (health topics). Anything invalid becomes a dead-letter record."""
    def dead(reason: str, key: str = "unknown") -> Record:
        return Record(KAFKA_DLQ_TOPIC, key, body, {"reason": reason, "routing_key": routing_key})

    try:
        topic = from_routing_key(routing_key)
        parts = parse_topic(topic)
    except InvalidTopic as exc:
        return dead(f"bad topic: {exc}")
    if parts.kind == "status":
        return None
    try:
        event = parse_event(body)
    except (UnknownEventType, ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return dead(f"invalid event: {type(exc).__name__}", parts.road_segment)
    camera = _camera_of(event)
    if camera != parts.camera_id:
        # a device may only speak for itself
        return dead(f"camera mismatch: topic {parts.camera_id}, event {camera}", parts.road_segment)
    if isinstance(event, AccidentDetected):
        site = event.data.site
        if f"{site.state}/{site.city}/{site.road}" != parts.road_segment:
            return dead("site does not match topic", parts.road_segment)
    return Record(KAFKA_EVENTS_TOPIC, parts.road_segment, body, {"event_type": event.type, "event_id": str(event.event_id)})


class BusSink:
    """Feeds the in-process bus. Used until services run as separate processes on Kafka."""

    def __init__(self, bus):
        self.bus = bus

    def send(self, record: Record) -> None:
        if record.topic == KAFKA_EVENTS_TOPIC:
            self.bus.publish(parse_event(record.value))


class KafkaSink:
    def __init__(self, bootstrap: str, **config):
        from confluent_kafka import Producer

        self._p = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all", **config})

    def send(self, record: Record) -> None:
        errors: list = []
        self._p.produce(record.topic, record.value, key=record.key, headers=list(record.headers.items()),
                        on_delivery=lambda err, _m: errors.append(err) if err else None)
        if self._p.flush(10) > 0:
            raise RuntimeError("kafka did not confirm the write in time")
        if errors:
            raise RuntimeError(f"kafka delivery failed: {errors[0]}")


class RabbitBridge:
    def __init__(self, sink: Sink, host: str = "localhost", port: int = 5672, user: str = "atms",
                 password: str = "atms-dev", queue: str = "atms.bridge"):
        import pika

        self.sink, self.queue_name = sink, queue
        self._conn = pika.BlockingConnection(pika.ConnectionParameters(host, port, credentials=pika.PlainCredentials(user, password)))
        self._ch = self._conn.channel()
        # quorum queue: a node loss does not drop events. Bound to everything under atms.
        self._ch.queue_declare(queue, durable=True, arguments={"x-queue-type": "quorum"})
        self._ch.queue_bind(queue, "amq.topic", BRIDGE_BINDING_KEY)
        self._ch.basic_qos(prefetch_count=50)

    def handle(self, ch, method, _props, body: bytes) -> None:
        record = convert(method.routing_key, body)
        try:
            if record is not None:
                self.sink.send(record)
        except Exception:  # noqa: BLE001 - leave it in RabbitMQ and try again, never lose an event
            log.exception("sink failed, requeueing")
            ch.basic_nack(method.delivery_tag, requeue=True)
            return
        ch.basic_ack(method.delivery_tag)

    def run(self, inactivity_timeout_s: float | None = None) -> None:
        """Consume until stopped. With a timeout, returns after that long without a message."""
        for method, props, body in self._ch.consume(self.queue_name, inactivity_timeout=inactivity_timeout_s):
            if method is None:
                break
            self.handle(self._ch, method, props, body)

    def close(self) -> None:
        self._conn.close()
