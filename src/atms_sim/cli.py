"""python -m atms_sim.cli edge --scenario major_two_vehicle --host localhost
python -m atms_sim.cli agencies --port 9100
python -m atms_sim.cli list
"""
from __future__ import annotations

import argparse
import sys

from .scenarios import SCENARIOS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atms_sim")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show scenarios")

    e = sub.add_parser("edge", help="publish a scenario through a simulated edge controller")
    e.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    e.add_argument("--host", default="localhost")
    e.add_argument("--port", type=int, default=1883)
    e.add_argument("--user", default="atms")
    e.add_argument("--password", default="atms-dev")
    e.add_argument("--outbox", default="simulator-out/outbox.sqlite3")
    e.add_argument("--clips", default="simulator-out/clips")
    e.add_argument("--playable", action="store_true", help="write a short clip a browser can play (needs ffmpeg)")

    a = sub.add_parser("agencies", help="run the fake agency dispatch server")
    a.add_argument("--port", type=int, default=9100)
    a.add_argument("--seed", type=int, default=None)
    a.add_argument("--reliable", action="store_true", help="never decline or time out, for demos")

    g = sub.add_parser("sms", help="run the fake SMS gateway")
    g.add_argument("--port", type=int, default=9200)
    g.add_argument("--seed", type=int, default=None)
    g.add_argument("--receipt-secret", default="dev-receipt-secret")

    t = sub.add_parser("sensors", help="send a window of synthetic road readings to the API, ending now")
    t.add_argument("--api", default="http://localhost:8000")
    t.add_argument("--token", default="sensor-dev")
    t.add_argument("--minutes", type=int, default=180)
    t.add_argument("--seed", type=int, default=7)
    t.add_argument("--at", default="2026-07-27 17:35", help="local time of day to replay, 17:35 is the evening peak building up")
    t.add_argument("--incident", action="store_true", help="replay a stretch that contains an incident")

    args = p.parse_args(argv)

    if args.cmd == "list":
        for s in SCENARIOS.values():
            print(f"{s.name:26} {s.camera_id}  conf {s.confidence:.2f}  {s.severity.value:8} {s.description}")
        return 0

    if args.cmd == "edge":
        from pathlib import Path

        from .edge import EdgeController, LocalClipStore
        from .outbox import Outbox
        from .publishers import MqttPublisher

        Path(args.outbox).parent.mkdir(parents=True, exist_ok=True)
        pub = MqttPublisher(args.host, args.port, args.user, args.password,
                            client_id=f"edge-{SCENARIOS[args.scenario].camera_id}")
        scenario = SCENARIOS[args.scenario]
        if args.playable:
            from dataclasses import replace

            from .edge import PlayableClipStore

            scenario = replace(scenario, clip_seconds=12.0)
        edge = EdgeController(Outbox(args.outbox), pub, PlayableClipStore(args.clips) if args.playable else LocalClipStore(args.clips))
        event = edge.report(scenario)
        print(f"queued {event.type} {event.event_id}, outbox depth {edge.outbox.depth()}")
        ok = edge.run_until_empty()
        pub.close()
        print("published" if ok else f"broker unreachable, {edge.outbox.depth()} still in outbox")
        return 0 if ok else 1

    if args.cmd == "agencies":
        import uvicorn

        from .agencies import create_app

        from atms_contracts.models import Agency
        from .agencies import DEFAULT_PROFILES, Profile

        profiles = {a: Profile(0.0, 0.0, p.ack_delay_s, p.eta_s, p.timeout_sleep_s) for a, p in DEFAULT_PROFILES.items()} if args.reliable else None
        uvicorn.run(create_app(args.seed, profiles), host="0.0.0.0", port=args.port)
        return 0
    if args.cmd == "sms":
        import uvicorn

        from .sms import create_app as create_sms

        uvicorn.run(create_sms(args.seed, receipt_secret=args.receipt_secret), host="0.0.0.0", port=args.port)
        return 0
    if args.cmd == "sensors":
        import pandas as pd

        from .sensors import push
        from atms_forecast.synth import generate

        df = generate(args.seed, 70)
        end = pd.Timestamp(args.at, tz="Asia/Kolkata").tz_convert("UTC")
        if args.incident:
            hit = df[(df.incident_lanes_closed > 0) & (df.incident_age_min >= 20)]
            end = hit.ts.iloc[len(hit) // 2]
        print(push(args.api, args.token, df, end, args.minutes), "source moment", end)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
