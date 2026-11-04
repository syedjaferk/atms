"""Puts a few real incidents on the controller by running the AI on catalog videos through the running stack.

    python scripts/seed_demo.py --api http://localhost:8001          # needs only httpx

Each video goes through the same path as the Check button on the CCTV page, so the incidents are real ones. Videos the AI is very
sure about are dispatched by themselves. The others wait on the controller's Needs review tab for you to verify or reject.
The defaults are chosen from what the AI said when the catalog was built, in different cities.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

DEFAULTS = ["v5", "v4", "v18", "v2"]      # Chennai critical, Bengaluru major, Gurugram minor, Pune minor


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--token", default="ops-dev")
    p.add_argument("videos", nargs="*", default=DEFAULTS, help="catalog video ids")
    args = p.parse_args()
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {args.token}"}, timeout=15) as api:
        for vid in args.videos:
            r = api.post(f"/v1/cctv/videos/{vid}/analyze", json={"publish": True})
            if r.status_code >= 400:
                print(f"{vid}: refused ({r.status_code} {r.text[:120]})")
                continue
            job = r.json()["job_id"]
            while True:
                j = api.get(f"/v1/cctv/jobs/{job}").json()
                if j["state"] in ("done", "failed"):
                    break
                time.sleep(2)
            v = j.get("verdict") or {}
            if j["state"] == "failed":
                print(f"{vid}: failed, {j['error']}")
            elif v.get("accident"):
                print(f"{vid}: accident at {j['camera_id']}, {v['severity']}, {round(v['confidence'] * 100)} percent, alarm sent")
            else:
                print(f"{vid}: AI saw no accident, nothing sent")
        time.sleep(5)
        for i in api.get("/v1/map").json()["incidents"]:
            print(f"incident {i['id'][:8]} {i['severity']} {i['state']} cameras {','.join(i['cameras'])} units {len(i['assignments'])}")
    print("Open the console in your browser and choose Controller.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
