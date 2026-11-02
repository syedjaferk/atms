"""Dispatch Orchestrator. Runs the dispatch saga for a verified incident and compensates on rejection.

It never writes incident state. It emits assignment events and the Incident Service materialises them.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from uuid import UUID, uuid4

from atms_contracts.models import (
    Agency, AssignmentAcked, AssignmentAckedData, AssignmentCancelled, AssignmentCancelledData,
    AssignmentDeclined, AssignmentDeclinedData, AssignmentRequested, AssignmentRequestedData, AssignmentState,
    AssignmentTimedOut, AssignmentTimedOutData, Envelope, IncidentRejected, IncidentState, IncidentVerified,
    Severity,
)

from .bus import InMemoryBus
from .geo import FacilityService
from .hub import AgencyAdapter, HubTimeout, HubUnavailable
from .state import SETTLED
from .store import Incident, Store

log = logging.getLogger("atms.orchestrator")
PRODUCER = "dispatch-orchestrator"

AGENCIES_BY_SEVERITY: dict[Severity, list[Agency]] = {
    Severity.minor: [Agency.police, Agency.tow],
    Severity.major: [Agency.police, Agency.ambulance, Agency.highway_patrol, Agency.tow],
    Severity.critical: [Agency.police, Agency.ambulance, Agency.highway_patrol, Agency.fire, Agency.tow],
}


@dataclass(frozen=True)
class DispatchConfig:
    max_attempts: int = 4
    timeouts_per_unit: int = 2      # same unit asked again (same key) before moving to the next unit
    resume_grace_s: float = 5.0     # an unanswered request nobody is working on this long gets picked up again


class DispatchOrchestrator:
    def __init__(self, store: Store, bus: InMemoryBus, geo: FacilityService, hub: AgencyAdapter,
                 config: DispatchConfig | None = None):
        self.store, self.bus, self.geo, self.hub = store, bus, geo, hub
        self.config = config or DispatchConfig()
        self.tasks: set[asyncio.Task] = set()
        self._started: set[tuple[UUID, Agency]] = set()     # sagas begun in this process

    async def on_event(self, ev: Envelope) -> None:
        inc = self.store.incidents.get(ev.incident_id) if ev.incident_id else None
        if inc is None:
            return
        if isinstance(ev, IncidentVerified):
            have = {a.agency for a in self.store.for_incident(inc.id)}     # a replayed event must not dispatch twice
            for agency in AGENCIES_BY_SEVERITY[inc.severity]:
                if agency not in have and (inc.id, agency) not in self._started:
                    self._started.add((inc.id, agency))
                    self._spawn(self._dispatch_agency(inc, agency))
        elif isinstance(ev, IncidentRejected):
            await self._cancel_all(inc, "incident rejected")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("dispatch task failed", exc_info=task.exception())

    async def sweep(self, interval_s: float = 1.0) -> None:
        while True:
            try:
                self.resume_open(self.config.resume_grace_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("dispatch resume sweep failed, will retry")
            await asyncio.sleep(interval_s)

    async def idle(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def _emit(self, cls, inc: Incident, data) -> None:
        self.store.emit(cls(producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id, data=data))
        self.store.commit()

    def _stopped(self, inc: Incident) -> bool:
        return inc.state in (IncidentState.rejected, IncidentState.closed)

    # ------------------------------------------------------------ saga for one agency

    def resume_open(self, min_age_s: float = 0.0) -> int:
        """Continue dispatches that were asked for but never answered and that no saga in this process is working on.

        Runs at start and then periodically, because after a restart the assignment record may only appear a moment
        later, when the incident service has caught up on the topic.
        The agency sees the same idempotency key, so a request that did reach it is answered, not repeated.
        """
        n = 0
        now = datetime.now(timezone.utc)
        for a in list(self.store.assignments.values()):
            inc = self.store.incidents.get(a.incident_id)
            if (inc and a.state in (AssignmentState.requested, AssignmentState.timed_out)
                    and (now - a.requested_at).total_seconds() >= min_age_s
                    and inc.state in (IncidentState.verified, IncidentState.dispatching, IncidentState.dispatched)
                    and (inc.id, a.agency) not in self._started):
                self._started.add((inc.id, a.agency))
                self._spawn(self._dispatch_agency(inc, a.agency, resume=a))
                n += 1
        return n

    async def _dispatch_agency(self, inc: Incident, agency: Agency, resume=None) -> None:
        ranked = self.geo.nearest(agency, inc.location, n=self.config.max_attempts)
        aid, rank, timeouts, last = uuid4(), 0, 0, None
        first_attempt = 1
        if resume is not None:
            aid, first_attempt = resume.id, resume.attempt
            rank = next((i for i, r in enumerate(ranked) if r.facility.id == resume.facility), 0)
        for attempt in range(first_attempt, self.config.max_attempts + 1):
            if self._stopped(inc):
                return
            facility = ranked[min(rank, len(ranked) - 1)].facility if ranked else None
            key = f"{inc.id}:{agency.value}:{rank}"          # same unit, same key, so a retry never doubles up
            self._emit(AssignmentRequested, inc, AssignmentRequestedData(
                assignment_id=aid, agency=agency, unit_ref=facility.id if facility else None,
                idempotency_key=key, attempt=attempt))
            try:
                result = await self.hub.dispatch(agency, str(inc.id), key, inc.severity, inc.location,
                                                 facility.id if facility else None)
            except HubTimeout:
                self._emit(AssignmentTimedOut, inc, AssignmentTimedOutData(assignment_id=aid, agency=agency, attempt=attempt))
                last, timeouts = "timeout", timeouts + 1
                if timeouts >= self.config.timeouts_per_unit:
                    rank, timeouts = rank + 1, 0
                continue
            except HubUnavailable as exc:
                self._emit(AssignmentDeclined, inc, AssignmentDeclinedData(
                    assignment_id=aid, agency=agency, reason=f"agency unreachable: {exc}"))
                last, rank, timeouts = "declined", rank + 1, 0
                continue

            if result.accepted:
                if self._stopped(inc) or self._cancelled(aid):   # rejected while the call was in flight
                    if not self._cancelled(aid):
                        self._emit(AssignmentCancelled, inc, AssignmentCancelledData(
                            assignment_id=aid, agency=agency, reason="incident rejected"))
                    await self._safe_cancel(agency, inc, key)
                    return
                self._emit(AssignmentAcked, inc, AssignmentAckedData(
                    assignment_id=aid, agency=agency, unit_ref=result.unit_ref, eta_s=result.eta_s))
                return
            self._emit(AssignmentDeclined, inc, AssignmentDeclinedData(
                assignment_id=aid, agency=agency, reason=result.reason or "declined"))
            last, rank, timeouts = "declined", rank + 1, 0

        if last == "timeout":       # give up; a timed out assignment is not a final state for an operator
            self._emit(AssignmentDeclined, inc, AssignmentDeclinedData(
                assignment_id=aid, agency=agency, reason=f"no answer after {self.config.max_attempts} attempts, call manually"))

    def _cancelled(self, aid: UUID) -> bool:
        a = self.store.assignments.get(aid)
        return a is not None and a.state == AssignmentState.cancelled

    # ------------------------------------------------------------ compensation

    async def _cancel_all(self, inc: Incident, reason: str) -> None:
        for a in self.store.for_incident(inc.id):
            if a.state in SETTLED:
                continue
            self._emit(AssignmentCancelled, inc, AssignmentCancelledData(
                assignment_id=a.id, agency=a.agency, reason=reason))
            if a.state in (AssignmentState.acked, AssignmentState.en_route, AssignmentState.on_scene):
                await self._safe_cancel(a.agency, inc, f"{inc.id}:{a.agency.value}:cancel")

    async def _safe_cancel(self, agency: Agency, inc: Incident, key: str) -> None:
        try:
            await self.hub.cancel(agency, str(inc.id), key)
        except HubUnavailable:
            log.warning("could not tell %s to stand down for %s", agency.value, inc.id)
