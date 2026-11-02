import re

import pytest

from atms_contracts.topics import (
    InvalidTopic, device_publish_pattern, event_topic, from_routing_key, parse_topic,
    status_topic, to_routing_key,
)


def test_event_topic_matches_the_documented_example():
    assert event_topic("MH", "PUNE", "NH48", "GNT001") == "atms/MH/PUNE/NH48/GNT001/event/accident"


def test_routing_key_is_the_topic_with_dots_and_round_trips():
    topic = event_topic("MH", "PUNE", "NH48", "GNT001")
    key = to_routing_key(topic)
    assert key == "atms.MH.PUNE.NH48.GNT001.event.accident"
    assert from_routing_key(key) == topic


def test_parse_gives_road_segment_for_the_kafka_key():
    parts = parse_topic(event_topic("MH", "PUNE", "NH48", "GNT001"))
    assert parts.road_segment == "MH/PUNE/NH48"
    assert (parts.camera_id, parts.kind, parts.name) == ("GNT001", "event", "accident")


def test_status_topic_has_no_event_name():
    parts = parse_topic(status_topic("MH", "PUNE", "NH48", "GNT001"))
    assert (parts.kind, parts.name) == ("status", None)


@pytest.mark.parametrize("bad", ["MH.X", "a/b", "NH 48", "", "x$"])
def test_segments_with_separators_are_refused(bad):
    with pytest.raises(InvalidTopic):
        event_topic("MH", "PUNE", bad, "GNT001")


@pytest.mark.parametrize("bad", [
    "other/MH/PUNE/NH48/GNT001/event/accident",
    "atms/MH/PUNE/NH48/GNT001/event",
    "atms/MH/PUNE/NH48/GNT001/status/extra",
    "atms/MH/PUNE/NH48/GNT001/bogus/accident",
])
def test_foreign_topics_are_refused(bad):
    with pytest.raises(InvalidTopic):
        parse_topic(bad)


def test_device_permission_covers_only_its_own_subtree():
    pattern = re.compile(device_publish_pattern("MH", "PUNE", "NH48", "GNT001"))
    assert pattern.match(to_routing_key(event_topic("MH", "PUNE", "NH48", "GNT001")))
    assert pattern.match(to_routing_key(status_topic("MH", "PUNE", "NH48", "GNT001")))
    assert not pattern.match(to_routing_key(event_topic("MH", "PUNE", "NH48", "GNT002")))
    assert not pattern.match(to_routing_key(event_topic("MH", "PUNE", "NH4", "GNT001")))
