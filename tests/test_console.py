import re
from pathlib import Path

import httpx
import pytest

from atms_contracts.models import Agency
from atms_core.api import CONSOLE_DIR, Principal, create_api
from atms_core.map_view import build_map, road_pieces
from atms_forecast.obs_store import MemoryObservationStore
from atms_forecast.segments import SEGMENTS
from atms_forecast.service import ForecastService

from conftest import Rig, detection, run, video
from test_forecast import NOW, SEG, data, feed, incident_end, model, peak_end, split  # noqa: F401  (fixtures)

TOKENS = {"ops": Principal("op1"), "police": Principal("police-app", Agency.police), "sensor": Principal("s", None, "sensor")}
H = lambda t: {"Authorization": f"Bearer {t}"}


async def rig(forecast=None, media_dir="simulator-out"):
    r = Rig(forecast=forecast)
    app = create_api(r.system, TOKENS, "s", run_bus=False, media_dir=media_dir)
    r.system.bus.start()
    return r, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms")


def test_road_pieces_tile_the_road_without_gaps_or_overlap():
    pieces = road_pieces()
    ids = [s.id for s in SEGMENTS]
    for a, b in zip(ids, ids[1:]):
        assert pieces[a][1] == pytest.approx(pieces[b][0])          # one ends where the next begins
    for s in SEGMENTS:
        start, end = pieces[s.id]
        assert start[0] < s.lat < end[0]                            # the sensor point lies inside its piece


def test_map_needs_an_operator_and_lists_facilities_boards_and_segments_without_a_model():
    async def go():
        r, http = await rig()
        assert (await http.get("/v1/map")).status_code == 401
        assert (await http.get("/v1/map", headers=H("police"))).status_code == 403
        assert (await http.get("/v1/map", headers=H("sensor"))).status_code == 403
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        assert len(body["segments"]) == 6 and all(s["status"] == "no forecast" for s in body["segments"])
        assert body["forecast_model"] is None and body["incidents"] == []
        assert {f["agency"] for f in body["facilities"]} == {a.value for a in Agency}
        assert [v["id"] for v in body["vms"]] == ["VMS-NH48-01", "VMS-NH48-02", "VMS-NH48-03"]
        assert all(v["text"] is None for v in body["vms"])
    run(go())


def test_an_open_incident_shows_its_units_and_the_boards_it_changed_then_clears_when_resolved():
    async def go():
        r, http = await rig()
        await r.feed(detection(confidence=0.94), video())
        inc = r.incident()
        await r.system.settle()
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        [one] = body["incidents"]
        assert one["id"] == str(inc.id) and one["lat"] == pytest.approx(inc.location.lat)
        assert one["assignments"] and all(a["facility"] for a in one["assignments"] if a["unit_ref"])
        shown = [v for v in body["vms"] if v["text"]]
        assert shown and "ACCIDENT" in shown[0]["text"]
        busy = {f["id"] for f in body["facilities"] if f["busy_with"]}
        assert busy == {a["facility"] for a in one["assignments"] if a["facility"]}

        r.system.service.reject(inc.id, "test cleanup", "op1")
        await r.system.settle()
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        assert body["incidents"] == [] and all(v["text"] is None for v in body["vms"])
    run(go())


def test_with_a_model_each_segment_carries_now_and_forecast_levels(model, data):
    async def go():
        svc = ForecastService(MemoryObservationStore(), model, "test-v1", clock=lambda: NOW)
        feed(svc.store, data, NOW, source_end=peak_end(data))
        r, http = await rig(svc)
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        assert body["forecast_model"] == "test-v1"
        for s in body["segments"]:
            assert s["status"] == "ok" and s["speed_kmh"] > 0 and s["now_level"] in ("free", "moderate", "heavy", "severe")
            assert [f["horizon_min"] for f in s["forecasts"]] == [15, 30, 60]
    run(go())


def test_with_old_readings_the_road_is_marked_as_having_no_recent_data(model):
    async def go():
        svc = ForecastService(MemoryObservationStore(), model, "test-v1", clock=lambda: NOW)
        r, http = await rig(svc)
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        assert {s["status"] for s in body["segments"]} == {"no recent data"} and all(s["speed_kmh"] is None for s in body["segments"])
    run(go())


def test_the_console_files_are_served_and_the_root_goes_there():
    async def go():
        r, http = await rig()
        page = await http.get("/console/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        for name in re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', page.text):
            res = await http.get(f"/console/{name}")
            assert res.status_code == 200, name
        assert (await http.get("/console/controller.html")).status_code == 200
        for name in ("api.js", "map.js", "panel.js", "app.js", "dom.js", "auth.js", "controller.js", "controller.css"):
            assert (await http.get(f"/console/{name}")).status_code == 200
        root = await http.get("/", follow_redirects=False)
        assert root.status_code in (302, 307) and root.headers["location"] == "/console/"
        assert (await http.get("/console/../main.py")).status_code in (404, 400)
    run(go())


def test_the_console_keeps_html_css_and_js_apart_and_has_no_emoji_or_secrets():
    files = sorted(CONSOLE_DIR.iterdir())
    assert {f.name for f in files} >= {"index.html", "controller.html", "style.css", "controller.css", "api.js", "map.js", "panel.js", "app.js", "dom.js", "auth.js", "controller.js"}
    for page in ("index.html", "controller.html"):
        html = (CONSOLE_DIR / page).read_text()
        assert "<style" not in html and not re.search(r"<script(?![^>]*src=)", html) and " onclick=" not in html
    emoji = re.compile("[\U0001F000-\U0001FFFF☀-➿⬀-⯿️]")
    for f in files:
        text = f.read_text()
        assert not emoji.search(text), f.name
        assert "ops-dev" not in text and "sensor-dev" not in text, f.name        # no development token baked in
        assert "innerHTML" not in text, f.name                                   # incident text is never parsed as markup


def test_the_live_stream_works_over_a_real_socket_and_refuses_bad_tokens(free_tcp_port):
    """Through ASGI alone this passes without a websocket library installed. A real server needs one."""
    import threading
    import time

    import uvicorn
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    r = Rig()
    app = create_api(r.system, TOKENS, "s", run_bus=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_tcp_port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with connect(f"ws://127.0.0.1:{free_tcp_port}/v1/stream?token=ops", open_timeout=5) as ws:
            assert len(app.state.gateway._clients) == 1       # connected and registered for pushes
        for who in ("police", "nobody"):                         # refused before the handshake completes
            with pytest.raises(InvalidStatus) as refused:
                connect(f"ws://127.0.0.1:{free_tcp_port}/v1/stream?token={who}", open_timeout=5)
            assert refused.value.response.status_code == 403
    finally:
        server.should_exit = True
        thread.join(5)


def link(path, exp=None, secret="s"):
    from time import time

    from atms_core.api import sign

    exp = exp or int(time()) + 300
    return f"{path}?exp={exp}&sig={sign(path, exp, secret)}"


def test_clips_are_served_only_through_a_valid_unexpired_link_and_support_seeking(tmp_path):
    async def go():
        (tmp_path / "clips").mkdir()
        (tmp_path / "clips" / "GNT001.mp4").write_bytes(bytes(range(256)) * 8)
        (tmp_path / "secret.txt").write_text("not media")
        r, http = await rig(media_dir=tmp_path / "clips" / "..")
        ok = await http.get(link("/media/clips/GNT001.mp4"))
        assert ok.status_code == 200 and ok.headers["content-type"] == "video/mp4" and len(ok.content) == 2048
        assert ok.headers["cache-control"] == "private, no-store"
        part = await http.get(link("/media/clips/GNT001.mp4"), headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == bytes(range(10, 20))        # a video can seek
        assert (await http.get("/media/clips/GNT001.mp4")).status_code == 422             # no signature at all
        assert (await http.get("/media/clips/GNT001.mp4?exp=9999999999&sig=bad")).status_code == 403
        assert (await http.get(link("/media/clips/GNT001.mp4", exp=1))).status_code == 403          # expired
        assert (await http.get(link("/media/clips/GNT001.mp4", secret="other"))).status_code == 403  # signed by someone else
        assert (await http.get(link("/media/clips/gone.mp4"))).status_code == 404
        # a signature for one file does not open another, and the path cannot climb out of the media folder
        other = link("/media/clips/GNT001.mp4").replace("GNT001", "GNT002")
        assert (await http.get(other)).status_code in (403, 404)
        sneaky = link("/media/../secret.txt")
        assert (await http.get(sneaky)).status_code in (403, 404)
        assert b"not media" not in (await http.get(sneaky)).content
    run(go())


def test_review_gives_the_verdict_the_clips_and_only_the_actions_that_are_allowed(tmp_path):
    async def go():
        (tmp_path / "clips").mkdir()
        (tmp_path / "clips" / "GNT001.mp4").write_bytes(b"x" * 10)
        r, http = await rig(media_dir=tmp_path)
        assert (await http.get("/v1/incidents/00000000-0000-0000-0000-000000000000/review", headers=H("ops"))).status_code == 404

        await r.feed(detection(confidence=0.74, severity=detection().data.severity), video())
        inc = r.incident()
        assert (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("police"))).status_code == 403
        body = (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("ops"))).json()
        a = body["assessment"]
        assert a["outcome"] == "waiting_for_operator" and a["confidence"] == pytest.approx(0.74) and a["would_auto_verify"] is False
        assert a["auto_verify_confidence"] == 0.9 and a["review_confidence"] == 0.6 and a["cameras"] == ["GNT001"]
        [clip] = body["evidence"]
        assert clip["kind"] == "clip" and clip["available"] is True and clip["size_bytes"] == 10 and clip["camera_id"] == "GNT001"
        assert (await http.get(clip["url"])).status_code == 200                               # the link it hands out works
        assert body["allowed"] == {"verify": True, "reject": True, "resolve": False, "close": False}
        assert body["units"] == []

        (tmp_path / "clips" / "GNT001.mp4").unlink()
        gone = (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("ops"))).json()["evidence"][0]
        assert gone["available"] is False and gone["size_bytes"] is None                       # the page can say the file is missing

        assert (await http.post(f"/v1/incidents/{inc.id}/verify", headers=H("ops"))).status_code == 200
        await r.system.settle()
        after = (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("ops"))).json()
        assert after["assessment"]["outcome"] == "verified_by_operator" and after["assessment"]["verified_by"] == "operator"
        assert after["allowed"]["verify"] is False and after["allowed"]["reject"] is True
        assert after["units"] and all(set(u["can"]) <= {"en_route", "on_scene", "done"} for u in after["units"])
        acked = next(u for u in after["units"] if u["state"] == "acked")
        assert acked["can"] == ["en_route"]                  # the page can only offer the next step the service would accept
    run(go())


def test_review_says_when_the_detector_acted_alone_and_when_it_was_overruled():
    async def go():
        r, http = await rig()
        await r.feed(detection(confidence=0.95), video())
        inc = r.incident()
        a = (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("ops"))).json()["assessment"]
        assert a["outcome"] == "verified_automatically" and a["would_auto_verify"] is True
        await http.post(f"/v1/incidents/{inc.id}/reject", json={"reason": "stationary tow truck"}, headers=H("ops"))
        await r.system.settle()
        after = (await http.get(f"/v1/incidents/{inc.id}/review", headers=H("ops"))).json()
        assert after["assessment"]["outcome"] == "rejected" and after["assessment"]["rejected_reason"] == "stationary tow truck"
        assert after["allowed"] == {"verify": False, "reject": False, "resolve": False, "close": True}
    run(go())


def test_a_clip_made_for_the_controller_is_a_real_playable_video(tmp_path):
    import shutil
    import subprocess

    from atms_sim.edge import PlayableClipStore

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg not installed")
    ref, size, sha = PlayableClipStore(tmp_path).put("GNT001", "t", 2)
    path = tmp_path / Path(ref).name
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, check=True).stdout
    assert "h264" in probe and "640" in probe and size == path.stat().st_size and len(sha) == 64


def test_map_lists_cameras_and_the_alerts_sent_to_officials():
    from atms_contracts.cameras import CAMERAS

    async def go():
        r, http = await rig()
        await r.feed(detection(confidence=0.94), video())
        await r.system.settle()
        body = (await http.get("/v1/map", headers=H("ops"))).json()
        assert {c["id"] for c in body["cameras"]} == set(CAMERAS) and len(body["cameras"]) >= 6
        [one] = body["incidents"]
        assert one["cameras"] == ["GNT001"]
        assert one["notices"] and {n["channel"] for n in one["notices"]} <= {"sms", "webhook"}
        assert any(n["agency"] for n in one["notices"])                     # agency desks are tied to an agency for the map
        assert all("left_at" in a for a in one["assignments"])             # when each unit set off, so the map can move it
    run(go())
