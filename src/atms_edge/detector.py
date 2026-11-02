"""Accident detection with Hugging Face models, in two stages.

Stage 1  hilmantm/detr-traffic-accident-detection finds accident regions in a frame.
Stage 2  PekingU/rtdetr_v2_r18vd (COCO) counts the vehicles around that region.

An accident only counts when it is seen in several sampled frames, which removes most
single-frame false positives. Severity and affected lanes are rules on top of the model
output, not something the models predict, and are meant to be tuned per camera.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from atms_contracts.models import Severity

ACCIDENT_MODEL = "hilmantm/detr-traffic-accident-detection"
VEHICLE_MODEL = "PekingU/rtdetr_v2_r18vd"
VEHICLE_LABELS = {"car", "motorcycle", "motorbike", "bus", "truck"}

# pipeline("object-detection") output: [{"score": float, "label": str, "box": {xmin,ymin,xmax,ymax}}]
DetectFn = Callable[[object], list[dict]]
Box = tuple[float, float, float, float]  # pixels: xmin, ymin, xmax, ymax

DEFAULT_LANES: dict[str, tuple[float, float]] = {
    "LANE_1": (0.0, 1 / 3), "LANE_2": (1 / 3, 2 / 3), "LANE_3": (2 / 3, 1.0),
}  # normalised x ranges across the frame, tune per camera


@dataclass(frozen=True)
class Detection:
    confidence: float
    severity: Severity
    vehicles: int
    lanes: tuple[str, ...]
    box: Box                 # normalised 0..1 on the best frame
    frames_confirmed: int
    frames_sampled: int
    model_version: str


def _box(d: dict) -> Box:
    b = d["box"]
    return (b["xmin"], b["ymin"], b["xmax"], b["ymax"])


def _intersects(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _expand(b: Box, factor: float) -> Box:
    cx, cy, w, h = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, (b[2] - b[0]) * factor, (b[3] - b[1]) * factor
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def classify_severity(vehicles: int, area_fraction: float) -> Severity:
    if vehicles >= 5 or area_fraction >= 0.25:
        return Severity.critical
    if vehicles >= 2:
        return Severity.major
    return Severity.minor


class AccidentDetector:
    def __init__(self, accident_fn: DetectFn, vehicle_fn: DetectFn,
                 lanes: dict[str, tuple[float, float]] | None = None,
                 accident_threshold: float = 0.5, vehicle_threshold: float = 0.5,
                 min_confirmed_frames: int = 2, model_version: str = f"{ACCIDENT_MODEL}+{VEHICLE_MODEL}"):
        self.accident_fn, self.vehicle_fn = accident_fn, vehicle_fn
        self.lanes = lanes or DEFAULT_LANES
        self.accident_threshold = accident_threshold
        self.vehicle_threshold = vehicle_threshold
        self.min_confirmed_frames = min_confirmed_frames
        self.model_version = model_version

    @classmethod
    def from_huggingface(cls, accident_model: str = ACCIDENT_MODEL, vehicle_model: str = VEHICLE_MODEL,
                         device: int = -1, **kwargs) -> "AccidentDetector":
        """Downloads the models on first use (cached under ~/.cache/huggingface). device -1 is CPU."""
        from transformers import pipeline

        acc = pipeline("object-detection", model=accident_model, device=device)
        veh = pipeline("object-detection", model=vehicle_model, device=device)
        return cls(acc, veh, model_version=f"{accident_model}+{vehicle_model}", **kwargs)

    def analyze(self, frames: Sequence) -> Detection | None:
        """Frames are PIL images from one camera, a few seconds apart. None means no accident."""
        return self.scan(frames)[0]

    def scan(self, frames: Sequence, min_frames: int | None = None) -> tuple[Detection | None, list[float]]:
        """Like analyze, plus the best accident score seen in each frame (0.0 when none).
        min_frames overrides the confirmation rule, so a clip with a single frame can still be judged."""
        hits: list[tuple[float, Box, object]] = []
        scores: list[float] = []
        for frame in frames:
            found = [d for d in self.accident_fn(frame) if d["label"] == "accident"]
            scores.append(round(max((d["score"] for d in found), default=0.0), 4))
            found = [d for d in found if d["score"] >= self.accident_threshold]
            if found:
                best = max(found, key=lambda d: d["score"])
                hits.append((best["score"], _box(best), frame))
        if len(hits) < (min_frames or self.min_confirmed_frames):
            return None, scores

        score, box, frame = max(hits, key=lambda h: h[0])
        width, height = frame.size
        vehicles = [d for d in self.vehicle_fn(frame)
                    if d["label"] in VEHICLE_LABELS and d["score"] >= self.vehicle_threshold]
        near = _expand(box, 1.25)
        count = max(1, sum(1 for v in vehicles if _intersects(_box(v), near)))  # contract needs at least 1

        nbox = (box[0] / width, box[1] / height, box[2] / width, box[3] / height)
        area = max(0.0, nbox[2] - nbox[0]) * max(0.0, nbox[3] - nbox[1])
        lanes = tuple(name for name, (lo, hi) in self.lanes.items() if nbox[0] < hi and lo < nbox[2]) or ("LANE_1",)
        return Detection(
            confidence=round(sum(h[0] for h in hits) / len(hits), 4),
            severity=classify_severity(count, area),
            vehicles=count, lanes=lanes, box=nbox,
            frames_confirmed=len(hits), frames_sampled=len(frames), model_version=self.model_version,
        ), scores


def sample_frames(video_path: str | Path, count: int = 5) -> list:
    """Evenly spaced RGB frames from a clip."""
    import cv2
    from PIL import Image

    cap = cv2.VideoCapture(str(video_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        raise ValueError(f"cannot read frames from {video_path}")
    out = []
    for i in range(count):
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(total - 1, int((i + 0.5) * total / count)))
        ok, bgr = cap.read()
        if ok:
            out.append(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
    cap.release()
    return out
