"""Backtest. Train on the past, score on a later period the model never saw, against two naive baselines."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import FEATURES, HORIZONS, build, level, level_rank
from .model import CongestionModel


def split_by_time(df: pd.DataFrame, test_days: int, gap_min: int = 60) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Test is the last test_days. Training stops a gap before it, so no training target reaches into the test period."""
    cutoff = df.ts.max() - pd.Timedelta(days=test_days)
    return df[df.ts < cutoff - pd.Timedelta(minutes=gap_min)], df[df.ts >= cutoff - pd.Timedelta(minutes=70)]


def score(model: CongestionModel, test: pd.DataFrame, test_start: pd.Timestamp, horizons=HORIZONS) -> dict:
    out: dict = {"test_from": str(test_start), "test_to": str(test.ts.max()), "horizons": {}}
    for h in horizons:
        data = build(test, h, model.hist, model.free_flow, seed=1)
        data = data[(data.index >= test_start) & data.target.notna()].dropna(subset=FEATURES[:6])
        pred = model.predict(data, h)
        y = data.target.to_numpy()
        err = lambda p: np.abs(np.asarray(p) - y)
        persistence, typical = data.speed_now.to_numpy(), data.hist_target.to_numpy()
        ff = data.free_flow.to_numpy()
        true_lv, pred_lv = level(y, ff), pred["level"].to_numpy()
        heavy_true, heavy_pred = level_rank(true_lv) >= 2, level_rank(pred_lv) >= 2
        incident = (data.incident_lanes_closed > 0).to_numpy()
        m = {
            "rows": int(len(y)),
            "mae": {"model": float(err(pred.speed).mean()), "persistence": float(err(persistence).mean()),
                    "typical_for_time": float(err(typical).mean())},
            "rmse_model": float(np.sqrt(((pred.speed.to_numpy() - y) ** 2).mean())),
            "skill_vs_persistence": float(1 - err(pred.speed).mean() / err(persistence).mean()),
            "skill_vs_typical": float(1 - err(pred.speed).mean() / err(typical).mean()),
            "level_accuracy": float((true_lv == pred_lv).mean()),
            "level_accuracy_persistence": float((true_lv == level(persistence, ff)).mean()),
            "heavy_or_worse": {
                "share_of_rows": float(heavy_true.mean()),
                "recall": float((heavy_pred & heavy_true).sum() / max(1, heavy_true.sum())),
                "precision": float((heavy_pred & heavy_true).sum() / max(1, heavy_pred.sum())),
                "recall_persistence": float(((level_rank(level(persistence, ff)) >= 2) & heavy_true).sum() / max(1, heavy_true.sum())),
            },
            "interval_coverage_80": float(((y >= pred.low.to_numpy()) & (y <= pred.high.to_numpy())).mean()),
            "interval_width_kmh": float((pred.high - pred.low).mean()),
            "during_incidents": {
                "rows": int(incident.sum()),
                "mae_model": float(err(pred.speed)[incident].mean()) if incident.any() else None,
                "mae_persistence": float(err(persistence)[incident].mean()) if incident.any() else None,
                "mae_typical": float(err(typical)[incident].mean()) if incident.any() else None,
            },
        }
        out["horizons"][str(h)] = m
    return out


def backtest(df: pd.DataFrame, test_days: int = 14, rounds: int | None = None, horizons=HORIZONS) -> tuple[CongestionModel, dict]:
    train, test = split_by_time(df, test_days)
    kwargs = {"rounds": rounds} if rounds else {}
    model = CongestionModel.fit(train, horizons=horizons, **kwargs)
    start = df.ts.max() - pd.Timedelta(days=test_days)
    return model, score(model, test, start, horizons)


def summary(report: dict) -> str:
    lines = [f"test period {report['test_from'][:16]} to {report['test_to'][:16]}"]
    for h, m in report["horizons"].items():
        a, i = m["mae"], m["during_incidents"]
        lines.append(f"{h:>3} min  MAE model {a['model']:.2f}  persistence {a['persistence']:.2f}  typical {a['typical_for_time']:.2f}"
                     f"  skill {m['skill_vs_persistence']*100:+.0f}% / {m['skill_vs_typical']*100:+.0f}%"
                     f"  level acc {m['level_accuracy']*100:.0f}%  heavy recall {m['heavy_or_worse']['recall']*100:.0f}%"
                     f" (persistence {m['heavy_or_worse']['recall_persistence']*100:.0f}%)  80% range covers {m['interval_coverage_80']*100:.0f}%")
        if i["rows"]:
            lines.append(f"         during incidents ({i['rows']} rows)  MAE model {i['mae_model']:.1f}  persistence {i['mae_persistence']:.1f}  typical {i['mae_typical']:.1f}")
    return "\n".join(lines)
