"""Topic naming. One place decides how MQTT topics, AMQP routing keys and Kafka keys relate.

MQTT topic      atms/MH/PUNE/NH48/GNT001/event/accident
AMQP key        atms.MH.PUNE.NH48.GNT001.event.accident   (RabbitMQ maps / to .)
Kafka topic     incident-events, keyed by road segment MH/PUNE/NH48
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ROOT = "atms"
KAFKA_EVENTS_TOPIC = "incident-events"
KAFKA_DLQ_TOPIC = "incident-events.dlq"
BRIDGE_BINDING_KEY = f"{ROOT}.#"  # bound on amq.topic
MQTT_EVENT_FILTER = f"{ROOT}/+/+/+/+/event/#"
MQTT_STATUS_FILTER = f"{ROOT}/+/+/+/+/status"

_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_KINDS = {"event", "status"}


class InvalidTopic(ValueError):
    pass


@dataclass(frozen=True)
class TopicParts:
    state: str
    city: str
    road: str
    camera_id: str
    kind: str  # event or status
    name: str | None = None  # accident, for kind=event

    @property
    def road_segment(self) -> str:
        """Kafka partition key, so one stretch of road is ordered."""
        return f"{self.state}/{self.city}/{self.road}"


def _check(*segments: str) -> None:
    for s in segments:
        if not _SEGMENT.match(s):
            raise InvalidTopic(f"bad topic segment {s!r}: only letters, digits, _ and -")


def event_topic(state: str, city: str, road: str, camera_id: str, name: str = "accident") -> str:
    _check(state, city, road, camera_id, name)
    return f"{ROOT}/{state}/{city}/{road}/{camera_id}/event/{name}"


def status_topic(state: str, city: str, road: str, camera_id: str) -> str:
    _check(state, city, road, camera_id)
    return f"{ROOT}/{state}/{city}/{road}/{camera_id}/status"


def parse_topic(topic: str) -> TopicParts:
    parts = topic.split("/")
    if parts[0] != ROOT or len(parts) not in (6, 7) or parts[5] not in _KINDS:
        raise InvalidTopic(f"not an ATMS topic: {topic!r}")
    kind = parts[5]
    if (kind == "event") != (len(parts) == 7):
        raise InvalidTopic(f"not an ATMS topic: {topic!r}")
    _check(*parts[1:5], *parts[6:])
    return TopicParts(parts[1], parts[2], parts[3], parts[4], kind, parts[6] if len(parts) == 7 else None)


def to_routing_key(topic: str) -> str:
    """MQTT topic to the AMQP routing key RabbitMQ uses on amq.topic."""
    parse_topic(topic)
    return topic.replace("/", ".")


def from_routing_key(key: str) -> str:
    topic = key.replace(".", "/")
    parse_topic(topic)
    return topic


def device_publish_pattern(state: str, city: str, road: str, camera_id: str) -> str:
    """Regex for the RabbitMQ topic permission of one camera: its own subtree only."""
    _check(state, city, road, camera_id)
    return rf"^{ROOT}\.{state}\.{city}\.{road}\.{camera_id}\..*$"
