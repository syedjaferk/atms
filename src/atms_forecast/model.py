"""Gradient boosted forecasters, one set per horizon: the expected speed and an 80 percent range.

LightGBM can return each feature's contribution to a single prediction (TreeSHAP), which is what the
explanations are built from. Contributions are in km/h and add up to the prediction, so the story told to
an operator is exactly the arithmetic the model did.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import FEATURES, HORIZONS, LABELS, HistProfile, build, level

PARAMS = dict(learning_rate=0.05, num_leaves=31, min_data_in_leaf=40, feature_fraction=0.9, bagging_fraction=0.8,
              bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=11, num_threads=4)
ROUNDS = 350


@dataclass
class CongestionModel:
    hist: HistProfile
    free_flow: dict[str, float]
    boosters: dict[int, dict[str, lgb.Booster]]
    meta: dict = field(default_factory=dict)
    widen: dict[int, float] = field(default_factory=dict)       # conformal widening of the range, km/h, per horizon

    # ------------------------------------------------------------ training

    @classmethod
    def fit(cls, train: pd.DataFrame, horizons=HORIZONS, rounds: int = ROUNDS, seed: int = 0,
            calibration_days: int = 7) -> "CongestionModel":
        """The last calibration_days of the data are not trained on. They are used once, to widen the
        80 percent range until it really covers 80 percent of outcomes it has not seen (conformalised quantiles)."""
        hist = HistProfile.fit(train)
        free_flow = train.groupby("segment").speed_kmh.quantile(0.95).to_dict()
        boosters: dict[int, dict[str, lgb.Booster]] = {}
        widen: dict[int, float] = {}
        end = train.ts.max()
        fit_until = end - pd.Timedelta(days=calibration_days) if calibration_days else end
        cutoff = fit_until
        for h in horizons:
            data = build(train, h, hist, free_flow, seed=seed)
            data = data[data.target.notna() & (data.target_ts <= cutoff)].dropna(subset=FEATURES[:6])
            X, y = data[FEATURES], data["target"]
            ds = lambda: lgb.Dataset(X, y, categorical_feature=["segment_code"], free_raw_data=False)
            boosters[h] = {
                "mean": lgb.train({**PARAMS, "objective": "regression"}, ds(), rounds),
                "q10": lgb.train({**PARAMS, "objective": "quantile", "alpha": 0.1}, ds(), rounds),
                "q90": lgb.train({**PARAMS, "objective": "quantile", "alpha": 0.9}, ds(), rounds),
            }
            if calibration_days:
                cal = build(train, h, hist, free_flow, seed=seed + 1)
                cal = cal[cal.target.notna() & (cal.index > fit_until + pd.Timedelta(minutes=60))].dropna(subset=FEATURES[:6])
                lo, hi = boosters[h]["q10"].predict(cal[FEATURES]), boosters[h]["q90"].predict(cal[FEATURES])
                scores = np.maximum(lo - cal.target.to_numpy(), cal.target.to_numpy() - hi)
                widen[h] = float(max(0.0, np.quantile(scores, 0.8 * (1 + 1 / len(scores)))))
        meta = {"trained_rows": int(len(train)), "data_from": str(train.ts.min()), "data_to": str(cutoff),
                "fit_until": str(cutoff), "calibration_days": calibration_days,
                "horizons": list(horizons), "features": FEATURES, "lightgbm": lgb.__version__}
        return cls(hist, free_flow, boosters, meta, widen)

    # ------------------------------------------------------------ predicting

    def predict(self, X: pd.DataFrame, horizon: int) -> pd.DataFrame:
        b = self.boosters[horizon]
        mean = b["mean"].predict(X[FEATURES])
        lo, hi = b["q10"].predict(X[FEATURES]), b["q90"].predict(X[FEATURES])
        w = self.widen.get(horizon, 0.0)
        lo, hi = np.minimum(lo, mean) - w, np.maximum(hi, mean) + w       # keep the range around the estimate
        out = pd.DataFrame({"speed": mean, "low": lo, "high": hi}, index=X.index)
        out["level"] = level(mean, X["free_flow"].to_numpy())
        return out

    def explain(self, X: pd.DataFrame, horizon: int, top: int = 5) -> list[list[dict]]:
        """Per row, the features that moved the forecast most, in km/h against the model's average."""
        contrib = self.boosters[horizon]["mean"].predict(X[FEATURES], pred_contrib=True)
        out = []
        for row in contrib:
            parts: dict[str, float] = {}
            for name, v in zip(FEATURES, row[:-1]):
                label = LABELS[name]
                parts[label] = parts.get(label, 0.0) + float(v)             # hour_sin and hour_cos are one story
            ranked = sorted(parts.items(), key=lambda kv: -abs(kv[1]))[:top]
            out.append([{"factor": k, "effect_kmh": round(v, 1), "direction": "slower" if v < 0 else "faster"}
                        for k, v in ranked if abs(v) >= 0.2])
        return out

    def base_value(self, horizon: int) -> float:
        return float(self.boosters[horizon]["mean"].predict(pd.DataFrame([[0.0] * len(FEATURES)], columns=FEATURES),
                                                           pred_contrib=True)[0][-1])

    # ------------------------------------------------------------ saving

    def save(self, directory: str | Path) -> None:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        for h, group in self.boosters.items():
            for kind, booster in group.items():
                booster.save_model(str(d / f"h{h}_{kind}.txt"))
        (d / "model.json").write_text(json.dumps({"hist": self.hist.to_json(), "free_flow": self.free_flow,
                                                  "meta": self.meta, "horizons": sorted(self.boosters),
                                                  "widen": {str(k): v for k, v in self.widen.items()}}))

    @classmethod
    def load(cls, directory: str | Path) -> "CongestionModel":
        d = Path(directory)
        info = json.loads((d / "model.json").read_text())
        boosters = {h: {k: lgb.Booster(model_file=str(d / f"h{h}_{k}.txt")) for k in ("mean", "q10", "q90")}
                    for h in info["horizons"]}
        return cls(HistProfile.from_json(info["hist"]), info["free_flow"], boosters, info["meta"],
                   {int(k): v for k, v in info.get("widen", {}).items()})
