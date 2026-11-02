import json

import pytest

from atms_contracts.models import AccidentDetected, VideoReady, parse_event
from atms_sim.edge import EdgeController, LocalClipStore
from atms_sim.outbox import Outbox
from atms_sim.publishers import InMemoryPublisher
from atms_sim.scenarios import SCENARIOS


@pytest.fixture
def edge(tmp_path):
    pub = InMemoryPublisher()
    return EdgeController(Outbox(), pub, LocalClipStore(tmp_path / "clips")), pub


def test_report_writes_to_outbox_and_touches_no_network(edge):
    ctl, pub = edge
    event = ctl.report(SCENARIOS["major_two_vehicle"])
    assert ctl.outbox.depth() == 2          # accident.detected and video.ready
    assert pub.sent == []
    assert event.data.confidence == 0.94


def test_drain_publishes_both_events_in_order_on_valid_topics(edge):
    ctl, pub = edge
    ctl.report(SCENARIOS["major_two_vehicle"])
    assert ctl.drain() == 2
    assert ctl.outbox.depth() == 0
    topics = [t for t, _ in pub.sent]
    assert topics == ["atms/MH/PUNE/NH48/GNT001/event/accident", "atms/MH/PUNE/NH48/GNT001/event/video"]
    types = [type(parse_event(p)) for _, p in pub.sent]
    assert types == [AccidentDetected, VideoReady]


def test_outage_keeps_events_then_delivers_when_broker_returns(edge):
    ctl, pub = edge
    ctl.report(SCENARIOS["major_two_vehicle"])
    pub.fail = True
    assert ctl.drain() == 0
    assert ctl.outbox.depth() == 2          # nothing lost
    assert [r.attempts for r in ctl.outbox.pending()] == [1, 0]  # stopped at the first failure, order kept
    pub.fail = False
    assert ctl.drain() == 2
    assert ctl.outbox.depth() == 0


def test_outbox_survives_a_restart(tmp_path):
    path = str(tmp_path / "outbox.sqlite3")
    first = EdgeController(Outbox(path), InMemoryPublisher(), LocalClipStore(tmp_path / "clips"))
    first.report(SCENARIOS["major_two_vehicle"])
    second_pub = InMemoryPublisher()
    second = EdgeController(Outbox(path), second_pub, LocalClipStore(tmp_path / "clips"))
    assert second.drain() == 2
    assert len(second_pub.sent) == 2


def test_adding_the_same_event_twice_is_harmless():
    box = Outbox()
    box.add("e1", "atms/MH/PUNE/NH48/GNT001/event/accident", b"x")
    box.add("e1", "atms/MH/PUNE/NH48/GNT001/event/accident", b"x")
    assert box.depth() == 1


def test_video_ready_points_at_a_real_clip_with_matching_hash(edge, tmp_path):
    import hashlib

    ctl, pub = edge
    ctl.report(SCENARIOS["major_two_vehicle"])
    ctl.drain()
    ready = json.loads(pub.sent[1][1])["data"]
    clip = (tmp_path / "clips" / ready["video_ref"].split("/")[-1]).read_bytes()
    assert len(clip) == ready["size_bytes"]
    assert hashlib.sha256(clip).hexdigest() == ready["sha256"]


def test_detections_in_one_bucket_share_a_dedup_key_and_second_camera_differs(edge):
    ctl, _ = edge
    a = ctl.report(SCENARIOS["major_two_vehicle"])
    b = ctl.report(SCENARIOS["duplicate_second_camera"])
    assert a.dedup_key != b.dedup_key      # different camera, correlator must merge them by location
    assert a.event_id != b.event_id


def test_every_scenario_is_valid(edge):
    ctl, pub = edge
    for s in SCENARIOS.values():
        ctl.report(s)
    assert ctl.drain() == 2 * len(SCENARIOS)
    for _, payload in pub.sent:
        parse_event(payload)
