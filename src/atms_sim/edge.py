"""Simulated edge controller: outbox first, publish after, clip upload on the side."""
from __future__ import annotations

import hashlib
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from atms_contracts.models import (
    AccidentDetected, AccidentDetectedData, VideoReady, VideoReadyData, VideoStatus, make_dedup_key,
)
from atms_contracts.topics import event_topic

from .outbox import Outbox
from .publishers import PublishError, Publisher
from .scenarios import CAMERAS, Scenario

MODEL_VERSION = "sim-detector-0.1"


class LocalClipStore:
    """Stands in for the object store. Writes a deterministic fake clip to disk."""

    def __init__(self, root: str | Path = "simulator-out/clips"):
        self.root = Path(root)

    def put(self, camera_id: str, name: str, seconds: float) -> tuple[str, int, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        data = random.Random(name).randbytes(int(seconds * 1024))
        path = self.root / f"{camera_id}-{name}.mp4"
        path.write_bytes(data)
        return f"clips/{path.name}", len(data), hashlib.sha256(data).hexdigest()

    def put_file(self, camera_id: str, name: str, source: str | Path) -> tuple[str, int, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        data = Path(source).read_bytes()
        path = self.root / f"{camera_id}-{name}{Path(source).suffix}"
        path.write_bytes(data)
        return f"clips/{path.name}", len(data), hashlib.sha256(data).hexdigest()


class PlayableClipStore(LocalClipStore):
    """Writes a short clip a browser can play, so the controller area has something to watch.

    It is a labelled test pattern made with ffmpeg, not footage of a crash. Real footage goes in with --video.
    """

    def put(self, camera_id: str, name: str, seconds: float) -> tuple[str, int, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{camera_id}-{name}.mp4"
        label = f"SIMULATED CCTV {camera_id}  %{{pts\\:hms}}"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=12",
             "-vf", f"drawtext=text='{label}':x=16:y=16:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.6",
             "-t", str(seconds), "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(path)], check=True)
        data = path.read_bytes()
        return f"clips/{path.name}", len(data), hashlib.sha256(data).hexdigest()


class EdgeController:
    def __init__(self, outbox: Outbox, publisher: Publisher, clips: LocalClipStore | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.outbox = outbox
        self.publisher = publisher
        self.clips = clips or LocalClipStore()
        self.clock = clock

    def report(self, scenario: Scenario) -> AccidentDetected:
        """Queue a scripted detection. Nothing here touches the network."""
        return self.report_detection(scenario.camera_id, scenario.confidence, scenario.severity,
                                     scenario.vehicles, list(scenario.lanes), scenario.clip_seconds)

    def report_detection(self, camera_id: str, confidence: float, severity, vehicles: int,
                         lanes: list[str], clip_seconds: float = 60.0, model_version: str = MODEL_VERSION,
                         clip_path: str | Path | None = None) -> AccidentDetected:
        """Queue accident.detected and video.ready. Nothing here touches the network."""
        cam = CAMERAS[camera_id]
        now = self.clock()
        event = AccidentDetected(
            producer=f"edge/{cam.site.camera_id}",
            occurred_at=now,
            dedup_key=make_dedup_key(cam.site.camera_id, "accident.detected", now),
            data=AccidentDetectedData(
                site=cam.site, location=cam.location, heading_deg=cam.heading_deg,
                confidence=confidence, severity=severity,
                vehicles_involved=vehicles, affected_lanes=lanes,
                video_status=VideoStatus.uploading, model_version=model_version,
            ),
        )
        topic = event_topic(cam.site.state, cam.site.city, cam.site.road, cam.site.camera_id)
        self._enqueue(event.event_id, topic, event.model_dump_json())

        if clip_path:
            video_ref, size, sha = self.clips.put_file(cam.site.camera_id, str(event.event_id), clip_path)
        else:
            video_ref, size, sha = self.clips.put(cam.site.camera_id, str(event.event_id), clip_seconds)
        ready = VideoReady(
            producer=event.producer, occurred_at=self.clock(),
            data=VideoReadyData(camera_id=cam.site.camera_id, video_ref=video_ref,
                                duration_s=clip_seconds, size_bytes=size, sha256=sha),
        )
        video_topic = event_topic(cam.site.state, cam.site.city, cam.site.road, cam.site.camera_id, "video")
        self._enqueue(ready.event_id, video_topic, ready.model_dump_json())
        return event

    def _enqueue(self, event_id, topic: str, payload: str) -> None:
        self.outbox.add(str(event_id), topic, payload.encode())

    def drain(self) -> int:
        """Publish everything pending. Stops at the first failure so order is kept."""
        sent = 0
        for row in self.outbox.pending():
            self.outbox.mark_attempt(row.event_id)
            try:
                self.publisher.publish(row.topic, row.payload)
            except PublishError:
                break
            self.outbox.delete(row.event_id)
            sent += 1
        return sent

    def run_until_empty(self, max_wait_s: float = 30.0, base_delay_s: float = 0.5) -> bool:
        """Drain with exponential backoff. Returns True when the outbox is empty."""
        delay, deadline = base_delay_s, time.monotonic() + max_wait_s
        while self.outbox.depth():
            self.drain()
            if not self.outbox.depth():
                break
            if time.monotonic() + delay > deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
        return True
