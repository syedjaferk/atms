"""Shared helpers: a whole core wired to the real agency simulator running in process."""
import asyncio
import atexit
import os
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from atms_contracts.models import (
    AccidentDetected, AccidentDetectedData, Agency, Location, Severity, Site, VideoReady, VideoReadyData,
)
from atms_core.hub import CircuitBreaker, HttpAgencyAdapter
from atms_core.incident_service import Policy
from atms_core.orchestrator import DispatchConfig
from atms_core.system import build_system
from atms_core.traffic import SimulatedVms
from atms_sim.agencies import Profile, create_app

KAFKA = os.environ.get("ATMS_KAFKA")       # host:port. Set it and every Rig uses Kafka on a topic of its own
_topics: list[str] = []
_buses: list = []
DB_URL = os.environ.get("ATMS_DB_URL")     # set it and every Rig runs on Postgres in its own schema
_schemas: list[str] = []


def new_schema() -> str:
    from atms_core.pg import migrate, seed_facilities

    schema = f"t_{uuid4().hex[:10]}"
    migrate(DB_URL, schema)
    seed_facilities(DB_URL, schema)
    _schemas.append(schema)
    return schema


@atexit.register
def _drop_kafka():
    for b in _buses:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass
    if KAFKA and _topics:
        from confluent_kafka.admin import AdminClient

        admin = AdminClient({"bootstrap.servers": KAFKA})
        for f in admin.delete_topics(_topics + [t + ".dlq" for t in _topics]).values():
            try:
                f.result(10)
            except Exception:  # noqa: BLE001
                pass


@atexit.register
def _drop_schemas():
    if DB_URL and _schemas:
        from atms_core.pg import drop_schema

        for s in _schemas:
            drop_schema(DB_URL, s)


def pg_parts(schema: str):
    from atms_core.pg import PostgresFacilities, PostgresRepo

    repo = PostgresRepo(DB_URL, schema)
    return repo, PostgresFacilities(repo)


T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
GNT001 = Site(state="MH", city="PUNE", road="NH48", camera_id="GNT001")
GNT002 = Site(state="MH", city="PUNE", road="NH48", camera_id="GNT002")
LOC1, LOC2 = Location(lat=18.4575, lon=73.8508), Location(lat=18.4586, lon=73.8507)


def detection(site=GNT001, loc=LOC1, confidence=0.94, severity=Severity.major, vehicles=2, lanes=("LANE_1", "LANE_2"),
              at=T0, heading=350.0) -> AccidentDetected:
    return AccidentDetected(
        producer=f"edge/{site.camera_id}", occurred_at=at,
        data=AccidentDetectedData(site=site, location=loc, heading_deg=heading, confidence=confidence,
                                  severity=severity, vehicles_involved=vehicles, affected_lanes=list(lanes),
                                  model_version="test"))


def video(camera="GNT001") -> VideoReady:
    return VideoReady(producer=f"edge/{camera}", data=VideoReadyData(
        camera_id=camera, video_ref=f"clips/{camera}.mp4", duration_s=60, size_bytes=10, sha256="b" * 64))


class Rig:
    """System plus the agency simulator behind it. Per agency behaviour is forced with a header."""

    def __init__(self, behaviors=None, timeout_s=0.2, policy=None, dispatch=None, schema=None,
                 sms=None, webhook=None, notify=None, contacts=None, receipt_secrets=None, clock=None, forecast=None):
        self.behaviors = behaviors or {}              # agency -> list of behaviors, consumed per call
        profiles = {a: Profile(ack_delay_s=(0, 0), timeout_sleep_s=1.0, p_decline=0, p_timeout=0) for a in Agency}
        self.agency_app = create_app(seed=1, profiles=profiles)
        rig = self

        class Forcing(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.ASGITransport(app=rig.agency_app)

            async def handle_async_request(self, request):
                agency = request.url.path.split("/")[3]
                queue = rig.behaviors.get(agency)
                if queue and request.url.path.endswith("/dispatch"):
                    request.headers["X-Sim-Behavior"] = queue.pop(0) if len(queue) > 1 else queue[0]
                # ASGITransport ignores timeouts, so enforce the read timeout here like a real network would
                limit = request.extensions.get("timeout", {}).get("read")
                try:
                    return await asyncio.wait_for(self.inner.handle_async_request(request), limit)
                except asyncio.TimeoutError as exc:
                    raise httpx.ReadTimeout("simulated read timeout", request=request) from exc

        self.client = httpx.AsyncClient(transport=Forcing(), base_url="http://agencies")
        self.hub = HttpAgencyAdapter(self.client, default_timeout_s=timeout_s, breaker=CircuitBreaker(threshold=50))
        self.vms = SimulatedVms()
        self.policy, self.dispatch = policy, dispatch or DispatchConfig()
        self.sms, self.webhook, self.notify, self.contacts = sms, webhook, notify, contacts
        self.receipt_secrets, self.clock, self.forecast = receipt_secrets, clock, forecast
        self.schema = schema or (new_schema() if DB_URL else None)
        self._build()

    def _build(self):
        repo, geo = pg_parts(self.schema) if self.schema else (None, None)
        bus = None
        if KAFKA:
            from atms_core.kafka_bus import KafkaBus

            if not hasattr(self, "topic"):
                self.topic = f"atms-test-{uuid4().hex[:10]}"
                _topics.append(self.topic)
            # same topic and group names across restarts, so a restarted service resumes from its offsets
            bus = KafkaBus(KAFKA, self.topic, self.topic + ".dlq", group_prefix=self.topic + getattr(self, "suffix", ""),
                           partitions=3)
            _buses.append(bus)
        extra = {} if self.clock is None else {"clock": self.clock}
        self.system = build_system(self.hub, self.vms, self.policy, self.dispatch, repo=repo, geo=geo, bus=bus,
                                   sms=self.sms, webhook=self.webhook, notify=self.notify, contacts=self.contacts,
                                   receipt_secrets=self.receipt_secrets, forecast=self.forecast, **extra)
        self.store, self.service = self.system.store, self.system.service

    async def replay_from_start(self):
        """New consumer groups on the same topic and database: every past event is handled again."""
        total = len(self.system.bus.log)
        await self.system.stop()
        self.suffix = getattr(self, "suffix", "") + ".replay"
        self._build()
        self.system.bus.start()
        await self.system.bus.ready()
        end = asyncio.get_running_loop().time() + 40
        while not all(g.consumed >= total for g in self.system.bus._groups if not g.broadcast):
            assert asyncio.get_running_loop().time() < end, "replay did not finish"
            await asyncio.sleep(0.05)
        await self.system.settle()
        return total

    async def restart(self):
        """Throw the process state away and start again from the database, as a crash and restart would."""
        await self.system.stop()
        self._build()

    async def feed(self, *events):
        for e in events:
            self.system.ingest(e)
        await self.system.settle()

    def incident(self):
        assert len(self.store.incidents) == 1, f"expected one incident, got {len(self.store.incidents)}"
        return next(iter(self.store.incidents.values()))

    def assignments(self):
        return {a.agency: a for a in self.store.assignments.values()}

    def event_types(self):
        return [e.type for e in self.system.bus.log]


def run(coro):
    """Run one async test body, then close every Kafka bus it created so consumers do not pile up."""
    before = len(_buses)
    try:
        return asyncio.run(coro)
    finally:
        for bus in _buses[before:]:
            try:
                bus.close()
            except Exception:  # noqa: BLE001
                pass
        del _buses[before:]
