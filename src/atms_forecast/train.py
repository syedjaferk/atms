"""Train, backtest, decide, register.

    python -m atms_forecast.train --registry models --days 56 --seed 7
    python -m atms_forecast.train --registry models --csv readings.csv     real data: ts,segment,speed_kmh,flow_vph,rain_mm_h,visibility_km,incident_lanes_closed,incident_age_min

The model is first trained without the last two weeks and scored on them. Only if it passes the
promotion rule is a final model trained on everything and made active.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from .evaluate import backtest, score, summary
from .model import CongestionModel
from .registry import Registry, decide
from .synth import generate


def run(df: pd.DataFrame, registry: Registry, test_days: int = 14, note: str = "", rounds: int | None = None,
        sim: bool = True, sim_nfev: int = 60) -> dict:
    model, report = backtest(df, test_days, rounds)
    start = df.ts.max() - pd.Timedelta(days=test_days)
    active_report, skipped = None, None
    if registry.active():
        _, active = registry.load()
        if pd.Timestamp(active.meta["data_to"]) < start:
            active_report = score(active, df[df.ts >= start - pd.Timedelta(minutes=70)], start)
        else:
            # The active model was trained on these very days, so scoring it here would flatter it.
            skipped = "active model has already seen the test period, so it is not compared on it"
    ok, reasons = decide(report, active_report)
    result = {"report": report, "promoted": False, "reasons": reasons, "active_before": registry.active(), "version": None,
              "comparison_skipped": skipped}
    if ok:
        final = CongestionModel.fit(df, **({"rounds": rounds} if rounds else {}))     # now use every day available
        twin_pack = None
        if sim:
            from .calibrate import fit as fit_twin, validate as validate_twin
            earlier, _ = fit_twin(df[df.ts < start - pd.Timedelta(minutes=60)], sim_nfev)       # never sees the test days
            check = validate_twin(earlier, df[df.ts >= start - pd.Timedelta(days=1)])
            twin, info = fit_twin(df, sim_nfev)                                                    # the one that is kept
            twin_pack = (twin, info, check)
            result["twin_check"] = check
        result["version"] = registry.register(final, report, note, twin_pack)
        registry.promote(result["version"])
        result["promoted"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atms_forecast.train")
    p.add_argument("--registry", default="models")
    p.add_argument("--csv")
    p.add_argument("--days", type=int, default=56)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--test-days", type=int, default=14)
    args = p.parse_args(argv)
    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["ts"])
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        note = f"trained on {args.csv}"
    else:
        df, note = generate(args.seed, args.days), f"synthetic traffic, seed {args.seed}, {args.days} days"
    result = run(df, Registry(args.registry), args.test_days, note)
    print(summary(result["report"]))
    if result["comparison_skipped"]:
        print("note:", result["comparison_skipped"])
    if result.get("twin_check"):
        c = result["twin_check"]
        print("what-if twin, checked on days it had not seen, replaying the real incidents and rain:")
        for h, r in c["from_start_state"].items():
            print(f"  {h} h ahead from the road as it was: error {r['mae_twin']} km/h (typical day {r['mae_typical']}, keep current speed {r['mae_keep_current']})"
                  f"; during incidents {r['mae_twin_incident']} (typical day {r['mae_typical_incident']})")
    if result["promoted"]:
        print(f"promoted {result['version']} (replaces {result['active_before']})")
        return 0
    print("not promoted:")
    for r in result["reasons"]:
        print("  -", r)
    return 1


if __name__ == "__main__":
    sys.exit(main())
