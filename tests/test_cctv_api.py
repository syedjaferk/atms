import json
import time

import httpx

from atms_contracts import catalog as catalog_mod, network
from atms_contracts.models import Agency
from atms_core.api import Principal, create_api

from conftest import Rig, run

TOKENS = {"ops": Principal("op1"), "police": Principal("police-app", Agency.police)}
H = lambda t: {"Authorization": f"Bearer {t}"}


def setup(tmp_path, detector_url=None):
    ds = tmp_path / "ds"
    (ds / "clips").mkdir(parents=True, exist_ok=True)
    (ds / "clips" / "a1.mp4").write_bytes(b"0123456789")
    (ds / "videos.json").write_text(json.dumps({"version": 1, "videos": [
        dict(id="a1", camera_id="CHN001", title="Collision", scenario="bad", truth="accident", label_source="ground truth",
             root="dataset", file="clips/a1.mp4", duration_s=3.0, size_bytes=10)]}))
    r = Rig()
    app = create_api(r.system, TOKENS, "s", run_bus=False, catalog=catalog_mod.load(ds, tmp_path), detector_url=detector_url)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms")


def test_overview_lists_every_area_with_video_counts_and_needs_an_operator(tmp_path):
    async def go():
        http = setup(tmp_path)
        assert (await http.get("/v1/cctv")).status_code == 401
        assert (await http.get("/v1/cctv", headers=H("police"))).status_code == 403
        body = (await http.get("/v1/cctv", headers=H("ops"))).json()
        assert {a["id"] for a in body["areas"]} == set(network.load().areas)
        chn1 = next(c for a in body["areas"] for c in a["cameras"] if c["id"] == "CHN001")
        assert (chn1["good"], chn1["bad"]) == (0, 1)
        assert body["detector"]["up"] is False                                 # none configured
        assert (await http.get("/v1/cctv/cameras/NOPE/videos", headers=H("ops"))).status_code == 404
    run(go())


def test_a_video_plays_only_through_its_signed_link(tmp_path):
    async def go():
        http = setup(tmp_path)
        [v] = (await http.get("/v1/cctv/cameras/CHN001/videos", headers=H("ops"))).json()["videos"]
        assert v["play_url"].startswith("http://atms/v1/cctv/media/dataset/clips/a1.mp4?exp=")
        ok = await http.get(v["play_url"].replace("http://atms", ""))
        assert ok.status_code == 200 and ok.content == b"0123456789"
        assert (await http.get("/v1/cctv/media/dataset/clips/a1.mp4?exp=1&sig=bad")).status_code == 403
        old = int(time.time()) - 5
        from atms_core.api import sign
        path = "/v1/cctv/media/dataset/clips/a1.mp4"
        assert (await http.get(f"{path}?exp={old}&sig={sign(path, old, 's')}")).status_code == 403
        up = int(time.time()) + 60
        other = "/v1/cctv/media/dataset/clips/not-in-the-catalog.mp4"          # a valid signature still only opens catalogued files
        assert (await http.get(f"{other}?exp={up}&sig={sign(other, up, 's')}")).status_code == 404
    run(go())


def test_analyze_says_so_when_there_is_no_detector_node(tmp_path):
    async def go():
        http = setup(tmp_path)
        r = await http.post("/v1/cctv/videos/a1/analyze", headers=H("ops"), json={})
        assert r.status_code == 503
        assert (await http.post("/v1/cctv/videos/zzz/analyze", headers=H("ops"), json={})).status_code == 404
        down = setup(tmp_path, "http://127.0.0.1:9")                            # nothing listens there
        assert (await down.post("/v1/cctv/videos/a1/analyze", headers=H("ops"), json={})).status_code == 503
        assert (await down.get("/v1/cctv", headers=H("ops"))).json()["detector"]["up"] is False
    run(go())
