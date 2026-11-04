import json
import shutil
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from atms_contracts import catalog as catalog_mod, network
from atms_contracts.models import Agency
from atms_core.geo import FacilityService, haversine_m
from atms_core.notifications import network_contacts
from atms_edge.detector import AccidentDetector
from atms_edge.service import create_service

from conftest import Rig, detection, run, video


def test_every_area_has_cameras_and_every_agency_close_enough_to_help():
    net = network.load()
    assert len(net.areas) >= 4
    assert len({c.site.camera_id for c in net.cameras.values()}) == len(net.cameras)
    geo = FacilityService()
    for cam in net.cameras.values():
        assert 6 < cam.location.lat < 36 and 68 < cam.location.lon < 98          # somewhere in India
        for agency in Agency:
            [nearest] = geo.nearest(agency, cam.location, 1)
            assert nearest.distance_m < 15_000, f"{cam.site.camera_id} has no {agency.value} station within 15 km"


def test_a_crash_in_chennai_is_dispatched_to_and_notified_through_chennai_only():
    net = network.load()
    cam = net.cameras["CHN003"]
    assert cam.site.city == "CHENNAI"

    async def go():
        r = Rig(contacts=network_contacts("http://agencies"))
        await r.feed(detection(site=cam.site, loc=cam.location, confidence=0.95), video("CHN003"))
        await r.system.settle()
        inc = r.incident()
        used = {a.facility for a in r.system.store.for_incident(inc.id) if a.facility}
        assert used and all("chn" in f for f in used)
        told = {n.contact_id for n in r.system.store.notifications.values() if n.incident_id == inc.id}
        assert "duty-chennai" in told and "duty-pune" not in told
        assert any(t.startswith("stn-") and "chn" in t for t in told)
        assert not any(t.startswith("stn-") and "chn" not in t for t in told), told       # no Pune, Bengaluru or Gurugram station was paged
    run(go())


def test_the_map_lists_every_area_and_a_chennai_incident_does_not_break_it():
    from atms_core.map_view import build_map

    cam = network.load().cameras["BLR002"]

    async def go():
        r = Rig()
        await r.feed(detection(site=cam.site, loc=cam.location, confidence=0.95), video("BLR002"))
        await r.system.settle()
        body = build_map(r.system)
        assert {a["id"] for a in body["areas"]} == set(network.load().areas)
        assert {c["id"] for c in body["cameras"]} == set(network.load().cameras)
        assert body["areas"][0]["corridor"] == [] and body["areas"][1]["corridor"]      # Pune has road sensors, Chennai gets a plain line
        [one] = body["incidents"]
        assert one["cameras"] == ["BLR002"] and one["segment"] is None
    run(go())


# ------------------------------------------------------------------ the catalog

def write_catalog(root, videos):
    (root / "clips").mkdir(parents=True)
    (root / "videos.json").write_text(json.dumps({"version": 1, "videos": videos}))


def entry(id="a1", camera="CHN001", scenario="bad", root="dataset", file="clips/a1.mp4"):
    return dict(id=id, camera_id=camera, title=id, scenario=scenario, truth="accident" if scenario == "bad" else "normal",
                label_source="ground truth", root=root, file=file, duration_s=3.0, size_bytes=10)


def test_catalog_groups_videos_by_camera_and_refuses_paths_outside_its_folders(tmp_path):
    write_catalog(tmp_path, [entry("a1"), entry("g1", scenario="good"), entry("x", camera="BLR001", file="../secret.mp4")])
    cat = catalog_mod.load(tmp_path, tmp_path / "videos")
    assert [v.id for v in cat.for_camera("CHN001")] == ["a1", "g1"][::-1] or {v.id for v in cat.for_camera("CHN001")} == {"a1", "g1"}
    assert {v.scenario for v in cat.for_camera("CHN001")} == {"good", "bad"}
    with pytest.raises(ValueError):
        cat.path(cat.videos["x"])
    assert not cat.exists(cat.videos["x"])


def test_the_real_catalog_gives_every_camera_good_and_bad_videos_that_exist():
    cat = catalog_mod.load()
    if not cat.videos:
        pytest.skip("dataset/videos.json not built, run python scripts/build_dataset.py")
    for cam in network.load().cameras:
        mine = cat.for_camera(cam)
        assert {v.scenario for v in mine} == {"good", "bad"}, cam
    assert all(v.camera_id in network.load().cameras for v in cat.videos.values())
    assert all(cat.exists(v) for v in cat.videos.values() if v.root == "dataset")


# ------------------------------------------------------------------ the detector service

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def make_clip(path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                    "-t", "4", "-pix_fmt", "yuv420p", str(path)], check=True)


class FakeEdge:
    def __init__(self):
        self.sent = []
        self.publisher = self

    def report_detection(self, camera_id, confidence, severity, vehicles, lanes, **kw):
        import uuid

        ev = type("E", (), {"event_id": uuid.uuid4()})()
        self.sent.append((camera_id, confidence, severity, vehicles, kw["clip_path"].is_file()))
        return ev

    def run_until_empty(self):
        return True

    def close(self):
        pass


def fake_detector(score):
    box = {"xmin": 10, "ymin": 10, "xmax": 120, "ymax": 90}
    return AccidentDetector(lambda f: [{"label": "accident", "score": score, "box": box}] if score else [], lambda f: [])


def wait_for(client, job_id, until=("done", "failed"), seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        j = client.get(f"/v1/jobs/{job_id}").json()
        if j["state"] in until:
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


@needs_ffmpeg
def test_the_detector_node_checks_a_video_cuts_the_evidence_and_publishes_the_alarm(tmp_path):
    ds = tmp_path / "ds"
    write_catalog(ds, [entry("a1", "CHN003")])
    make_clip(ds / "clips" / "a1.mp4")
    edge = FakeEdge()
    app = create_service(catalog_mod.load(ds, tmp_path), lambda: fake_detector(0.9), lambda: edge, tmp_path / "tmp", warm=False)
    with TestClient(app) as c:
        job = c.post("/v1/analyze", json={"video_id": "a1"})
        assert job.status_code == 202
        done = wait_for(c, job.json()["job_id"])
    assert done["state"] == "done", done
    assert done["verdict"]["accident"] and done["verdict"]["frames_confirmed"] >= 2
    assert done["frames_done"] == done["frames_total"] == len(done["frame_scores"]) == 8
    assert done["published"]["camera_id"] == "CHN003" and done["published"]["sent"]
    assert edge.sent == [("CHN003", 0.9, edge.sent[0][2], edge.sent[0][3], True)]        # the evidence clip existed when it was sent


@needs_ffmpeg
def test_a_quiet_video_publishes_nothing_and_check_only_never_publishes(tmp_path):
    ds = tmp_path / "ds"
    write_catalog(ds, [entry("g1", "CHN003", scenario="good", file="clips/g1.mp4"), entry("a1", "CHN003", file="clips/a1.mp4")])
    make_clip(ds / "clips" / "g1.mp4")
    make_clip(ds / "clips" / "a1.mp4")
    edge = FakeEdge()
    quiet = create_service(catalog_mod.load(ds, tmp_path), lambda: fake_detector(0.0), lambda: edge, tmp_path / "tmp", warm=False)
    with TestClient(quiet) as c:
        j = wait_for(c, c.post("/v1/analyze", json={"video_id": "g1"}).json()["job_id"])
    assert j["verdict"]["accident"] is False and j["published"] is None and edge.sent == []

    loud = create_service(catalog_mod.load(ds, tmp_path), lambda: fake_detector(0.9), lambda: edge, tmp_path / "tmp", warm=False)
    with TestClient(loud) as c:
        j = wait_for(c, c.post("/v1/analyze", json={"video_id": "a1", "publish": False}).json()["job_id"])
    assert j["verdict"]["accident"] is True and j["published"] is None and edge.sent == []


def test_the_detector_node_refuses_unknown_videos_and_survives_a_broken_one(tmp_path):
    ds = tmp_path / "ds"
    write_catalog(ds, [entry("a1", "CHN003")])
    (ds / "clips" / "a1.mp4").write_bytes(b"not a video")
    app = create_service(catalog_mod.load(ds, tmp_path), lambda: fake_detector(0.9), lambda: FakeEdge(), tmp_path / "tmp", warm=False)
    with TestClient(app) as c:
        assert c.post("/v1/analyze", json={"video_id": "nope"}).status_code == 404
        j = wait_for(c, c.post("/v1/analyze", json={"video_id": "a1"}).json()["job_id"])
        assert j["state"] == "failed" and j["error"]
        assert c.get("/health").json()["ok"] is True                                   # the worker is still alive


@needs_ffmpeg
def test_the_job_says_when_it_is_loading_the_models_and_when_loading_fails(tmp_path):
    ds = tmp_path / "ds"
    write_catalog(ds, [entry("a1", "CHN003")])
    make_clip(ds / "clips" / "a1.mp4")

    def broken():
        raise OSError("no internet")

    app = create_service(catalog_mod.load(ds, tmp_path), broken, lambda: FakeEdge(), tmp_path / "tmp", warm=False)
    with TestClient(app) as c:
        j = wait_for(c, c.post("/v1/analyze", json={"video_id": "a1"}).json()["job_id"])
        assert j["state"] == "failed" and "no internet" in j["error"]
        assert j["model"]["state"] == "failed" and "no internet" in j["model"]["error"]
        h = c.get("/health").json()
        assert h["model_loaded"] is False and h["model_state"] == "failed"
