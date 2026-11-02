from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from atms_forecast.evaluate import split_by_time
from atms_forecast.features import FEATURES, HORIZONS, HistProfile, build, level
from atms_forecast.model import CongestionModel
from atms_forecast.obs_store import MemoryObservationStore
from atms_forecast.registry import Registry, decide
from atms_forecast.segments import LOCAL_TZ, SEGMENTS
from atms_forecast.service import ForecastService, NoModel, StaleData, UnknownSegment
from atms_forecast.synth import generate

SEG = "NH48-S03"


@pytest.fixture(scope="module")
def data():
    return generate(seed=7, days=42)


@pytest.fixture(scope="module")
def split(data):
    cutoff = data.ts.max() - pd.Timedelta(days=7)
    return data[data.ts < cutoff - pd.Timedelta(minutes=60)], data[data.ts >= cutoff - pd.Timedelta(minutes=70)], cutoff


@pytest.fixture(scope="module")
def model(split):
    return CongestionModel.fit(split[0], rounds=120, calibration_days=5)


# ------------------------------------------------------------------ the synthetic world has real structure


def test_generator_is_deterministic_and_has_rush_hours_incidents_and_rain(data):
    again = generate(seed=7, days=42)
    pd.testing.assert_frame_equal(data, again)
    assert not data.equals(generate(seed=8, days=42))
    local = data.ts.dt.tz_convert(LOCAL_TZ)
    weekday = data[local.dt.dayofweek < 5]
    wl = local[local.dt.dayofweek < 5]
    night = weekday[wl.dt.hour == 3].speed_kmh.mean()
    evening = weekday[(wl.dt.hour == 17) & (wl.dt.minute >= 30)].speed_kmh.mean()
    assert night > 90 and evening < night - 15
    assert data[data.incident_lanes_closed > 0].speed_kmh.mean() < data[data.incident_lanes_closed == 0].speed_kmh.mean() - 15
    dry_peak = data[(data.rain_mm_h < 0.1) & (data.incident_lanes_closed == 0)]
    wet_peak = data[(data.rain_mm_h > 5) & (data.incident_lanes_closed == 0)]
    assert wet_peak.speed_kmh.mean() < dry_peak.speed_kmh.mean() - 3


def test_an_incident_pushes_a_queue_back_onto_the_segment_upstream(data):
    up, down = "NH48-S02", "NH48-S03"
    d = data.set_index(["segment", "ts"])
    hit = data[(data.segment == down) & (data.incident_lanes_closed == 2) & (data.incident_age_min >= 25)]
    assert len(hit) > 20
    ts = hit.ts.values
    after = d.loc[up].reindex(pd.DatetimeIndex(ts, tz="UTC") + pd.Timedelta(minutes=15)).speed_kmh.dropna()
    normal = d.loc[up].speed_kmh.mean()
    assert after.mean() < normal - 3


# ------------------------------------------------------------------ no cheating


def test_features_at_time_t_do_not_change_when_the_future_changes(data, split):
    train = split[0]
    hist = HistProfile.fit(train)
    ff = train.groupby("segment").speed_kmh.quantile(0.95).to_dict()
    t = data[data.segment == SEG].ts.iloc[3000]
    past = data[data.ts <= t]
    tampered = data.copy()
    tampered.loc[tampered.ts > t, ["speed_kmh", "flow_vph", "incident_lanes_closed"]] = [5.0, 0.0, 3]
    a = build(past, 30, hist, ff, seed=None).query("segment == @SEG").loc[t]
    b = build(tampered[tampered.ts <= t + pd.Timedelta(hours=3)], 30, hist, ff, seed=None).query("segment == @SEG").loc[t]
    known_now = [f for f in FEATURES if f not in ("rain_at_target",)]
    pd.testing.assert_series_equal(a[known_now].astype(float), b[known_now].astype(float), check_names=False)
    assert pd.isna(a["target"]) and not pd.isna(b["target"])          # only the target looks ahead


def test_typical_speed_profile_comes_from_training_data_only(data, split):
    train, _test, cutoff = split
    a = HistProfile.fit(train)
    altered = data.copy()
    altered.loc[altered.ts >= cutoff, "speed_kmh"] = 1.0
    b = HistProfile.fit(altered[altered.ts < cutoff - pd.Timedelta(minutes=60)])
    assert a.table == b.table


def test_training_never_reaches_into_the_test_period(data):
    train, test = split_by_time(data, test_days=7)
    cutoff = data.ts.max() - pd.Timedelta(days=7)
    assert train.ts.max() <= cutoff - pd.Timedelta(minutes=60) and test.ts.min() < cutoff


# ------------------------------------------------------------------ the model


def holdout(model, split, horizon):
    train, test, cutoff = split
    X = build(test, horizon, model.hist, model.free_flow, seed=1)
    X = X[(X.index >= cutoff) & X.target.notna()].dropna(subset=FEATURES[:6])
    return X


@pytest.mark.parametrize("h", [15, 30, 60])
def test_model_beats_both_naive_baselines_on_days_it_never_saw(model, split, h):
    X = holdout(model, split, h)
    y = X.target.to_numpy()
    mae = lambda p: float(np.abs(np.asarray(p) - y).mean())
    m = mae(model.predict(X, h).speed)
    assert m < mae(X.speed_now), "worse than carrying the current speed forward"
    assert m < mae(X.hist_target), "worse than the typical speed for the time of day"


def test_the_range_is_ordered_and_covers_about_four_in_five_outcomes(model, split):
    for h in HORIZONS:
        X = holdout(model, split, h)
        p = model.predict(X, h)
        assert (p.low <= p.speed + 1e-9).all() and (p.speed <= p.high + 1e-9).all()
        cover = float(((X.target >= p.low) & (X.target <= p.high)).mean())
        assert 0.68 <= cover <= 0.93, (h, cover)


def test_heavy_congestion_is_caught_more_often_than_by_looking_at_the_current_speed_at_an_hour(model, split):
    X = holdout(model, split, 60)
    truth = level(X.target, X.free_flow) 
    heavy = np.isin(truth, ["heavy", "severe"])
    got = np.isin(model.predict(X, 60).level, ["heavy", "severe"])
    naive = np.isin(level(X.speed_now, X.free_flow), ["heavy", "severe"])
    assert heavy.sum() > 50
    assert (got & heavy).sum() / heavy.sum() > (naive & heavy).sum() / heavy.sum() + 0.1


def test_explanations_add_up_exactly_to_the_forecast(model, split):
    X = holdout(model, split, 30).iloc[[500, 900, 1500]]
    contrib = model.boosters[30]["mean"].predict(X[FEATURES], pred_contrib=True)
    pred = model.boosters[30]["mean"].predict(X[FEATURES])
    np.testing.assert_allclose(contrib.sum(axis=1), pred, atol=1e-6)         # features plus the average give the forecast


def test_the_reasons_name_the_incident_when_there_is_one_and_the_time_of_day_at_a_peak(model, split):
    X = holdout(model, split, 30)
    inc = X[(X.incident_lanes_closed >= 2) & (X.incident_age_min >= 20)].iloc[[0]]
    top = [e["factor"] for e in model.explain(inc, 30, top=4)[0]]
    assert any("incident" in f or "lanes closed" in f or "speed" in f for f in top)
    why = model.explain(inc, 30, top=5)[0]
    assert all({"factor", "effect_kmh", "direction"} <= set(w) for w in why)
    assert sum(1 for w in why if w["direction"] == "slower") >= 1


def test_saved_and_loaded_model_forecasts_identically(model, split, tmp_path):
    model.save(tmp_path / "m")
    loaded = CongestionModel.load(tmp_path / "m")
    X = holdout(model, split, 60).iloc[:50]
    pd.testing.assert_frame_equal(model.predict(X, 60), loaded.predict(X, 60))
    assert loaded.widen == model.widen and loaded.free_flow == model.free_flow


def test_congestion_levels():
    assert list(level([90, 70, 50, 30], 100)) == ["free", "moderate", "heavy", "severe"]


# ------------------------------------------------------------------ registry and the promotion rule


def report(model_mae, persistence=5.0, typical=5.0, horizons=("15", "30", "60")):
    return {"horizons": {h: {"mae": {"model": model_mae, "persistence": persistence, "typical_for_time": typical}} for h in horizons}}


def test_promotion_rule():
    ok, why = decide(report(2.0), None)
    assert ok and not why
    ok, why = decide(report(5.5), None)                                         # no better than either baseline
    assert not ok and any("current speed" in w for w in why) and any("typical" in w for w in why)
    ok, why = decide(report(2.0, typical=1.5), None)
    assert not ok and any("typical" in w for w in why)
    ok, why = decide(report(4.9), None)                                         # better, but only by 2 percent
    assert not ok and any("only 2.0% better" in w for w in why)
    ok, _ = decide(report(2.03), report(2.0))                                   # within 2 percent of the active model
    assert ok
    ok, why = decide(report(2.5), report(2.0))                                  # clearly worse than the active model
    assert not ok and any("worse than the active" in w for w in why)
    ok, _ = decide(report(1.8), report(2.0))
    assert ok


def test_registry_keeps_versions_and_one_active(model, tmp_path):
    reg = Registry(tmp_path / "reg")
    assert reg.active() is None
    with pytest.raises(LookupError):
        reg.load()
    v1 = reg.register(model, report(2.0), "first")
    v2 = reg.register(model, report(1.9), "second")
    assert v1 != v2 and reg.versions() == sorted([v1, v2])
    reg.promote(v2)
    version, loaded = reg.load()
    assert version == v2 and loaded.free_flow == model.free_flow
    assert reg.report(v1)["note"] == "first"
    with pytest.raises(KeyError):
        reg.promote("nope")


# ------------------------------------------------------------------ the live service


def feed(store, df, now, minutes=180, source_end=None, speed=None):
    """Put the last stretch of a synthetic day into the store, shifted so it ends at `now`."""
    source_end = source_end or df.ts.max()
    w = df[(df.ts > source_end - pd.Timedelta(minutes=minutes)) & (df.ts <= source_end)]
    shift = pd.Timestamp(now) - source_end
    store.add_observations([{"segment": r.segment, "ts": (r.ts + shift).to_pydatetime(),
                             "speed_kmh": r.speed_kmh if speed is None else speed, "flow_vph": r.flow_vph} for r in w.itertuples()])
    wx = w.drop_duplicates("ts")
    store.add_weather([{"kind": "observed", "ts": (r.ts + shift).to_pydatetime(), "rain_mm_h": r.rain_mm_h,
                        "visibility_km": r.visibility_km} for r in wx.itertuples()])
    return shift


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def incident_end(data, seg=SEG):
    """A moment when an incident on seg is well under way and the road has visibly broken down."""
    hit = data[(data.segment == seg) & (data.incident_lanes_closed == 2) & (data.incident_age_min >= 25)]
    row = hit.loc[hit.speed_kmh.idxmin()]
    return row.ts, float(row.incident_age_min)


def peak_end(data):
    local = data.ts.dt.tz_convert(LOCAL_TZ)
    day = data[(local.dt.dayofweek == 1) & (local.dt.hour == 17) & (local.dt.minute == 30)].ts
    return day.iloc[len(day) // 2]


@pytest.fixture
def svc(model, data):
    store = MemoryObservationStore()
    service = ForecastService(store, model, "test-v1", clock=lambda: NOW)
    feed(store, data, NOW, source_end=peak_end(data))
    return service


def test_a_forecast_has_every_horizon_a_range_a_baseline_and_reasons(svc):
    f = svc.forecast(SEG)
    assert [r["horizon_min"] for r in f["forecasts"]] == [15, 30, 60] and f["model_version"] == "test-v1"
    for r in f["forecasts"]:
        assert r["low_kmh"] <= r["speed_kmh"] <= r["high_kmh"] and r["level"] in ("free", "moderate", "heavy", "severe")
        assert r["why"] and set(r["baseline"]) == {"keep_current_speed", "typical_for_time"}
    assert f["current_speed_kmh"] > 0 and f["free_flow_kmh"] > 80 and f["inputs"]["data_age_s"] <= 300


def test_what_if_closing_lanes_slows_the_forecast_and_the_reasons_say_why(svc):
    base = svc.forecast(SEG, [30])["forecasts"][0]
    hurt = svc.forecast(SEG, [30], overrides={"lanes_closed": 2})
    r = hurt["forecasts"][0]
    assert r["speed_kmh"] < base["speed_kmh"] - 3 and hurt["what_if"] == {"lanes_closed": 2}
    assert any("lanes closed" in w["factor"] or "incident" in w["factor"] for w in r["why"])


def test_an_incident_known_to_the_system_changes_the_forecast(svc):
    before = svc.forecast(SEG, [30])["forecasts"][0]["speed_kmh"]
    svc.incidents = lambda: {SEG: (2, 12.0)}
    f = svc.forecast(SEG, [30])
    assert f["forecasts"][0]["speed_kmh"] < before - 3 and f["incident"] == {"lanes_closed": 2, "minutes_since_start": 12.0}


def test_forecast_refuses_rather_than_guess(model, data):
    store = MemoryObservationStore()
    empty = ForecastService(store, model, "t", clock=lambda: NOW)
    with pytest.raises(StaleData):
        empty.forecast(SEG)
    feed(store, data, NOW - timedelta(hours=2), source_end=peak_end(data))        # readings are two hours old
    with pytest.raises(StaleData):
        empty.forecast(SEG)
    with pytest.raises(UnknownSegment):
        empty.forecast("NH99-S01")
    with pytest.raises(NoModel):
        ForecastService(store, None, "none", clock=lambda: NOW).forecast(SEG)
    thin = MemoryObservationStore()
    thin.add_observations([{"segment": SEG, "ts": NOW - timedelta(minutes=m), "speed_kmh": 90.0, "flow_vph": 3000.0} for m in (0, 5, 10)])
    with pytest.raises(StaleData):
        ForecastService(thin, model, "t", clock=lambda: NOW).forecast(SEG)           # three readings are not enough history


def test_a_weather_forecast_is_used_for_the_target_time(svc):
    dry = svc.forecast(SEG, [60])
    svc.store.add_weather([{"kind": "forecast", "ts": NOW + timedelta(minutes=60), "rain_mm_h": 12.0, "visibility_km": 3.0}])
    wet = svc.forecast(SEG, [60])
    assert wet["inputs"]["rain_at_target_mm_h"] == 12.0 and dry["inputs"]["rain_at_target_mm_h"] != 12.0
    assert wet["forecasts"][0]["speed_kmh"] <= dry["forecasts"][0]["speed_kmh"] + 0.5


def test_heavy_congestion_is_announced_once_not_every_minute(model, data):
    end, age = incident_end(data)
    store = MemoryObservationStore()
    svc = ForecastService(store, model, "test-v1", clock=lambda: NOW)
    feed(store, data, NOW, source_end=end)
    sent = []
    svc.emit = sent.append
    svc.incidents = lambda: {SEG: (2, age)}
    first = svc.run_cycle(NOW)
    assert any(a["segment"] == SEG for a in first), "expected the closed lanes to produce a heavy forecast"
    assert len(sent) == len(first) and sent[0].type == "congestion.forecast" and sent[0].data.level in ("heavy", "severe")
    assert sent[0].data.segment_id == SEG and sent[0].data.model_version == "test-v1" and sent[0].data.main_factor
    assert svc.run_cycle(NOW) == [] and len(sent) == len(first)                      # still heavy, no repeat
    svc._level_state[SEG] = 0                                                        # the road recovered in between
    assert any(a["segment"] == SEG for a in svc.run_cycle(NOW))                      # and it can announce again


def test_forecasts_are_recorded_then_scored_against_what_happened(model, data):
    store = MemoryObservationStore()
    svc = ForecastService(store, model, "test-v1", clock=lambda: NOW)
    end = peak_end(data)
    shift = feed(store, data, NOW, source_end=end)
    svc.run_cycle(NOW)
    assert len(store.forecasts) == 3 * len(SEGMENTS)
    assert svc.score_pending(NOW) == 0                                               # nothing has happened yet
    later = data[(data.ts > end) & (data.ts <= end + pd.Timedelta(minutes=70))]       # the road carries on
    store.add_observations([{"segment": r.segment, "ts": (r.ts + shift).to_pydatetime(), "speed_kmh": r.speed_kmh,
                             "flow_vph": r.flow_vph} for r in later.itertuples()])
    assert svc.score_pending(NOW + timedelta(minutes=70)) == 3 * len(SEGMENTS)
    acc = svc.accuracy(24, NOW + timedelta(minutes=70), min_samples=1)
    assert set(acc["horizons"]) == {"15", "30", "60"} and acc["horizons"]["15"]["samples"] == len(SEGMENTS)
    assert acc["horizons"]["15"]["mae_model"] < 15
    assert svc.accuracy(24, NOW + timedelta(minutes=70))["drift"] is False             # six samples are too few to call drift


def test_drift_is_flagged_when_reality_stops_matching_the_model(model, data):
    store = MemoryObservationStore()
    svc = ForecastService(store, model, "test-v1", clock=lambda: NOW,
                          backtest={"horizons": {"15": {"mae": {"model": 2.2}}, "30": {"mae": {"model": 2.7}}, "60": {"mae": {"model": 3.3}}}})
    for k in range(12):                                                              # a dozen forecast cycles, five minutes apart
        t = NOW + timedelta(minutes=5 * k)
        feed(store, data, t, source_end=peak_end(data) + pd.Timedelta(minutes=5 * k))
        svc.run_cycle(t)
    store.add_observations([{"segment": s.id, "ts": NOW + timedelta(minutes=m), "speed_kmh": 15.0, "flow_vph": 100.0}
                            for s in SEGMENTS for m in range(0, 200)])                # the road collapses: 15 km/h everywhere
    end = NOW + timedelta(minutes=200)
    svc.score_pending(end)
    acc = svc.accuracy(24, end, min_samples=10)
    assert acc["drift"] is True and acc["reasons"]


# ------------------------------------------------------------------ incidents to segments


def test_incidents_map_to_the_nearest_segment_and_only_when_open():
    from atms_contracts.models import IncidentState as I, Location, Severity
    from atms_core.forecast_link import open_incidents_by_segment
    from atms_core.store import Incident, Store
    from uuid import uuid4

    store = Store()

    def inc(lat, lon, state, lanes=("LANE_1", "LANE_2")):
        i = Incident(uuid4(), state, Severity.major, 0.9, Location(lat=lat, lon=lon), 0.0, "MH/PUNE/NH48",
                     NOW - timedelta(minutes=20), NOW, "t", list(lanes))
        store.incidents[i.id] = i
    inc(18.4576, 73.8509, I.dispatched)
    inc(18.4426, 73.8508, I.closed)                  # closed, ignored
    inc(19.5, 73.0, I.verified)                      # nowhere near the road
    out = open_incidents_by_segment(store, NOW)
    assert list(out) == [SEG] and out[SEG][0] == 2 and 19 < out[SEG][1] < 21


# ------------------------------------------------------------------ the training pipeline


def test_pipeline_promotes_a_good_model_and_does_not_reject_a_retrain_for_beating_a_model_that_saw_the_test_days(tmp_path):
    from atms_forecast.train import run

    df = generate(seed=3, days=35)
    reg = Registry(tmp_path / "reg")
    first = run(df, reg, test_days=7, rounds=60)
    assert first["promoted"] and reg.active() == first["version"]
    again = run(df, reg, test_days=7, rounds=60)                         # same data, the active model has seen the test days
    assert again["comparison_skipped"] and again["promoted"] and not again["reasons"]
    assert len(reg.versions()) == 2 and reg.active() == again["version"]
    assert reg.report(again["version"])["backtest"]["horizons"]["30"]["mae"]["model"] < \
        reg.report(again["version"])["backtest"]["horizons"]["30"]["mae"]["persistence"]


def test_pipeline_refuses_to_promote_a_model_trained_on_nonsense_and_keeps_the_old_one(tmp_path):
    from atms_forecast.train import run

    good = generate(seed=3, days=35)
    reg = Registry(tmp_path / "reg")
    ok = run(good, reg, test_days=7, rounds=60)
    junk = generate(seed=3, days=35)
    junk["speed_kmh"] = np.random.default_rng(0).permutation(junk["speed_kmh"].to_numpy())     # no pattern left to learn
    bad = run(junk, reg, test_days=7, rounds=60)
    assert not bad["promoted"] and bad["reasons"] and reg.active() == ok["version"] and len(reg.versions()) == 1
