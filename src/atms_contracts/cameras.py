"""The roadside cameras. Kept as a module of its own because the edge tools and the operator map both import CAMERAS."""
from __future__ import annotations

from .network import Camera, load

CAMERAS: dict[str, Camera] = load().cameras

__all__ = ["CAMERAS", "Camera"]
