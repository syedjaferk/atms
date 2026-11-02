"""Traffic Management. Applies a lane plan on verification and reverts exactly that plan later."""
from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from atms_contracts.models import (
    Envelope, IncidentRejected, IncidentResolved, IncidentVerified, TrafficPlanApplied, TrafficPlanAppliedData,
    TrafficPlanReverted, TrafficPlanRevertedData,
)

from .bus import InMemoryBus
from .store import Incident, Plan, Store

PRODUCER = "traffic-management"

# VMS boards upstream of each road segment, nearest first (illustrative)
VMS_BY_SEGMENT: dict[str, list[str]] = {"MH/PUNE/NH48": ["VMS-NH48-01", "VMS-NH48-02", "VMS-NH48-03"]}
# where the boards stand, for the operator map (illustrative, upstream of the cameras)
VMS_LOCATIONS: dict[str, tuple[float, float]] = {
    "VMS-NH48-01": (18.4540, 73.8508), "VMS-NH48-02": (18.4480, 73.8508), "VMS-NH48-03": (18.4420, 73.8508)}
REROUTE_BY_SEGMENT: dict[str, str] = {"MH/PUNE/NH48": "Use the service road via Katraj until further notice"}


class DeviceError(Exception):
    pass


class VmsClient(Protocol):
    async def show(self, device: str, text: str) -> None: ...
    async def clear(self, device: str) -> None: ...


class SimulatedVms:
    def __init__(self) -> None:
        self.showing: dict[str, str] = {}
        self.down: set[str] = set()

    async def show(self, device: str, text: str) -> None:
        if device in self.down:
            raise DeviceError(device)
        self.showing[device] = text

    async def clear(self, device: str) -> None:
        if device in self.down:
            raise DeviceError(device)
        self.showing.pop(device, None)


class TrafficManagement:
    def __init__(self, store: Store, bus: InMemoryBus, vms: VmsClient):
        self.store, self.bus, self.vms = store, bus, vms

    def _emit(self, cls, inc: Incident, data) -> None:
        self.store.emit(cls(producer=PRODUCER, incident_id=inc.id, trace_id=inc.trace_id, data=data))
        self.store.commit()

    async def on_event(self, ev: Envelope) -> None:
        inc = self.store.incidents.get(ev.incident_id) if ev.incident_id else None
        if inc is None:
            return
        if isinstance(ev, IncidentVerified):
            await self._apply(inc)
        elif isinstance(ev, (IncidentRejected, IncidentResolved)):
            await self._revert(inc)

    async def _apply(self, inc: Incident) -> None:
        if inc.id in self.store.plans:
            return
        lanes = sorted(inc.lanes)
        text = f"ACCIDENT AHEAD {' '.join(lanes)} CLOSED REDUCE SPEED"
        devices = VMS_BY_SEGMENT.get(inc.road_segment, [])
        plan = Plan(uuid4(), inc.id, lanes, {})
        failed: list[str] = []
        for dev in devices:
            try:
                await self.vms.show(dev, text)
                plan.messages[dev] = text
            except DeviceError:
                failed.append(dev)             # one dead board must not stop the others
        self.store.record_plan(plan)
        self._emit(TrafficPlanApplied, inc, TrafficPlanAppliedData(
            plan_id=plan.id, lanes_closed=lanes, vms_messages=plan.messages,
            reroute_advisory=REROUTE_BY_SEGMENT.get(inc.road_segment), failed_devices=failed))

    async def _revert(self, inc: Incident) -> None:
        plan = self.store.plans.get(inc.id)
        if plan is None or plan.reverted:
            return
        for dev in reversed(list(plan.messages)):
            try:
                await self.vms.clear(dev)
            except DeviceError:
                pass                           # stays on the operator task list
        plan.reverted = True
        self.store.record_plan(plan)
        self._emit(TrafficPlanReverted, inc, TrafficPlanRevertedData(plan_id=plan.id))
