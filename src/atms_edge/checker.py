"""Video checker. Runs the Hugging Face detector over clips and, when asked, raises the alarm.

A clip is a video file, or a folder of numbered frames from one camera (the Kaggle CCTV set ships that way).
Folders named "Accident" and "Non Accident" give the expected answer, so a run also reports how often the
models agree with the labels. With --publish each confirmed accident goes out over MQTT exactly as a camera
would send it, and the core platform does the rest: incident, nearest facilities, agency dispatch and notices.
"""
from __future__ import annotations

import itertools
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .detector import Detection

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
FRAME_SUFFIXES = {".jpg", ".jpeg", ".png"}
_FRAME = re.compile(r"^(?P<clip>.*?)[_ ]*\(?(?P<n>\d+)\)?$")


@dataclass
class Clip:
    name: str
    frames: list[Path] = field(default_factory=list)
    video: Path | None = None
    expected: bool | None = None      # True accident, False normal, None unknown


@dataclass
class Verdict:
    clip: Clip
    detection: Detection | None
    scores: list[float]

    @property
    def accident(self) -> bool:
        return self.detection is not None

    @property
    def agrees(self) -> bool | None:
        return None if self.clip.expected is None else self.clip.expected == self.accident


def _expected(path: Path) -> bool | None:
    parts = [p.lower() for p in path.parts]
    if any(p in ("non accident", "non_accident", "normal") for p in parts):
        return False
    if any(p == "accident" for p in parts):
        return True
    return None


def _frame_key(path: Path) -> tuple[str, int]:
    m = _FRAME.match(path.stem)
    return (m.group("clip") or "clip", int(m.group("n"))) if m else (path.stem, 0)


def discover(root: str | Path) -> list[Clip]:
    """Videos and frame folders under root. Frames named test28_18.jpg and acc1 (4).jpg group by clip."""
    root = Path(root)
    clips: list[Clip] = []
    if root.is_file() and root.suffix.lower() in VIDEO_SUFFIXES:
        return [Clip(root.stem, video=root, expected=_expected(root))]
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in VIDEO_SUFFIXES:
            clips.append(Clip(p.stem, video=p, expected=_expected(p)))
    groups: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in FRAME_SUFFIXES:
            clip, n = _frame_key(p)
            groups.setdefault((p.parent, clip), []).append((n, p))
    for (folder, clip), items in sorted(groups.items()):
        items.sort()
        clips.append(Clip(f"{folder.parent.name}/{folder.name}/{clip}", [p for _, p in items], expected=_expected(folder)))
    return clips


def load_frames(clip: Clip, count: int = 5) -> list:
    from PIL import Image

    if clip.video:
        from .detector import sample_frames
        return sample_frames(clip.video, count)
    paths = clip.frames
    if len(paths) > count:
        paths = [paths[int((i + 0.5) * len(paths) / count)] for i in range(count)]
    return [Image.open(p).convert("RGB") for p in paths]


def check(detector, clip: Clip, count: int = 5) -> Verdict:
    frames = load_frames(clip, count)
    detection, scores = detector.scan(frames, min(detector.min_confirmed_frames, len(frames)) or 1)
    return Verdict(clip, detection, scores)


def to_mp4(clip: Clip, out: str | Path, seconds_per_frame: float = 1.0) -> Path:
    """A browser-playable clip for the controller. Frame folders are stitched; videos are re-encoded."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    common = ["-vf", "scale=960:-2,format=yuv420p", "-c:v", "libx264", "-profile:v", "baseline", "-movflags", "+faststart"]
    if clip.video:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip.video), *common, str(out)]
    else:
        listing = out.with_suffix(".txt")
        lines = itertools.chain.from_iterable((f"file '{p.resolve()}'", f"duration {seconds_per_frame}") for p in clip.frames)
        listing.write_text("\n".join(lines) + f"\nfile '{clip.frames[-1].resolve()}'\n")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
               "-r", "10", *common, str(out)]
    subprocess.run(cmd, check=True)
    if not clip.video:
        listing.unlink()
    return out


def summary(verdicts: list[Verdict]) -> dict:
    """Confusion counts against the folder labels."""
    lab = [v for v in verdicts if v.clip.expected is not None]
    tp = sum(1 for v in lab if v.clip.expected and v.accident)
    fp = sum(1 for v in lab if not v.clip.expected and v.accident)
    fn = sum(1 for v in lab if v.clip.expected and not v.accident)
    tn = sum(1 for v in lab if not v.clip.expected and not v.accident)
    return {"clips": len(verdicts), "labelled": len(lab), "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "true_negative": tn,
            "accuracy": round((tp + tn) / len(lab), 3) if lab else None,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None}
