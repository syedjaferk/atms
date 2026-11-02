import json
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from atms_core.api import Principal, create_api
from atms_forecast.calibrate import fit, load, save, validate
from atms_forecast.obs_store import MemoryObservationStore
from atms_forecast.registry import Registry
from atms_forecast.service import ForecastService
from atms_forecast.twin import (DemandSpec, IncidentSpec, Params, Responses, Scenario, StartState, WeatherSpec, kpis, simulate)
from atms_forecast.whatif import WhatIf, WhatIfError, validate_scenario

from conftest import Rig, detection, run
from test_forecast import NOW, SEG, data, feed, model, peak_end, split  # noqa: F401  (fixtures and helpers)

H = lambda t: {"Authorization": f"Bearer {t}"}
TOKENS = {"ops": Principal("op1"), "sensor": Principal("sensor-feed", None, "sensor")}
EVENING = datetime(2026, 11, 3, 11, 30, tzinfo=timezone.utc)        # 17:00 local on a Tuesday, as a typical day


@pytest.fixture(scope="module")
def twin_pack(data):
    cut = data.ts.max() - pd.Timedelta(days=7)
    twin, info = fit(data[data.ts < cut], max_nfev=40)
    return twin, info, cut


@pytest.fixture(scope="module")
def twin(twin_pack):
    return twin_pack[0]


def vh(twin, sc, start=EVENING, horizon=240):
    return kpis(twin.run(sc, StartState(start), horizon), twin.free_flow, None)["vehicle_hours_lost"]


# ------------------------------------------------------------------ is the twin any good


def test_on_days_it_never_saw_the_twin_beats_the_typical_day_overall_and_by_a_mile_during_incidents(twin_pack, data):
    twin, _info, cut = twin_pack
    rep = validate(twin, data[data.ts >= cut - pd.Timedelta(days=1)])
    whole = rep["whole_day_from_midnight"]
    assert whole["mae_twin"] < whole["mae_typical"]
    assert whole["mae_twin_during_incidents"] < 0.6 * whole["mae_typical_during_incidents"]
    for h in ("1", "2"):
        r = rep["from_start_state"][h]
        assert r["mae_twin"] < r["mae_keep_current"], h
        assert r["mae_twin_incident"] < r["mae_typical_incident"], h


def test_fitted_numbers_are_physical_and_survive_saving(twin_pack, tmp_path):
    twin, info, _ = twin_pack
    p = twin.p
    assert 1500 <= p.cap_per_lane <= 2600 and 0 <= p.spill <= 1 and 0.7 <= p.discharge <= 1.0 and p.alpha_down > p.alpha_up
    assert all(0.6 <= s <= 1.5 for s in p.seg_scale)
    save(twin, info, {"note": "x"}, tmp_path / "sim.json")
    again, meta = load(tmp_path / "sim.json")
    sc = Scenario("x", [IncidentSpec(SEG, 20, 45, 2)])
    a = twin.run(sc, StartState(EVENING), 180)["speed"]
    b = again.run(sc, StartState(EVENING), 180)["speed"]
    np.testing.assert_allclose(a, b, atol=0.05)
    assert meta["validation"] == {"note": "x"}


# ------------------------------------------------------------------ the physics behaves


def test_a_normal_evening_is_slower_than_a_normal_night(twin):
    run = twin.run(Scenario("n"), StartState(datetime(2026, 11, 3, 18, 30, tzinfo=timezone.utc)), 120)        # midnight local
    peak = twin.run(Scenario("n"), StartState(EVENING), 120)
    assert run["speed"].mean() > peak["speed"].mean() + 5


def test_more_lanes_closed_and_longer_closures_cost_more(twin):
    base = vh(twin, Scenario("none"))
    one = vh(twin, Scenario("1", [IncidentSpec(SEG, 15, 45, 1)]))
    two = vh(twin, Scenario("2", [IncidentSpec(SEG, 15, 45, 2)]))
    longer = vh(twin, Scenario("long", [IncidentSpec(SEG, 15, 90, 2)]))
    assert base < one < two < longer


def test_a_queue_grows_backwards_onto_the_segments_behind_the_incident(twin):
    run = twin.run(Scenario("i", [IncidentSpec("NH48-S04", 10, 60, 2)]), StartState(EVENING), 180)
    base = twin.run(Scenario("n"), StartState(EVENING), 180)
    i = {s: k for k, s in enumerate(run["ids"])}
    drop = lambda seg: (base["speed"][:, i[seg]] - run["speed"][:, i[seg]]).max()
    assert drop("NH48-S04") > 30 and drop("NH48-S03") > 8 and drop("NH48-S06") < 3        # behind it yes, ahead of it no


def test_rain_and_extra_traffic_make_things_worse_and_less_traffic_better(twin):
    base = vh(twin, Scenario("n"))
    assert vh(twin, Scenario("rain", weather=[WeatherSpec(0, 240, 10)])) >= base
    assert vh(twin, Scenario("match", demand=[DemandSpec(0, 240, 1.25)])) > base + 20
    assert vh(twin, Scenario("quiet", demand=[DemandSpec(0, 240, 0.8)])) < base
    one_seg = vh(twin, Scenario("one", demand=[DemandSpec(0, 240, 1.4, SEG)]))
    assert base < one_seg < vh(twin, Scenario("all", demand=[DemandSpec(0, 240, 1.4)]))


def test_each_response_helps_and_none_makes_things_worse(twin):
    inc = [IncidentSpec(SEG, 15, 60, 2)]
    plain = vh(twin, Scenario("plain", inc))
    fast = vh(twin, Scenario("fast", inc, responses=Responses(clearance_speedup=0.3)))
    shoulder = vh(twin, Scenario("sh", inc, responses=Responses(shoulder_lane=True)))
    divert = vh(twin, Scenario("div", inc, responses=Responses(diversion_share=0.15)))
    both = vh(twin, Scenario("all", inc, responses=Responses(clearance_speedup=0.3, shoulder_lane=True, diversion_share=0.15)))
    assert fast < plain and shoulder < plain and divert < plain
    assert both <= min(fast, shoulder, divert) + 1e-6


def test_many_scenarios_at_once_give_the_same_answers_as_one_at_a_time(twin):
    sc = [Scenario("a", [IncidentSpec(SEG, 10, 40, 1)]), Scenario("b", [IncidentSpec("NH48-S02", 30, 60, 2)], demand=[DemandSpec(0, 120, 1.2)])]
    state = StartState(EVENING)
    singles = [twin.run(s, state, 150) for s in sc]
    built = [twin.build(s, state, 150) for s in sc]
    stack = lambda k: np.concatenate([b[k] for b in built])
    speed, _f, _q = simulate(twin.p, stack(0), stack(1), stack(2), twin.lanes, twin.free_flow, stack(4), stack(3), stack(5))
    for k, r in enumerate(singles):
        np.testing.assert_allclose(speed[k], r["speed"], atol=1e-9)


def test_speed_never_drops_to_a_standstill(twin):
    run = twin.run(Scenario("worst", [IncidentSpec(SEG, 0, 120, 3)], demand=[DemandSpec(0, 240, 1.5)]), StartState(EVENING), 240)
    assert run["speed"].min() >= 4.0 and np.isfinite(run["speed"]).all()


# ------------------------------------------------------------------ where it starts


def live_whatif(twin, data, model_):
    store = MemoryObservationStore()
    feed(store, data, NOW, source_end=peak_end(data))
    return WhatIf(twin, store, clock=lambda: NOW), store


def test_it_starts_from_the_road_as_it_is_now_when_readings_are_fresh_else_from_a_typical_day(twin, data, model):
    wi, store = live_whatif(twin, data, model)
    state, info = wi.start_state(None)
    assert info["mode"] == "live" and len(info["speeds_kmh"]) == 6 and 0.7 <= info["demand_against_typical"] <= 1.3
    last = {s: float(store.recent(s, NOW - timedelta(minutes=30), NOW).speed_kmh.iloc[-3:].mean()) for s in twin.ids}
    first = twin.run(Scenario("n"), state, 60)["speed"][0]
    np.testing.assert_allclose(first, [last[s] for s in twin.ids], atol=12)               # the first step is near the readings
    _, future = wi.start_state(NOW + timedelta(days=2))
    assert future["mode"] == "typical day" and future["speeds_kmh"] is None
    stale = WhatIf(twin, MemoryObservationStore(), clock=lambda: NOW)
    assert stale.start_state(None)[1]["mode"] == "typical day"


# ------------------------------------------------------------------ comparing and advising


def test_scenarios_are_compared_with_a_normal_day_and_ranked(twin, data, model):
    wi, _ = live_whatif(twin, data, model)
    res = wi.compare([Scenario("bad", [IncidentSpec(SEG, 10, 60, 2)]),
                      Scenario("handled", [IncidentSpec(SEG, 10, 60, 2)], responses=Responses(clearance_speedup=0.3, shoulder_lane=True))], 180)
    b = res["baseline"]["kpis"]["vehicle_hours_lost"]
    bad, handled = res["scenarios"]
    assert bad["vs_normal_day"]["extra_vehicle_hours_lost"] == pytest.approx(bad["kpis"]["vehicle_hours_lost"] - b, abs=0.11)
    assert res["lowest_delay"] == "handled" and handled["kpis"]["vehicle_hours_lost"] < bad["kpis"]["vehicle_hours_lost"]
    # NOW is a Sunday evening, traffic is light, so closing two lanes costs delay but does not jam the road
    assert bad["vs_normal_day"]["extra_vehicle_hours_lost"] > 0 and bad["kpis"]["minutes_to_recover_after_clearance"] is not None
    assert len(bad["series"]["speed_kmh"]) == 180 // 15 and bad["series"]["segments"] == twin.ids
    assert res["model_check"] is None and res["start"]["mode"] == "live"


def test_a_scenario_that_makes_no_sense_is_refused():
    def bad(**kw):
        with pytest.raises(WhatIfError):
            validate_scenario(Scenario("x", **kw), kw.pop("h", 240) if False else 240)
    bad(incidents=[IncidentSpec("NH99-S01", 0, 30, 1)])
    bad(incidents=[IncidentSpec(SEG, 0, -5, 1)])
    bad(incidents=[IncidentSpec(SEG, 0, 30, 4)])
    bad(demand=[DemandSpec(0, 30, 9.0)])
    bad(demand=[DemandSpec(0, 30, 1.2, "NH99-S01")])
    with pytest.raises(WhatIfError):
        validate_scenario(Scenario("x"), 5000)
    with pytest.raises(WhatIfError):
        validate_scenario(Scenario("x", responses=Responses(clearance_speedup=0.95)), 240)


def test_incident_options_rank_the_ways_of_handling_it_and_price_the_delay(twin, data, model):
    wi, _ = live_whatif(twin, data, model)
    res = wi.incident_options(SEG, 2, "major", elapsed_min=10)
    names = [s["name"] for s in res["scenarios"]]
    assert names[0] == "as things stand" and len(names) == 6
    saves = {s["name"]: s["saves_vs_as_things_stand"]["vehicle_hours"] for s in res["scenarios"]}
    assert saves["as things stand"] == 0 and max(saves.values()) > 0 and saves["everything, and clear 30 percent faster"] >= saves["clear 30 percent faster"]
    assert res["recommendation"]["option"] != "as things stand" and res["recommendation"]["saves_vehicle_hours"] > 0
    assert res["cost_of_delay"]["each_extra_10_min_closed_costs_vehicle_hours"] > 0
    assert res["assumptions"]["expected_minutes_remaining"] == 50 and "major" in res["assumptions"]["basis"]
    late = wi.incident_options(SEG, 2, "major", elapsed_min=58)
    assert late["assumptions"]["expected_minutes_remaining"] == 15                 # never below fifteen


# ------------------------------------------------------------------ the API


async def api(model, twin, data, with_twin=True):
    store = MemoryObservationStore()
    feed(store, data, NOW, source_end=peak_end(data))
    svc = ForecastService(store, model, "v", clock=lambda: NOW)
    if with_twin:
        svc.whatif = WhatIf(twin, store, clock=lambda: NOW, validation={"note": "checked"})
    r = Rig(forecast=svc)
    app = create_api(r.system, TOKENS, "s", run_bus=False)
    return r, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://atms")


def test_whatif_endpoint_runs_scenarios_and_refuses_nonsense(model, twin, data):
    async def go():
        r, http = await api(model, twin, data)
        body = {"horizon_min": 120, "scenarios": [
            {"name": "event traffic", "demand": [{"at_min": 0, "duration_min": 120, "factor": 1.3}]},
            {"name": "closure", "incidents": [{"segment": SEG, "at_min": 10, "duration_min": 45, "lanes_closed": 2}],
             "responses": {"shoulder_lane": True}}]}
        assert (await http.post("/v1/whatif", json=body)).status_code == 401
        res = await http.post("/v1/whatif", json=body, headers=H("ops"))
        out = res.json()
        assert res.status_code == 200 and [s["name"] for s in out["scenarios"]] == ["event traffic", "closure"]
        assert out["model_check"] == {"note": "checked"} and out["start"]["mode"] == "live"
        assert out["scenarios"][0]["vs_normal_day"]["extra_vehicle_hours_lost"] > 0
        for bad in ({"scenarios": []}, {"scenarios": [{"name": "x"}] * 9}, {"horizon_min": 5, "scenarios": [{"name": "x"}]},
                    {"scenarios": [{"name": "x", "responses": {"clearance_speedup": 0.99}}]},
                    {"scenarios": [{"name": "x", "incidents": [{"segment": SEG, "duration_min": 30, "lanes_closed": 9}]}]}):
            assert (await http.post("/v1/whatif", json=bad, headers=H("ops"))).status_code == 422
        unknown = {"scenarios": [{"name": "x", "incidents": [{"segment": "NH99-S01", "duration_min": 30}]}]}
        assert (await http.post("/v1/whatif", json=unknown, headers=H("ops"))).status_code == 422
    run(go())


def test_without_a_twin_the_endpoints_say_so(model, twin, data):
    async def go():
        r, http = await api(model, twin, data, with_twin=False)
        assert (await http.post("/v1/whatif", json={"scenarios": [{"name": "x"}]}, headers=H("ops"))).status_code == 503
    run(go())


def test_live_incident_options_endpoint(model, twin, data):
    async def go():
        r, http = await api(model, twin, data)
        await r.feed(detection())                                      # a major accident at the camera on NH48-S03
        iid = str(r.incident().id)
        assert (await http.post(f"/v1/incidents/{iid}/whatif", headers=H("sensor"))).status_code == 403
        assert (await http.post("/v1/incidents/00000000-0000-0000-0000-000000000000/whatif", headers=H("ops"))).status_code == 404
        res = await http.post(f"/v1/incidents/{iid}/whatif", headers=H("ops"))
        out = res.json()
        assert res.status_code == 200 and out["assumptions"]["segment"] == SEG and out["assumptions"]["lanes_closed"] == 2
        assert out["recommendation"]["option"] and len(out["scenarios"]) == 6
        r.incident().location = type(r.incident().location)(lat=19.5, lon=73.0)           # nowhere near the road
        assert (await http.post(f"/v1/incidents/{iid}/whatif", headers=H("ops"))).status_code == 422
    run(go())


# ------------------------------------------------------------------ registry and training


def test_the_twin_is_registered_with_the_model_and_checked_on_days_it_never_saw(tmp_path):
    from atms_forecast.synth import generate
    from atms_forecast.train import run as train

    df = generate(seed=3, days=35)
    reg = Registry(tmp_path / "reg")
    res = train(df, reg, test_days=7, rounds=60, sim=True, sim_nfev=25)
    assert res["promoted"] and res["twin_check"]["days"] >= 7
    pack = reg.load_sim()
    assert pack is not None
    twin, meta = pack
    assert meta["validation"]["whole_day_from_midnight"]["mae_twin"] == res["twin_check"]["whole_day_from_midnight"]["mae_twin"]
    assert meta["fit"]["days"] > res["twin_check"]["days"]                       # the kept twin used everything
    assert twin.run(Scenario("x"), StartState(EVENING), 60)["speed"].shape == (12, 6)
    assert Registry(tmp_path / "empty").load_sim() is None
