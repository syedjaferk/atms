"""Public API (contracts/openapi.yaml). Thin: authenticates, calls the Incident Service, maps errors."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from atms_contracts.models import Agency, IncidentState, ResponderStatus
from atms_contracts.signing import verify_receipt

from .incident_service import Conflict, Forbidden, NotFound
from .map_view import build_map, road_state
from .realtime import RealtimeGateway
from .review_view import allowed, units, verdict
from .system import System
from .views import (AssignmentView, IncidentDetailView, IncidentView, NotificationView, assignment_view, detail_view,
                    incident_view, notification_view)

EVIDENCE_TTL_S = 300
CONSOLE_DIR = Path(__file__).parent / "console"


@dataclass(frozen=True)
class Principal:
    name: str
    agency: Agency | None = None     # None means an operator or a sensor feed
    role: str = "operator"           # operator, or sensor for traffic readings only

    @property
    def is_operator(self) -> bool:
        return self.agency is None and self.role == "operator"


class AssignmentPatch(BaseModel):
    status: ResponderStatus
    eta_s: int | None = Field(default=None, ge=0)
    note: str | None = None


class RejectBody(BaseModel):
    reason: str = Field(min_length=3)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def sign(path: str, expires: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"{path}:{expires}".encode(), hashlib.sha256).hexdigest()


def verify_signature(path: str, expires: int, sig: str, secret: str, now: float | None = None) -> bool:
    return (now or time.time()) <= expires and hmac.compare_digest(sign(path, expires, secret), sig)


def create_api(system: System, tokens: dict[str, Principal], signing_secret: str, run_bus: bool = True,
               media_dir: str | Path = "simulator-out", catalog=None, detector_url: str | None = None) -> FastAPI:
    gateway = RealtimeGateway(system.store)
    system.bus.subscribe("realtime", None, gateway.on_event, broadcast=True)
    svc, store = system.service, system.store

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_bus:
            await system.start()
        yield
        if run_bus:
            await system.stop()

    app = FastAPI(title="ATMS Incident API", version="0.1.0", lifespan=lifespan)
    app.state.gateway = gateway

    bearer = HTTPBearer(auto_error=False, description="A token such as ops-dev (development only)")   # gives Swagger its Authorize button

    def principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
            raise HTTPException(401, "missing bearer token")
        p = tokens.get(creds.credentials.strip())
        if p is None:
            raise HTTPException(401, "invalid token")
        return p

    def operator(p: Principal = Depends(principal)) -> Principal:
        if not p.is_operator:
            raise HTTPException(403, "operators only")
        return p

    def sensor(p: Principal = Depends(principal)) -> Principal:
        if not (p.is_operator or p.role == "sensor"):
            raise HTTPException(403, "sensor or operator only")
        return p

    if system.forecast is not None:
        from atms_forecast.api import mount as mount_forecast

        from .forecast_link import incident_inputs

        mount_forecast(app, system.forecast, principal, operator, sensor,
                       incident_for=lambda iid: incident_inputs(system.store, iid))

    if catalog is not None:
        from . import cctv

        cctv.mount(app, operator, catalog, detector_url, lambda path, exp: sign(path, exp, signing_secret),
                   lambda path, exp, sig: verify_signature(path, exp, sig, signing_secret))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.exception_handler(Conflict)
    async def _conflict(_r, exc: Conflict):
        return JSONResponse({"error": exc.message, "current_state": exc.current_state}, status_code=409)

    @app.exception_handler(NotFound)
    async def _not_found(_r, exc: NotFound):
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.exception_handler(Forbidden)
    async def _forbidden(_r, exc: Forbidden):
        return JSONResponse({"error": str(exc)}, status_code=403)

    def idempotent(p: Principal, request: Request, key: str | None, make):
        """A repeated Idempotency-Key returns the first answer and does not run the command again."""
        if not key:
            return make()
        k = (p.name, request.method, request.url.path, key)
        found = store.repo.idempotency_get(k)
        if found is None:
            found = make()
            if found[0] < 500:
                store.repo.idempotency_put(k, found[0], found[1])
        return found

    def respond(view) -> tuple[int, dict]:
        return 200, view.model_dump(mode="json")

    def reply(result: tuple[int, dict]) -> JSONResponse:
        return JSONResponse(result[1], status_code=result[0])

    @app.get("/v1/incidents")
    async def list_incidents(state: list[IncidentState] | None = Query(default=None), road: str | None = None,
                       limit: int = Query(default=50, ge=1, le=200), _p: Principal = Depends(principal)):
        items = [i for i in store.incidents.values()
                 if (not state or i.state in state) and (not road or road in i.road_segment)]
        items.sort(key=lambda i: i.opened_at, reverse=True)
        return {"items": [incident_view(i).model_dump(mode="json") for i in items[:limit]]}

    @app.get("/v1/map")
    async def map_data(_p: Principal = Depends(operator)):
        """Road state, forecasts, facilities, message boards and open incidents, for the operator console."""
        road = await asyncio.to_thread(road_state, system.forecast)      # model calls take seconds, keep them off the event loop
        return build_map(system, road=road)

    @app.get("/v1/incidents/{incident_id}", response_model=IncidentDetailView)
    async def get_incident(incident_id: UUID, _p: Principal = Depends(principal)):
        inc = store.incidents.get(incident_id)
        if inc is None:
            raise NotFound()
        return detail_view(inc, store)

    @app.post("/v1/incidents/{incident_id}/verify", response_model=IncidentView)
    async def verify(incident_id: UUID, request: Request, p: Principal = Depends(operator),
               idempotency_key: str | None = Header(default=None)):
        return reply(idempotent(p, request, idempotency_key,
                                lambda: respond(incident_view(svc.verify(incident_id, operator_id=p.name)))))

    @app.post("/v1/incidents/{incident_id}/reject", response_model=IncidentView)
    async def reject(incident_id: UUID, body: RejectBody, request: Request, p: Principal = Depends(operator),
               idempotency_key: str | None = Header(default=None)):
        return reply(idempotent(p, request, idempotency_key,
                                lambda: respond(incident_view(svc.reject(incident_id, body.reason, p.name)))))

    @app.post("/v1/incidents/{incident_id}/resolve", response_model=IncidentView)
    async def resolve(incident_id: UUID, request: Request, p: Principal = Depends(principal),
                idempotency_key: str | None = Header(default=None)):
        inc = store.incidents.get(incident_id)
        if inc is None:
            raise NotFound()
        if not p.is_operator and not any(a.agency == p.agency for a in store.for_incident(incident_id)):
            raise Forbidden("your agency is not assigned to this incident")
        return reply(idempotent(p, request, idempotency_key,
                                lambda: respond(incident_view(svc.resolve(incident_id, p.name)))))

    @app.post("/v1/incidents/{incident_id}/close", response_model=IncidentView)
    async def close(incident_id: UUID, request: Request, p: Principal = Depends(operator),
              idempotency_key: str | None = Header(default=None)):
        return reply(idempotent(p, request, idempotency_key, lambda: respond(incident_view(svc.close(incident_id)))))

    @app.patch("/v1/incidents/{incident_id}/assignments/{assignment_id}", response_model=AssignmentView)
    async def patch_assignment(incident_id: UUID, assignment_id: UUID, body: AssignmentPatch, request: Request,
                         p: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)):
        def run():
            a = svc.update_assignment(incident_id, assignment_id, body.status, body.eta_s, p.agency)
            if body.note:
                svc.add_note(incident_id, body.note, p.name)
            return respond(assignment_view(a))
        return reply(idempotent(p, request, idempotency_key, run))

    @app.post("/v1/incidents/{incident_id}/notes", status_code=201)
    async def add_note(incident_id: UUID, body: NoteBody, p: Principal = Depends(principal)):
        svc.add_note(incident_id, body.text, p.name)
        return {"ok": True}

    media_root = Path(media_dir).resolve()

    def evidence_items(inc, request: Request) -> list[dict]:
        expires = int(time.time()) + EVIDENCE_TTL_S
        refs = [("clip", e["ref"]) for e in inc.evidence] + ([("report", inc.report_ref)] if inc.report_ref else [])
        items = []
        for kind, ref in refs:
            path = f"/media/{ref}"
            url = f"{request.base_url}media/{ref}?exp={expires}&sig={sign(path, expires, signing_secret)}"
            items.append({"kind": kind, "url": url, "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))})
        return items

    @app.get("/v1/incidents/{incident_id}/evidence")
    async def evidence(incident_id: UUID, request: Request, _p: Principal = Depends(operator)):
        inc = store.incidents.get(incident_id)
        if inc is None:
            raise NotFound()
        return {"items": evidence_items(inc, request)}

    def media_file(ref: str) -> Path | None:
        path = (media_root / ref).resolve()
        return path if media_root in path.parents and path.is_file() else None

    @app.get("/media/{ref:path}", include_in_schema=False)
    async def media(ref: str, exp: int, sig: str):
        """Serves a clip or report to whoever holds a link signed by /evidence and not yet expired. Range requests work, so video seeks."""
        if not verify_signature(f"/media/{ref}", exp, sig, signing_secret):
            raise HTTPException(403, "link expired or not valid")
        path = media_file(ref)
        if path is None:
            raise HTTPException(404, "file not found")
        return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else None,
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/v1/incidents/{incident_id}/review")
    async def review(incident_id: UUID, request: Request, _p: Principal = Depends(operator)):
        """Everything for judging one incident in one call, with the actions the incident service would accept right now."""
        inc = store.incidents.get(incident_id)
        if inc is None:
            raise NotFound()
        clips = {e["ref"]: e for e in inc.evidence}
        evidence = []
        for item in evidence_items(inc, request):
            ref = item["url"].split("/media/", 1)[1].split("?", 1)[0]
            path = media_file(ref)
            meta = clips.get(ref, {})
            evidence.append({**item, "camera_id": meta.get("camera_id"), "sha256": meta.get("sha256"),
                             "available": path is not None, "size_bytes": path.stat().st_size if path else None})
        return {"incident": incident_view(inc).model_dump(mode="json"), "assessment": verdict(inc, svc.policy),
                "evidence": evidence, "allowed": allowed(inc), "units": units(store, inc)}

    @app.get("/v1/incidents/{incident_id}/notifications")
    async def list_notifications(incident_id: UUID, _p: Principal = Depends(operator)):
        if incident_id not in store.incidents:
            raise NotFound()
        items = sorted((n for n in store.notifications.values() if n.incident_id == incident_id),
                       key=lambda n: n.created_at)
        return {"items": [notification_view(n).model_dump(mode="json") for n in items]}

    @app.post("/v1/notifications/{notification_id}/retry", response_model=NotificationView)
    async def retry_notification(notification_id: UUID, _p: Principal = Depends(operator)):
        return notification_view(await system.notifications.retry(notification_id))

    @app.post("/v1/notifications/receipts/{provider}")
    async def receipt(provider: str, request: Request, x_signature: str | None = Header(default=None)):
        """Delivery receipts from an SMS provider. Authenticated by a signature of the body, not a bearer token."""
        secret = system.notifications.receipt_secrets.get(provider)
        raw = await request.body()
        if secret is None or not verify_receipt(secret, x_signature, raw):
            raise HTTPException(401, "bad receipt signature")
        try:
            body = json.loads(raw)
            ref, status = body["message_id"], body["status"]
        except (ValueError, KeyError):
            raise HTTPException(422, "expected message_id and status")
        if status not in ("delivered", "undelivered"):
            raise HTTPException(422, "status must be delivered or undelivered")
        n = system.notifications.record_receipt(provider, ref, status == "delivered", body.get("reason"))
        return {"id": str(n.id), "status": n.status}

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket):
        p = tokens.get(ws.query_params.get("token", ""))
        if p is None or not p.is_operator:
            await ws.close(code=4401)
            return
        await ws.accept()
        q = gateway.connect()
        listener = asyncio.create_task(ws.receive_text())       # notices the client going away
        try:
            while True:
                nxt = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({listener, nxt}, return_when=asyncio.FIRST_COMPLETED)
                if listener in done:
                    nxt.cancel()
                    break
                await ws.send_json(nxt.result())
        except WebSocketDisconnect:
            pass
        finally:
            listener.cancel()
            gateway.disconnect(q)

    @app.get("/", include_in_schema=False)
    async def home():
        return RedirectResponse("/console/")

    # the console is plain files, it asks for an operator token and calls the same API as any other client
    app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")
    return app
