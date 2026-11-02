import os
import urllib.request

import pytest
from PIL import Image

from atms_contracts.models import Severity, parse_event
from atms_edge.detector import AccidentDetector, classify_severity, sample_frames
from atms_sim.edge import EdgeController, LocalClipStore
from atms_sim.outbox import Outbox
from atms_sim.publishers import InMemoryPublisher


def det(label, score, x0, y0, x1, y1):
    return {"label": label, "score": score, "box": {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1}}


def frame(w=300, h=200):
    return Image.new("RGB", (w, h))


def detector(accident_by_frame, vehicles, **kw):
    calls = iter(accident_by_frame)
    return AccidentDetector(lambda f: next(calls), lambda f: vehicles, **kw)


def test_no_accident_label_means_no_detection():
    d = detector([[det("vehicle", 0.99, 0, 0, 50, 50)]] * 3, [])
    assert d.analyze([frame()] * 3) is None


def test_single_frame_hit_is_not_enough_by_default():
    d = detector([[det("accident", 0.9, 10, 10, 90, 90)], [], []], [])
    assert d.analyze([frame()] * 3) is None


def test_confirmed_accident_counts_nearby_vehicles_only():
    acc = [det("accident", 0.9, 100, 50, 200, 150)]
    vehicles = [det("car", 0.9, 110, 60, 150, 100), det("truck", 0.8, 160, 90, 210, 140),
                det("car", 0.9, 0, 0, 20, 20),          # far away
                det("person", 0.9, 120, 60, 140, 90)]   # not a vehicle
    d = detector([acc, acc], vehicles)
    r = d.analyze([frame(), frame()])
    assert r.vehicles == 2 and r.severity == Severity.major
    assert (r.frames_confirmed, r.frames_sampled) == (2, 2)
    assert r.confidence == 0.9


def test_low_scores_are_ignored():
    d = detector([[det("accident", 0.3, 0, 0, 50, 50)]] * 2, [])
    assert d.analyze([frame()] * 2) is None


def test_vehicle_count_never_below_one_to_satisfy_the_contract():
    acc = [det("accident", 0.9, 100, 50, 200, 150)]
    r = detector([acc, acc], []).analyze([frame()] * 2)
    assert r.vehicles == 1 and r.severity == Severity.minor


def test_lanes_follow_the_box_position():
    left = [det("accident", 0.9, 5, 50, 60, 150)]       # inside the left third of 300px
    r = detector([left, left], []).analyze([frame()] * 2)
    assert r.lanes == ("LANE_1",)
    wide = [det("accident", 0.9, 50, 50, 250, 150)]
    r = detector([wide, wide], []).analyze([frame()] * 2)
    assert r.lanes == ("LANE_1", "LANE_2", "LANE_3")


def test_custom_lane_map():
    acc = [det("accident", 0.9, 100, 50, 200, 150)]
    r = detector([acc, acc], [], lanes={"FAST": (0.0, 0.5), "SLOW": (0.5, 1.0)}).analyze([frame()] * 2)
    assert r.lanes == ("FAST", "SLOW")


@pytest.mark.parametrize("vehicles,area,expected", [
    (1, 0.05, Severity.minor), (2, 0.05, Severity.major), (5, 0.05, Severity.critical),
    (1, 0.30, Severity.critical)])
def test_severity_rules(vehicles, area, expected):
    assert classify_severity(vehicles, area) == expected


def test_detection_flows_through_the_edge_as_a_valid_event(tmp_path):
    acc = [det("accident", 0.9, 100, 50, 200, 150)]
    r = detector([acc, acc], [det("car", 0.9, 110, 60, 150, 100)]).analyze([frame()] * 2)
    pub = InMemoryPublisher()
    edge = EdgeController(Outbox(), pub, LocalClipStore(tmp_path))
    clip = tmp_path / "in.mp4"
    clip.write_bytes(b"clipbytes")
    edge.report_detection("GNT001", r.confidence, r.severity, r.vehicles, list(r.lanes),
                          model_version=r.model_version, clip_path=clip)
    assert edge.drain() == 2
    event = parse_event(pub.sent[0][1])
    assert event.data.model_version == r.model_version and event.data.confidence == 0.9
    assert parse_event(pub.sent[1][1]).data.size_bytes == len(b"clipbytes")


def test_sample_frames_reads_a_real_clip(tmp_path):
    import cv2
    import numpy as np

    path = str(tmp_path / "c.mp4")
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for i in range(30):
        out.write(np.full((48, 64, 3), i * 8, dtype=np.uint8))
    out.release()
    frames = sample_frames(path, 5)
    assert len(frames) == 5 and frames[0].size == (64, 48)


@pytest.mark.skipif(not os.environ.get("ATMS_RUN_HF"), reason="downloads models (set ATMS_RUN_HF=1)")
def test_real_models_on_the_model_card_demo_images(tmp_path):
    base = "https://huggingface.co/hilmantm/detr-traffic-accident-detection/resolve/main/demo/"

    def load(name):
        path = tmp_path / name
        urllib.request.urlretrieve(base + name, path)
        return Image.open(path).convert("RGB")

    d = AccidentDetector.from_huggingface(min_confirmed_frames=1)
    hit = d.analyze([load("accident-2.png")])
    assert hit is not None and hit.confidence > 0.8
    assert d.analyze([load("traffic-jams-2.png")]) is None
