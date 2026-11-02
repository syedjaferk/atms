"""The video catalog. Which videos belong to which camera, and whether each one is a good scenario (nothing happens) or a bad one
(an accident).

The index is dataset/videos.json, written by scripts/build_dataset.py. Two folders hold the files, so they can be mounted separately:
the dataset folder (stitched clips and the index) and the videos folder (your own recordings, never modified).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Video:
    id: str
    camera_id: str
    title: str
    scenario: str                 # good or bad
    truth: str                    # normal, accident, or unlabelled (nobody has checked it by eye)
    label_source: str             # "ground truth" from a labelled dataset, or "model" when a model pre-sorted it
    root: str                     # "dataset" or "videos"
    file: str                     # relative to that root
    duration_s: float
    size_bytes: int
    model: dict = field(default_factory=dict)      # what the AI said when the catalog was built: accident, confidence, severity


class Catalog:
    def __init__(self, videos: list[Video], dataset_dir: Path, videos_dir: Path):
        self.videos = {v.id: v for v in videos}
        self.dataset_dir, self.videos_dir = dataset_dir, videos_dir

    def for_camera(self, camera_id: str) -> list[Video]:
        return sorted((v for v in self.videos.values() if v.camera_id == camera_id), key=lambda v: (v.scenario, v.id))

    def path(self, v: Video) -> Path:
        root = self.dataset_dir if v.root == "dataset" else self.videos_dir
        p = (root / v.file).resolve()
        if root.resolve() not in p.parents:
            raise ValueError("video path escapes its folder")
        return p

    def exists(self, v: Video) -> bool:
        try:
            return self.path(v).is_file()
        except ValueError:
            return False


def dirs() -> tuple[Path, Path]:
    return Path(os.environ.get("ATMS_DATASET_DIR", "dataset")), Path(os.environ.get("ATMS_VIDEO_DIR", "videos"))


def load(dataset_dir: str | Path | None = None, videos_dir: str | Path | None = None) -> Catalog:
    d, v = dirs()
    d, v = Path(dataset_dir) if dataset_dir else d, Path(videos_dir) if videos_dir else v
    index = d / "videos.json"
    raw = json.loads(index.read_text()) if index.is_file() else {"videos": []}
    return Catalog([Video(**item) for item in raw["videos"]], d, v)
