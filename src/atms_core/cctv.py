"""The CCTV side of the API. Which cameras exist, which videos belong to each, and a way to ask the detector node to check one.

The core never runs the AI models itself. It hands the request to the detector node (a separate service, ATMS_DETECTOR_URL) and
passes its progress back. The browser only ever talks to this API, so there is one origin and one token.
"""
from __future__ import annotations

import time
from typing import Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from atms_contracts import catalog as catalog_mod, network

LINK_TTL_S = 900


class AnalyzeBody(BaseModel):
    publish: bool = True


def mount(app: FastAPI, operator: Callable, catalog: catalog_mod.Catalog, detector_url: str | None,
          sign: Callable[[str, int], str], verify: Callable[[str, int, str], bool]) -> None:
    net = network.load()
    client = httpx.AsyncClient(base_url=detector_url or "http://detector.invalid", timeout=10)

    def play_url(request: Request, v: catalog_mod.Video) -> str | None:
        if not catalog.exists(v):
            return None
        path = f"/v1/cctv/media/{v.root}/{v.file}"
        exp = int(time.time()) + LINK_TTL_S
        return f"{request.base_url}{path[1:]}?exp={exp}&sig={sign(path, exp)}"

    def video_view(request: Request, v: catalog_mod.Video) -> dict:
        return {"id": v.id, "camera_id": v.camera_id, "title": v.title, "scenario": v.scenario, "truth": v.truth,
                "label_source": v.label_source, "duration_s": v.duration_s, "size_mb": round(v.size_bytes / 1e6, 1),
                "model": v.model, "play_url": play_url(request, v)}

    @app.get("/v1/cctv")
    async def overview(_p=Depends(operator)):
        """Every area with its cameras and how many good and bad videos each camera has."""
        areas = []
        for a in net.areas.values():
            cams = []
            for c in net.cameras_in(a.id):
                vids = catalog.for_camera(c.site.camera_id)
                cams.append({"id": c.site.camera_id, "name": c.name, "lat": c.location.lat, "lon": c.location.lon,
                             "heading_deg": c.heading_deg, "good": sum(v.scenario == "good" for v in vids),
                             "bad": sum(v.scenario == "bad" for v in vids)})
            areas.append({"id": a.id, "name": a.name, "road": a.road_name, "road_segment": a.road_segment,
                          "center": {"lat": a.center.lat, "lon": a.center.lon}, "corridor": [list(p) for p in a.corridor], "cameras": cams})
        return {"areas": areas, "videos": len(catalog.videos), "detector": await _detector_health()}

    @app.get("/v1/cctv/cameras/{camera_id}/videos")
    async def camera_videos(camera_id: str, request: Request, _p=Depends(operator)):
        if camera_id not in net.cameras:
            raise HTTPException(404, "unknown camera")
        c = net.cameras[camera_id]
        return {"camera": {"id": camera_id, "name": c.name, "area": c.area, "lat": c.location.lat, "lon": c.location.lon},
                "videos": [video_view(request, v) for v in catalog.for_camera(camera_id)]}

    @app.get("/v1/cctv/media/{root}/{ref:path}", include_in_schema=False)
    async def media(root: str, ref: str, exp: int, sig: str):
        if root not in ("dataset", "videos") or not verify(f"/v1/cctv/media/{root}/{ref}", exp, sig):
            raise HTTPException(403, "link expired or invalid")
        v = next((x for x in catalog.videos.values() if x.root == root and x.file == ref), None)
        if v is None or not catalog.exists(v):
            raise HTTPException(404, "not found")
        return FileResponse(catalog.path(v), media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    async def _detector_health() -> dict:
        if not detector_url:
            return {"up": False, "reason": "ATMS_DETECTOR_URL is not set"}
        try:
            r = await client.get("/health", timeout=3)
            return {"up": True, **r.json()}
        except Exception:                                  # noqa: BLE001
            return {"up": False, "reason": "the detector node did not answer"}

    @app.post("/v1/cctv/videos/{video_id}/analyze", status_code=202)
    async def analyze(video_id: str, body: AnalyzeBody | None = None, _p=Depends(operator)):
        if video_id not in catalog.videos:
            raise HTTPException(404, "unknown video")
        if not detector_url:
            raise HTTPException(503, "no detector node is configured (ATMS_DETECTOR_URL)")
        try:
            r = await client.post("/v1/analyze", json={"video_id": video_id, "publish": (body or AnalyzeBody()).publish})
        except httpx.HTTPError:
            raise HTTPException(503, "the detector node did not answer") from None
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", "detector refused"))
        return r.json()

    @app.get("/v1/cctv/jobs/{job_id}")
    async def job(job_id: str, _p=Depends(operator)):
        try:
            r = await client.get(f"/v1/jobs/{job_id}")
        except httpx.HTTPError:
            raise HTTPException(503, "the detector node did not answer") from None
        if r.status_code >= 400:
            raise HTTPException(r.status_code, "unknown job")
        return r.json()
