"""Build the video catalog, dataset/videos.json.

Two kinds of video go in.
  Labelled clips   stitched from the Accident and Non Accident frame folders of the Kaggle CCTV set under videos/archive.
                   These are ground truth, someone looked at them.
  Your own videos  every mp4 under videos/. Nobody has labelled them, so they are sorted by what the AI said about them
                   (dataset/model_labels.json from `python -m atms_edge.cli check videos --only videos --json ...`) and marked
                   as model labelled. Edit dataset/labels.csv (id,truth) to correct any of them, the catalog then shows ground truth.

Each video is then given to a camera, a few of each scenario per camera, spread over every area.

    python scripts/build_dataset.py            # needs ffmpeg and ffprobe
"""
from __future__ import annotations

import csv
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from atms_contracts import network  # noqa: E402
from atms_edge import checker  # noqa: E402

DATASET, VIDEOS = ROOT / "dataset", ROOT / "videos"
CLIPS_PER_LABEL = 24            # stitched clips per scenario, enough for every camera to have one of each


def probe(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return round(float(out), 1) if out else 0.0


def stitch(limit: int) -> list[tuple[str, checker.Clip, Path]]:
    """Labelled clips from the frame folders. Longest sequences first, so the clips have some length to them."""
    clips = [c for c in checker.discover(VIDEOS / "archive" / "data", only="frames") if len(c.frames) >= 4 and c.expected is not None]
    out = []
    for label, tag in ((True, "acc"), (False, "ok")):
        mine = sorted((c for c in clips if c.expected is label), key=lambda c: (-len(c.frames), c.name))[:limit]
        for i, c in enumerate(mine, 1):
            dest = DATASET / "clips" / f"{tag}-{i:02d}.mp4"
            if not dest.exists():
                checker.to_mp4(c, dest, seconds_per_frame=1.0)
            out.append((f"{tag}-{i:02d}", c, dest))
    return out


def main() -> int:
    net = network.load()
    cams = sorted(net.cameras)
    rng = random.Random(7)
    DATASET.mkdir(exist_ok=True)
    entries: list[dict] = []

    for vid, clip, dest in stitch(CLIPS_PER_LABEL):
        acc = clip.expected
        entries.append(dict(id=vid, title=("Collision" if acc else "Free flowing traffic") + f", {len(clip.frames)} frames",
                            scenario="bad" if acc else "good", truth="accident" if acc else "normal", label_source="ground truth",
                            root="dataset", file=f"clips/{dest.name}", duration_s=probe(dest), size_bytes=dest.stat().st_size))

    model = {Path(m["file"]).stem: m for m in json.loads((DATASET / "model_labels.json").read_text())} if (DATASET / "model_labels.json").is_file() else {}
    fixed = {}
    if (DATASET / "labels.csv").is_file():
        fixed = {r["id"]: r["truth"] for r in csv.DictReader((DATASET / "labels.csv").open()) if r.get("truth")}
    for mp4 in sorted(VIDEOS.glob("*.mp4"), key=lambda p: (len(p.stem), p.stem)):
        m = model.get(mp4.stem, {})
        if not m and mp4.stem not in fixed:
            continue                                  # nobody and no model has looked at it yet, so it cannot be sorted
        if mp4.stem in fixed:
            truth, source = fixed[mp4.stem], "ground truth"
        else:
            truth, source = "unlabelled", "model"
        bad = (truth == "accident") if truth != "unlabelled" else bool(m.get("accident"))
        entries.append(dict(id=mp4.stem, title=f"Recording {mp4.stem}", scenario="bad" if bad else "good", truth=truth,
                            label_source=source, root="videos", file=mp4.name, duration_s=probe(mp4), size_bytes=mp4.stat().st_size,
                            model={k: m[k] for k in ("accident", "confidence", "severity", "vehicles") if k in m}))

    # Give every camera the same mix. Round robin over the cameras, bad and good dealt separately so each camera gets some of both.
    for scenario in ("bad", "good"):
        pool = [e for e in entries if e["scenario"] == scenario]
        rng.shuffle(pool)
        for i, e in enumerate(pool):
            e["camera_id"] = cams[i % len(cams)]
    entries.sort(key=lambda e: (e["camera_id"], e["scenario"], e["id"]))
    (DATASET / "videos.json").write_text(json.dumps({"version": 1, "videos": entries}, indent=1))

    per = {}
    for e in entries:
        per.setdefault(e["camera_id"], {"good": 0, "bad": 0})[e["scenario"]] += 1
    print(f"{len(entries)} videos over {len(per)} cameras")
    for c in cams:
        print(f"  {c} {net.cameras[c].name:<24} good {per.get(c, {}).get('good', 0)} bad {per.get(c, {}).get('bad', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
