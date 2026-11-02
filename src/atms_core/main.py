"""Run the core in one process: bridge thread, services, API.

    ATMS_RABBIT_HOST=localhost ATMS_AGENCIES_URL=http://localhost:9100 python -m atms_core.main

Environment
  ATMS_RABBIT_HOST / ATMS_RABBIT_USER / ATMS_RABBIT_PASSWORD   device broker (default localhost, atms, atms-dev)
  ATMS_AGENCIES_URL        agency systems (default the simulator on :9100)
  ATMS_KAFKA_BOOTSTRAP     host:9092. Services talk over Kafka and the bridge writes there. Without it, in process
  ATMS_DATABASE_URL        postgresql://user:pass@host:5433/atms. Without it state is in memory only
  ATMS_SMS_URL             SMS gateway base URL. Providers "primary" and "fallback" are paths on it. Unset records only
  ATMS_PUBLIC_URL          where gateways send receipts, default http://localhost:8000
  ATMS_RECEIPT_SECRET      shared secret for SMS delivery receipts
  ATMS_FORECAST_MODEL_DIR  model registry directory (python -m atms_forecast.train). Unset disables forecasting
  ATMS_API_PORT            default 8000
  ATMS_TOKENS              JSON {"token": {"name": "...", "agency": "police" | null}}
  ATMS_SIGNING_SECRET      evidence link signing
  ATMS_MEDIA_DIR           where clips live, evidence refs are relative to it (default simulator-out)
  ATMS_DATASET_DIR         dataset folder with videos.json and the stitched clips (default dataset)
  ATMS_VIDEO_DIR           your own recordings (default videos)
  ATMS_DETECTOR_URL        detector node, for the Analyze button on the CCTV page (for example http://localhost:8300)
  ATMS_AGENCIES_URL        also decides the stations' notice addresses
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

import httpx
import uvicorn

from atms_contracts import catalog
from atms_contracts.models import Agency, parse_event

from .api import Principal, create_api
from .bridge import Record, RabbitBridge
from .hub import HttpAgencyAdapter
from .notifications import network_contacts
from .system import build_system
from atms_contracts.topics import KAFKA_EVENTS_TOPIC

log = logging.getLogger("atms.main")

DEV_TOKENS = {
    "sensor-dev": {"name": "sensor-feed", "agency": None, "role": "sensor"},
    "ops-dev": {"name": "ops", "agency": None},
    "police-dev": {"name": "police-app", "agency": "police"},
    "ambulance-dev": {"name": "ambulance-app", "agency": "ambulance"},
    "highway-dev": {"name": "highway-app", "agency": "highway_patrol"},
    "fire-dev": {"name": "fire-app", "agency": "fire"},
    "tow-dev": {"name": "tow-app", "agency": "tow"},
}


class ThreadSafeBusSink:
    """The bridge runs in its own thread, the bus belongs to the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, bus):
        self.loop, self.bus = loop, bus

    def send(self, record: Record) -> None:
        if record.topic == KAFKA_EVENTS_TOPIC:
            self.loop.call_soon_threadsafe(self.bus.publish, parse_event(record.value))


def load_tokens() -> dict[str, Principal]:
    raw = json.loads(os.environ["ATMS_TOKENS"]) if "ATMS_TOKENS" in os.environ else DEV_TOKENS
    if "ATMS_TOKENS" not in os.environ:
        log.warning("using built-in development tokens, set ATMS_TOKENS outside development")
    return {t: Principal(v["name"], Agency(v["agency"]) if v.get("agency") else None, v.get("role", "operator"))
            for t, v in raw.items()}


def build_forecast(repo):
    """The forecast service if a model registry is configured. Readings go to Postgres when there is a database."""
    directory = os.environ.get("ATMS_FORECAST_MODEL_DIR")
    if not directory:
        log.warning("ATMS_FORECAST_MODEL_DIR not set, congestion forecasting is off")
        return None
    from atms_forecast.obs_store import MemoryObservationStore, PostgresObservationStore
    from atms_forecast.registry import Registry
    from atms_forecast.service import ForecastService

    version, model = Registry(directory).load()
    report = Registry(directory).report(version)["backtest"]
    store = PostgresObservationStore(repo) if repo is not None else MemoryObservationStore()
    log.info("forecasting with model %s", version)
    svc = ForecastService(store, model, version, backtest=report)
    sim = Registry(directory).load_sim(version)
    if sim:
        from atms_forecast.whatif import WhatIf

        svc.whatif = WhatIf(sim[0], store, validation=sim[1]["validation"])
        log.info("what-if twin loaded")
    return svc


async def serve() -> None:
    loop = asyncio.get_running_loop()
    client = httpx.AsyncClient(base_url=os.environ.get("ATMS_AGENCIES_URL", "http://localhost:9100"))
    sms_url = os.environ.get("ATMS_SMS_URL")
    extra: dict = {}
    if sms_url:
        from .notify_providers import HttpSmsProvider, WebhookSender

        callback = os.environ.get("ATMS_PUBLIC_URL", "http://localhost:8000")
        sms_client = httpx.AsyncClient(base_url=sms_url)
        secret = os.environ.get("ATMS_RECEIPT_SECRET", "dev-receipt-secret")
        extra = {
            "sms": [HttpSmsProvider(n, sms_client, callback_url=f"{callback}/v1/notifications/receipts/{n}")
                    for n in ("primary", "fallback")],
            "webhook": WebhookSender(httpx.AsyncClient()),
            "receipt_secrets": {"primary": secret, "fallback": secret},
        }
    else:
        log.warning("ATMS_SMS_URL not set, notices are recorded in memory and not sent anywhere")
    kafka = os.environ.get("ATMS_KAFKA_BOOTSTRAP")
    bus = None
    if kafka:
        from .kafka_bus import KafkaBus

        bus = KafkaBus(kafka)
        log.info("services connected through Kafka at %s", kafka)
    else:
        log.warning("ATMS_KAFKA_BOOTSTRAP not set, services talk in process only")
    url = os.environ.get("ATMS_DATABASE_URL")
    if url:
        from .pg import PostgresFacilities, PostgresRepo, migrate, seed_contacts, seed_facilities

        log.info("migrations applied: %s", migrate(url) or "none pending")
        seed_facilities(url)
        seed_contacts(url)
        repo = PostgresRepo(url)
        system = build_system(HttpAgencyAdapter(client), repo=repo, geo=PostgresFacilities(repo), bus=bus, contacts=network_contacts(), forecast=build_forecast(repo), **extra)
        log.info("loaded %d incidents from the database", len(system.store.incidents))
    else:
        log.warning("ATMS_DATABASE_URL not set, state is lost on restart")
        system = build_system(HttpAgencyAdapter(client), bus=bus, contacts=network_contacts(), forecast=build_forecast(None), **extra)
    app = create_api(system, load_tokens(), os.environ.get("ATMS_SIGNING_SECRET", "dev-secret"),
                     media_dir=os.environ.get("ATMS_MEDIA_DIR", "simulator-out"),
                     catalog=catalog.load(), detector_url=os.environ.get("ATMS_DETECTOR_URL"))

    if kafka:
        from .bridge import KafkaSink

        sink = KafkaSink(kafka)                       # the correlator group reads these straight from the topic
        KafkaBus.ensure_topics(bus)
    else:
        sink = ThreadSafeBusSink(loop, system.bus)
    bridge = RabbitBridge(sink, os.environ.get("ATMS_RABBIT_HOST", "localhost"),
                          user=os.environ.get("ATMS_RABBIT_USER", "atms"),
                          password=os.environ.get("ATMS_RABBIT_PASSWORD", "atms-dev"))
    threading.Thread(target=bridge.run, name="bridge", daemon=True).start()

    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("ATMS_API_PORT", "8000")), log_level="info")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
