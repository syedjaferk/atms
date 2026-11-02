import json
from datetime import timedelta

import httpx
import pandas as pd
import pytest

from atms_contracts.models import Agency
from atms_core.api import Principal, create_api
from atms_forecast.obs_store import MemoryObservationStore, PostgresObservationStore
from atms_forecast.service import ForecastService

from conftest import DB_URL, Rig, new_schema, pg_parts, run
from test_forecast import NOW, SEG, data, feed, incident_end, model, peak_end, split  # noqa: F401  (fixtures)

TOKENS = {"ops": Principal("op1"), "sensor": Principal("sensor-feed", None, "sensor"),
          "police": Principal("police-app", Agency.police)}
H = lambda t: {"Authorization": f"Bearer {t}"}


def readings_payload(data, end, minutes=180):
    w = data[(data.ts > end - pd.Timedelta(minutes=minutes)) & (data.ts <= end)]
    shift = pd.Timestamp(NOW) - end
    return ({"items": [{"segment": r.segment, "ts": (r.ts + shift).isoformat(), "speed_kmh": r.speed_kmh, "flow_vph": r.flow_vph}
                       for r in w.itertuples()]},
            {"items": [{"ts": (r.ts + shift).isoformat(), "rain_mm_h": r.rain_mm_h, "visibility_km": r.visibility_km}
                       for r in w.drop_duplicates("ts").itertuples()]})


async def build(model, store=None):
    svc = ForecastService(store or MemoryObservationStore(), model, "test-v1", clock=lambda: NOW,
                          backtest={"horizons": {"15": {"mae": {"model": 2.2}}}})
    r = Rig(forecast=svc)
    app = create_api(r.system, TOKENS, "s", run_bus=False)
    return r, svc, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms")


def test_sensors_post_readings_then_anyone_signed_in_reads_the_forecast(model, data):
    async def go():
        r, svc, http = await build(model)
        readings, weather = readings_payload(data, peak_end(data))
        assert (await http.post("/v1/traffic/readings", json=readings)).status_code == 401
        assert (await http.post("/v1/traffic/readings", json=readings, headers=H("police"))).status_code == 403
        assert (await http.get(f"/v1/forecast/{SEG}", headers=H("sensor"))).status_code == 503     # no readings yet, refuses to guess
        assert (await http.post("/v1/traffic/weather", json=weather, headers=H("sensor"))).json()["accepted"] == len(weather["items"])
        res = await http.post("/v1/traffic/readings", json=readings, headers=H("sensor"))
        assert res.status_code == 200 and res.json()["accepted"] == len(readings["items"]) and res.json()["rejected"] == []

        f = await http.get(f"/v1/forecast/{SEG}", headers=H("police"))
        body = f.json()
        assert f.status_code == 200 and [x["horizon_min"] for x in body["forecasts"]] == [15, 30, 60]
        assert all(x["why"] for x in body["forecasts"]) and body["model_version"] == "test-v1"
        one = (await http.get(f"/v1/forecast/{SEG}?horizons=30", headers=H("ops"))).json()
        assert [x["horizon_min"] for x in one["forecasts"]] == [30]
        assert (await http.get("/v1/forecast/NH99-S01", headers=H("ops"))).status_code == 404
        assert (await http.get(f"/v1/forecast/{SEG}?horizons=abc", headers=H("ops"))).status_code == 422
        assert (await http.get(f"/v1/forecast/{SEG}?lanes_closed=9", headers=H("ops"))).status_code == 422

        slow = (await http.get(f"/v1/forecast/{SEG}?horizons=30&lanes_closed=2", headers=H("ops"))).json()
        assert slow["what_if"] == {"lanes_closed": 2}
        assert slow["forecasts"][0]["speed_kmh"] < one["forecasts"][0]["speed_kmh"] - 3

        info = (await http.get("/v1/forecast/model", headers=H("ops"))).json()
        assert info["version"] == "test-v1" and info["horizons_min"] == [15, 30, 60] and "NH48-S03" in info["segments"]
        overview = (await http.get("/v1/forecast", headers=H("ops"))).json()
        assert len(overview["items"]) == 6 and all(i["status"] == "ok" for i in overview["items"])
        acc = (await http.get("/v1/forecast/accuracy", headers=H("ops"))).json()
        assert acc["horizons"] == {} and acc["drift"] is False
    run(go())


def test_bad_readings_are_rejected_one_by_one_not_the_whole_batch(model, data):
    async def go():
        r, svc, http = await build(model)
        good = {"segment": SEG, "ts": (NOW - timedelta(minutes=5)).isoformat(), "speed_kmh": 80, "flow_vph": 3000}
        batch = {"items": [good,
                           {**good, "segment": "NH99-S01"},
                           {**good, "ts": (NOW + timedelta(hours=1)).isoformat()},
                           {**good, "ts": (NOW - timedelta(days=30)).isoformat()}]}
        res = (await http.post("/v1/traffic/readings", json=batch, headers=H("sensor"))).json()
        assert res["accepted"] == 1 and [x["reason"] for x in res["rejected"]] == [
            "unknown segment", "timestamp outside the accepted window", "timestamp outside the accepted window"]
        for bad in ({**good, "speed_kmh": -5}, {**good, "speed_kmh": 900}, {**good, "flow_vph": -1}):
            assert (await http.post("/v1/traffic/readings", json={"items": [bad]}, headers=H("sensor"))).status_code == 422
        too_many = {"items": [good] * 5001}
        assert (await http.post("/v1/traffic/readings", json=too_many, headers=H("sensor"))).status_code == 422
        assert len(svc.store.obs) == 1
    run(go())


def test_a_heavy_forecast_becomes_an_event_on_the_bus_and_reaches_the_audit_log(model, data):
    async def go():
        r, svc, http = await build(model)
        end, age = incident_end(data)
        feed(svc.store, data, NOW, source_end=end)
        svc.incidents = lambda: {SEG: (2, age)}
        alerts = svc.run_cycle(NOW)
        await r.system.settle()
        assert alerts and "congestion.forecast" in r.event_types()
        ev = next(e for e in r.system.bus.log if e.type == "congestion.forecast")
        assert ev.data.segment_id == SEG and ev.incident_id is None and ev.producer == "forecast-service"
        assert any(a["type"] == "congestion.forecast" for a in r.store.audit)
        assert not r.system.bus.dead
    run(go())


@pytest.mark.skipif(not DB_URL, reason="ATMS_DB_URL not set")
def test_postgres_store_gives_the_same_forecasts_and_scores_as_memory(model, data):
    async def go():
        schema = new_schema()
        repo, _ = pg_parts(schema)
        pg_store, mem_store = PostgresObservationStore(repo), MemoryObservationStore()
        end = peak_end(data)
        for st in (pg_store, mem_store):
            feed(st, data, NOW, source_end=end)
        a = ForecastService(pg_store, model, "v", clock=lambda: NOW).forecast(SEG)
        b = ForecastService(mem_store, model, "v", clock=lambda: NOW).forecast(SEG)
        assert [(x["speed_kmh"], x["level"]) for x in a["forecasts"]] == [(x["speed_kmh"], x["level"]) for x in b["forecasts"]]
        assert a["current_speed_kmh"] == b["current_speed_kmh"] and a["inputs"]["rain_now_mm_h"] == b["inputs"]["rain_now_mm_h"]

        svc = ForecastService(pg_store, model, "v", clock=lambda: NOW)
        svc.run_cycle(NOW)
        svc.run_cycle(NOW)                                            # the same cycle twice does not double the log
        import psycopg
        with psycopg.connect(DB_URL, options=f"-c search_path={schema},public", autocommit=True) as c:
            assert c.execute("select count(*) from forecast_log").fetchone()[0] == 18
            shift = pd.Timestamp(NOW) - end
            later = data[(data.ts > end) & (data.ts <= end + pd.Timedelta(minutes=70))]
            pg_store.add_observations([{"segment": x.segment, "ts": (x.ts + shift).to_pydatetime(), "speed_kmh": x.speed_kmh,
                                        "flow_vph": x.flow_vph} for x in later.itertuples()])
            assert svc.score_pending(NOW + timedelta(minutes=70)) == 18
            assert c.execute("select count(*) from forecast_log where actual is not null and abs_error is not null").fetchone()[0] == 18
        acc = svc.accuracy(24, NOW + timedelta(minutes=70), min_samples=1)
        assert acc["horizons"]["60"]["samples"] == 6
        pg_store.add_observations([{"segment": SEG, "ts": NOW, "speed_kmh": 50.0, "flow_vph": 1.0}])           # upsert, not duplicate
        pg_store.add_observations([{"segment": SEG, "ts": NOW, "speed_kmh": 51.0, "flow_vph": 2.0}])
        assert pg_store.speed_at(SEG, NOW) == 51.0
    run(go())
