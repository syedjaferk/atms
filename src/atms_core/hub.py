"""Integration Hub. One adapter interface per agency system, with a circuit breaker.

The HTTP adapter speaks to the agency simulator today. A real agency gets its own adapter
class with the same two methods.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from atms_contracts.models import Agency, Location, Severity


class HubTimeout(Exception):
    """The agency did not answer in time. The request may still have been processed."""


class HubUnavailable(Exception):
    """The agency system is down or its circuit is open."""


@dataclass(frozen=True)
class DispatchResult:
    accepted: bool
    unit_ref: str | None = None
    eta_s: int | None = None
    reason: str | None = None


class AgencyAdapter(Protocol):
    async def dispatch(self, agency: Agency, incident_id: str, key: str, severity: Severity,
                       location: Location, facility: str | None) -> DispatchResult: ...

    async def cancel(self, agency: Agency, incident_id: str, key: str) -> None: ...


@dataclass
class CircuitBreaker:
    threshold: int = 3
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: dict[Agency, int] = field(default_factory=dict)
    _opened_at: dict[Agency, float] = field(default_factory=dict)

    def allow(self, agency: Agency) -> bool:
        opened = self._opened_at.get(agency)
        if opened is None:
            return True
        if self.clock() - opened >= self.cooldown_s:      # half open, let one through
            del self._opened_at[agency]
            self._failures[agency] = self.threshold - 1
            return True
        return False

    def success(self, agency: Agency) -> None:
        self._failures[agency] = 0
        self._opened_at.pop(agency, None)

    def failure(self, agency: Agency) -> None:
        self._failures[agency] = self._failures.get(agency, 0) + 1
        if self._failures[agency] >= self.threshold:
            self._opened_at[agency] = self.clock()


class HttpAgencyAdapter:
    def __init__(self, client: httpx.AsyncClient, timeouts_s: dict[Agency, float] | None = None,
                 default_timeout_s: float = 60.0, breaker: CircuitBreaker | None = None):
        self.client, self.timeouts_s, self.default_timeout_s = client, timeouts_s or {}, default_timeout_s
        self.breaker = breaker or CircuitBreaker()

    def _timeout(self, agency: Agency) -> float:
        return self.timeouts_s.get(agency, self.default_timeout_s)

    async def dispatch(self, agency, incident_id, key, severity, location, facility) -> DispatchResult:
        if not self.breaker.allow(agency):
            raise HubUnavailable(f"circuit open for {agency.value}")
        body = {"incident_id": incident_id, "idempotency_key": key, "severity": severity.value,
                "location": location.model_dump(), "facility": facility}
        try:
            resp = await self.client.post(f"/v1/agencies/{agency.value}/dispatch", json=body,
                                          timeout=self._timeout(agency))
        except httpx.TimeoutException as exc:
            self.breaker.failure(agency)
            raise HubTimeout(agency.value) from exc
        except httpx.TransportError as exc:
            self.breaker.failure(agency)
            raise HubUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            self.breaker.failure(agency)
            raise HubUnavailable(f"{agency.value} answered {resp.status_code}")
        self.breaker.success(agency)               # a decline is a healthy answer
        data = resp.json()
        if resp.status_code == 200 and data.get("accepted"):
            return DispatchResult(True, data["unit_ref"], data["eta_s"])
        return DispatchResult(False, reason=data.get("reason", "declined"))

    async def cancel(self, agency, incident_id, key) -> None:
        try:
            await self.client.post(f"/v1/agencies/{agency.value}/cancel",
                                   json={"incident_id": incident_id, "idempotency_key": key},
                                   timeout=self._timeout(agency))
        except httpx.HTTPError as exc:
            raise HubUnavailable(str(exc)) from exc
