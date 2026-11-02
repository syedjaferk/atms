"""The edge node as a service. It stands in for the compute box next to a roadside camera.

It watches nothing by itself. You ask it to check one video from the catalog, it runs the AI models over frames of that video,
and if it sees an accident it does what a real camera box does: cuts the evidence clip, then publishes accident.detected and
video.ready over MQTT. Everything after that is the core platform's job.

    POST /v1/analyze   {"video_id": "v4", "publish": true}   ->  202 {"job_id": ...}
    GET  /v1/jobs/{id}                                       ->  state, per frame scores, verdict, what was published
    GET  /health

One job runs at a time because the models use the whole CPU. Further jobs wait their turn and report their place in the queue.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from atms_contracts import catalog as catalog_mod
from atms_contracts.cameras import CAMERAS

from . import checker
from .detector import AccidentDetector, sample_frames_timed

log = logging.getLogger("atms.detector")
EVIDENCE_BEFORE_S, EVIDENCE_LENGTH_S = 4.0, 12.0


class AnalyzeBody(BaseModel):
    video_id: str
    camera_id: str | None = None        # defaults to the camera the catalog gave the video to
    publish: bool = True


@dataclass
class Job:
    id: str
    video_id: str
    camera_id: str
    publish: bool
    state: str = "queued"               # queued, running, done, failed
    done: int = 0
    total: int = 0
    scores: list[float] = field(default_factory=list)
    times: list[float] = field(default_factory=list)
    verdict: dict | None = None
    published: dict | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def view(self, queue_position: int = 0) -> dict:
        return {"job_id": self.id, "video_id": self.video_id, "camera_id": self.camera_id, "state": self.state,
                "queue_position": queue_position, "frames_done": self.done, "frames_total": self.total,
                "frame_scores": self.scores, "frame_times_s": self.times, "verdict": self.verdict, "published": self.published,
                "error": self.error, "seconds": round((self.finished or time.time()) - self.created, 1)}


def frames_for(duration_s: float) -> int:
    """One frame every ten seconds or so, between 8 and 24. A short clip gets the same care as a long one."""
    return max(8, min(24, round(duration_s / 10)))


def create_service(catalog: catalog_mod.Catalog, detector: Callable[[], AccidentDetector], edge_factory: Callable[[], object],
                   evidence_dir: str | Path = "simulator-out/tmp", warm: bool = True) -> FastAPI:
    """detector() builds the models (slow, called once). edge_factory() returns an EdgeController wired to the broker."""
    app = FastAPI(title="ATMS detector node")
    jobs: dict[str, Job] = {}
    order: list[str] = []
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detector")
    model: dict = {}
    lock = threading.Lock()

    def load_model() -> AccidentDetector:
        with lock:
            if "m" not in model:
                t = time.time()
                model["m"] = detector()
                log.info("models ready in %.1fs", time.time() - t)
            return model["m"]

    if warm:
        threading.Thread(target=lambda: _quiet(load_model), name="warm", daemon=True).start()

    def run(job: Job) -> None:
        job.state = "running"
        try:
            video = catalog.videos[job.video_id]
            path = catalog.path(video)
            sampled = sample_frames_timed(path, frames_for(video.duration_s))
            frames = [f for f, _ in sampled]
            job.total, job.times = len(frames), [t for _, t in sampled]
            det = load_model()

            def progress(i: int, n: int, score: float) -> None:
                job.done, job.scores = i + 1, [*job.scores, score]

            result, scores, top = det.scan(frames, min(det.min_confirmed_frames, len(frames)) or 1, on_frame=progress)
            job.scores = scores
            job.verdict = None if result is None else {
                "accident": True, "confidence": result.confidence, "severity": result.severity.value, "vehicles": result.vehicles,
                "lanes": list(result.lanes), "frames_confirmed": result.frames_confirmed, "frames_sampled": result.frames_sampled,
                "best_frame": top, "best_time_s": job.times[top] if top is not None else None, "model_version": result.model_version}
            if result is None:
                job.verdict = {"accident": False, "frames_sampled": len(frames), "best_score": max(scores, default=0.0)}
            elif job.publish:
                job.published = publish(job, path, result, job.times[top] if top is not None else 0.0)
            job.state = "done"
        except Exception as e:                      # noqa: BLE001  a bad video must not stop the worker
            log.exception("job %s failed", job.id)
            job.state, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            job.finished = time.time()

    def publish(job: Job, path: Path, result, at_s: float) -> dict:
        start = max(0.0, at_s - EVIDENCE_BEFORE_S)
        clip = checker.cut_clip(path, start, EVIDENCE_LENGTH_S, Path(evidence_dir) / f"{job.id}.mp4")
        edge = edge_factory()
        try:
            event = edge.report_detection(job.camera_id, result.confidence, result.severity, result.vehicles, list(result.lanes),
                                          clip_seconds=EVIDENCE_LENGTH_S, model_version=result.model_version, clip_path=clip)
            sent = edge.run_until_empty()
        finally:
            clip.unlink(missing_ok=True)
            getattr(edge.publisher, "close", lambda: None)()
        return {"event_id": str(event.event_id), "camera_id": job.camera_id, "sent": sent}

    @app.get("/health")
    async def health():
        return {"ok": True, "model_loaded": "m" in model, "videos": len(catalog.videos), "jobs": len(jobs)}

    @app.post("/v1/analyze", status_code=202)
    async def analyze(body: AnalyzeBody):
        v = catalog.videos.get(body.video_id)
        if v is None:
            raise HTTPException(404, "unknown video")
        if not catalog.exists(v):
            raise HTTPException(404, "the video file is missing from the mounted folder")
        camera = body.camera_id or v.camera_id
        if camera not in CAMERAS:
            raise HTTPException(422, f"unknown camera {camera}")
        job = Job(uuid.uuid4().hex[:12], v.id, camera, body.publish)
        jobs[job.id] = job
        order.append(job.id)
        for old in order[:-50]:                       # keep the last 50
            jobs.pop(old, None)
        del order[:-50]
        pool.submit(run, job)
        return job.view(sum(1 for j in jobs.values() if j.state in ("queued", "running")) - 1)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        ahead = sum(1 for j in jobs.values() if j.state in ("queued", "running") and j.created < job.created) if job.state == "queued" else 0
        return job.view(ahead)

    return app


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:                               # noqa: BLE001
        logging.getLogger("atms.detector").exception("model warm up failed, it will be tried again on the first job")


def main() -> None:
    import uvicorn

    from atms_sim.edge import EdgeController, LocalClipStore
    from atms_sim.outbox import Outbox
    from atms_sim.publishers import MqttPublisher

    logging.basicConfig(level=logging.INFO)
    out = Path(os.environ.get("ATMS_MEDIA_DIR", "simulator-out"))
    host, port = os.environ.get("ATMS_BROKER_HOST", "localhost"), int(os.environ.get("ATMS_BROKER_PORT", "1883"))
    user, password = os.environ.get("ATMS_BROKER_USER", "atms"), os.environ.get("ATMS_BROKER_PASSWORD", "atms-dev")

    def edge() -> EdgeController:
        pub = MqttPublisher(host, port, user, password, client_id=f"edge-node-{uuid.uuid4().hex[:6]}")
        return EdgeController(Outbox(str(out / "outbox.sqlite3")), pub, clips=LocalClipStore(out / "clips"))

    app = create_service(catalog_mod.load(), AccidentDetector.from_huggingface, edge, out / "tmp")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ATMS_DETECTOR_PORT", "8300")))


if __name__ == "__main__":
    main()
