"""python -m atms_edge.cli check videos/archive/data/val --limit 12 [--publish --cameras GNT001,GNT003]
python -m atms_edge.cli analyze --camera GNT001 frame1.png frame2.png [--publish]
python -m atms_edge.cli analyze --camera GNT001 --video clip.mp4 --publish
"""
from __future__ import annotations

import argparse
import sys

from .detector import AccidentDetector, sample_frames


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atms_edge")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="run the Hugging Face detector on frames or a clip")
    a.add_argument("images", nargs="*", help="image files, a few seconds apart")
    a.add_argument("--video", help="a clip to sample frames from")
    a.add_argument("--frames", type=int, default=5)
    a.add_argument("--camera", default="GNT001")
    a.add_argument("--min-frames", type=int, default=2, help="frames that must show the accident")
    a.add_argument("--publish", action="store_true", help="queue and publish the detection over MQTT")
    a.add_argument("--host", default="localhost")
    a.add_argument("--port", type=int, default=1883)
    a.add_argument("--user", default="atms")
    a.add_argument("--password", default="atms-dev")
    c = sub.add_parser("check", help="check clips or frame folders with the AI models and optionally raise the alarm")
    c.add_argument("path", help="a video, or a folder of videos / numbered frames (Accident and Non Accident subfolders are used as labels)")
    c.add_argument("--limit", type=int, default=0, help="check at most this many clips, balanced across labels")
    c.add_argument("--frames", type=int, default=5, help="frames sampled per clip")
    c.add_argument("--cameras", default="GNT001", help="camera ids to place detections at, comma separated, used in turn")
    c.add_argument("--publish", action="store_true", help="send each confirmed accident over MQTT as a camera would")
    c.add_argument("--api", default="http://localhost:8000", help="core API, queried after publishing to show what happened")
    c.add_argument("--verify", action="store_true", help="act as the operator and verify each new incident through the API")
    c.add_argument("--watch", type=int, default=0, help="keep narrating what the platform does for this many seconds")
    c.add_argument("--token", default="ops-dev")
    c.add_argument("--host", default="localhost")
    c.add_argument("--port", type=int, default=1883)
    c.add_argument("--user", default="atms")
    c.add_argument("--password", default="atms-dev")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return run_check(args)

    from PIL import Image

    frames = sample_frames(args.video, args.frames) if args.video else [Image.open(i).convert("RGB") for i in args.images]
    if not frames:
        p.error("give image files or --video")
    det = AccidentDetector.from_huggingface(min_confirmed_frames=args.min_frames).analyze(frames)
    if det is None:
        print(f"no accident confirmed in {len(frames)} frames")
        return 0
    print(f"accident confidence {det.confidence} severity {det.severity.value} vehicles {det.vehicles} "
          f"lanes {','.join(det.lanes)} confirmed in {det.frames_confirmed}/{det.frames_sampled} frames")
    if not args.publish:
        return 0

    from atms_sim.edge import EdgeController
    from atms_sim.outbox import Outbox
    from atms_sim.publishers import MqttPublisher

    pub = MqttPublisher(args.host, args.port, args.user, args.password, client_id=f"edge-{args.camera}")
    edge = EdgeController(Outbox("simulator-out/outbox.sqlite3"), pub)
    event = edge.report_detection(args.camera, det.confidence, det.severity, det.vehicles,
                                  list(det.lanes), model_version=det.model_version, clip_path=args.video)
    ok = edge.run_until_empty()
    pub.close()
    print(f"{'published' if ok else 'still in outbox'} {event.type} {event.event_id}")
    return 0 if ok else 1


def run_check(args) -> int:
    import itertools
    import time
    from pathlib import Path

    from atms_contracts.cameras import CAMERAS
    from atms_contracts.models import Severity

    from . import checker

    clips = checker.discover(args.path)
    if args.limit:
        yes = [c for c in clips if c.expected is True]
        no = [c for c in clips if c.expected is False]
        other = [c for c in clips if c.expected is None]
        half = max(1, args.limit // 2)
        clips = (yes[:half] + no[:half] + other)[:args.limit] if (yes and no) else clips[:args.limit]
    if not clips:
        print(f"no videos or frames found under {args.path}")
        return 1
    cams = [c.strip() for c in args.cameras.split(",") if c.strip()]
    unknown = [c for c in cams if c not in CAMERAS]
    if unknown:
        print(f"unknown camera {','.join(unknown)}; known: {','.join(CAMERAS)}")
        return 2

    det = AccidentDetector.from_huggingface()
    verdicts = []
    for clip in clips:
        v = checker.check(det, clip, args.frames)
        verdicts.append(v)
        d = v.detection
        mark = "" if v.agrees is None else ("  ok" if v.agrees else "  DISAGREES with label")
        detail = f"{d.severity.value}, {d.vehicles} vehicle(s), lanes {','.join(d.lanes)}, confidence {d.confidence}" if d else "no accident"
        print(f"{clip.name:<34} {'ACCIDENT' if d else 'normal  '} {detail}  frames {v.scores}{mark}")
    s = checker.summary(verdicts)
    print(f"\nchecked {s['clips']} clips, accuracy {s['accuracy']}, precision {s['precision']}, recall {s['recall']} "
          f"(tp {s['true_positive']} fp {s['false_positive']} fn {s['false_negative']} tn {s['true_negative']})")
    if not args.publish:
        return 0

    from atms_sim.edge import EdgeController, LocalClipStore
    from atms_sim.outbox import Outbox
    from atms_sim.publishers import MqttPublisher

    pub = MqttPublisher(args.host, args.port, args.user, args.password, client_id="edge-checker")
    edge = EdgeController(Outbox("simulator-out/outbox.sqlite3"), pub, clips=LocalClipStore("simulator-out/clips"))
    camera = itertools.cycle(cams)
    sent = []
    for v in verdicts:
        if not v.detection:
            continue
        cam = next(camera)
        mp4 = checker.to_mp4(v.clip, Path("simulator-out/tmp") / f"{cam}-{abs(hash(v.clip.name)) % 10**8}.mp4")
        d = v.detection
        event = edge.report_detection(cam, d.confidence, d.severity, d.vehicles, list(d.lanes), clip_seconds=len(v.clip.frames) or 10.0,
                                      model_version=d.model_version, clip_path=mp4)
        sent.append((cam, v.clip.name, event.event_id))
        mp4.unlink(missing_ok=True)
    ok = edge.run_until_empty()
    pub.close()
    for cam, name, eid in sent:
        print(f"published accident from {cam} for {name} ({eid})")
    if not ok:
        print("some events are still in the outbox; is RabbitMQ up?")
        return 1
    if sent:
        narrate(args, [str(e) for _, _, e in sent])
    return 0


def narrate(args, event_ids) -> None:
    """Follow the platform for a while and print each thing it does, once."""
    import time

    import httpx

    seen, verified = set(), set()
    deadline = time.monotonic() + max(args.watch, 6)
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {args.token}"}, timeout=10) as api:
        def say(line):
            if line not in seen:
                seen.add(line)
                print(f"  {time.strftime('%H:%M:%S')}  {line}")

        print("\nWhat the platform does")
        while time.monotonic() < deadline:
            try:
                items = api.get("/v1/map").json()["incidents"]
            except Exception as e:  # noqa: BLE001
                print(f"could not read {args.api}/v1/map ({e})")
                return
            for i in items:
                ref = i["id"][:8]
                say(f"incident {ref} opened, {i['severity']} on {i['segment']}, seen by {','.join(i['cameras'])}, confidence {i['confidence']}")
                if i["state"] == "detected" and args.verify and i["id"] not in verified:
                    verified.add(i["id"])
                    api.post(f"/v1/incidents/{i['id']}/verify", headers={"Idempotency-Key": f"check-{i['id']}"})
                    say(f"operator verified {ref}")
                if i["state"] != "detected":
                    say(f"incident {ref} is {i['state'].replace('_', ' ')}")
                for a in i["assignments"]:
                    eta = f", {round(a['eta_s'] / 60)} min away" if a["eta_s"] else ""
                    say(f"{ref} {a['agency']} {a['state'].replace('_', ' ')}{(' from ' + a['facility']) if a['facility'] else ''}{eta}")
                for n in i["notices"]:
                    say(f"{ref} {n['channel']} to {n['contact']} {n['status']}")
            time.sleep(2)
    print(f"\nWatch the units on the map at {args.api}/console/")


if __name__ == "__main__":
    sys.exit(main())
