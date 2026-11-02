"""Scripted detections. Each scenario is what a real camera might report."""
from __future__ import annotations

from dataclasses import dataclass

from atms_contracts.cameras import CAMERAS, Camera  # noqa: F401  (re-exported for the simulator)
from atms_contracts.models import Severity


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    camera_id: str
    confidence: float
    severity: Severity
    vehicles: int
    lanes: tuple[str, ...]
    clip_seconds: float = 60.0


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("major_two_vehicle", "Two-vehicle collision, auto-verify range", "GNT001",
             0.94, Severity.major, 2, ("LANE_1", "LANE_2")),
    Scenario("duplicate_second_camera", "Same crash seen by the next camera, must merge", "GNT002",
             0.91, Severity.major, 2, ("LANE_1", "LANE_2")),
    Scenario("needs_operator", "Middle confidence, an operator must verify", "GNT001",
             0.74, Severity.major, 1, ("LANE_1",)),
    Scenario("low_confidence", "Below the incident threshold, logged only", "GNT001",
             0.45, Severity.minor, 1, ("LANE_3",)),
    Scenario("critical_pileup", "Pile-up, adds fire to the dispatch", "GNT001",
             0.97, Severity.critical, 5, ("LANE_1", "LANE_2", "LANE_3")),
)}
