"""Model registry. Versions on disk with their backtest, one marked active, and a rule for who may become active."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .model import CongestionModel


MIN_GAIN = 0.05


def decide(candidate: dict, active_on_same_window: dict | None, margin: float = 0.02, min_gain: float = MIN_GAIN) -> tuple[bool, list[str]]:
    """A candidate becomes active only if it beats both naive baselines at every horizon by a real margin
    (a model trained on noise still edges past a noisy baseline) and is not worse than the model it would
    replace, scored on the same period."""
    reasons: list[str] = []
    for h, m in candidate["horizons"].items():
        a = m["mae"]
        if a["model"] >= a["persistence"]:
            reasons.append(f"{h} min: no better than carrying the current speed forward ({a['model']:.2f} vs {a['persistence']:.2f})")
        if a["model"] >= a["typical_for_time"]:
            reasons.append(f"{h} min: no better than the typical speed for the time of day ({a['model']:.2f} vs {a['typical_for_time']:.2f})")
        best = min(a["persistence"], a["typical_for_time"])
        if a["model"] < best and a["model"] > best * (1 - min_gain):
            reasons.append(f"{h} min: only {(1 - a['model'] / best) * 100:.1f}% better than the best naive baseline, {min_gain * 100:.0f}% required")
        if active_on_same_window:
            old = active_on_same_window["horizons"][h]["mae"]["model"]
            if a["model"] > old * (1 + margin):
                reasons.append(f"{h} min: worse than the active model ({a['model']:.2f} vs {old:.2f})")
    return (not reasons), reasons


class Registry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "versions").mkdir(parents=True, exist_ok=True)

    def register(self, model: CongestionModel, report: dict, note: str = "", sim=None) -> str:
        version = "v" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        d = self.root / "versions" / version
        n = 1
        while d.exists():
            n += 1
            d = self.root / "versions" / f"{version}-{n}"
        d.mkdir(parents=True)
        model.save(d / "model")
        if sim is not None:                                       # (twin, fit info, validation on days it had not seen)
            from .calibrate import save as save_sim
            save_sim(*sim, d / "sim.json")
        (d / "report.json").write_text(json.dumps({"backtest": report, "note": note,
                                                    "registered_at": datetime.now(timezone.utc).isoformat()}, indent=2))
        return d.name

    def versions(self) -> list[str]:
        return sorted(p.name for p in (self.root / "versions").iterdir() if p.is_dir())

    def active(self) -> str | None:
        f = self.root / "ACTIVE"
        return f.read_text().strip() if f.exists() else None

    def promote(self, version: str) -> None:
        if version not in self.versions():
            raise KeyError(version)
        (self.root / "ACTIVE").write_text(version + "\n")

    def load(self, version: str | None = None) -> tuple[str, CongestionModel]:
        version = version or self.active()
        if version is None:
            raise LookupError("no active model, train one first")
        return version, CongestionModel.load(self.root / "versions" / version / "model")

    def load_sim(self, version: str | None = None):
        """The calibrated twin that was registered with a model version, or None."""
        version = version or self.active()
        path = self.root / "versions" / str(version) / "sim.json"
        if not version or not path.exists():
            return None
        from .calibrate import load as load_sim
        return load_sim(path)

    def report(self, version: str) -> dict:
        return json.loads((self.root / "versions" / version / "report.json").read_text())
