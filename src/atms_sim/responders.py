"""Simulated responders. They report progress through the public API, like a real unit would."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from atms_contracts.models import ResponderStatus


@dataclass(frozen=True)
class Step:
    delay_s: float
    status: ResponderStatus
    eta_s: int | None


def plan_steps(eta_s: int, rng: random.Random, on_scene_s: tuple[int, int] = (600, 1800),
               speedup: float = 1.0) -> list[Step]:
    """en_route now, on_scene when the ETA has passed, done after the work. speedup shrinks the waits."""
    return [
        Step(0.0, ResponderStatus.en_route, eta_s),
        Step(eta_s / speedup, ResponderStatus.on_scene, 0),
        Step(rng.randint(*on_scene_s) / speedup, ResponderStatus.done, None),
    ]


def run_responder(client: httpx.Client, incident_id: str, assignment_id: str, steps: list[Step],
                  sleep: Callable[[float], None] = time.sleep) -> list[httpx.Response]:
    """PATCH each step in order. Stops if the API answers with an error (for example 409)."""
    out: list[httpx.Response] = []
    for step in steps:
        sleep(step.delay_s)
        body: dict = {"status": step.status.value}
        if step.eta_s is not None:
            body["eta_s"] = step.eta_s
        resp = client.patch(
            f"/v1/incidents/{incident_id}/assignments/{assignment_id}",
            json=body,
            headers={"Idempotency-Key": f"{assignment_id}:{step.status.value}"},
        )
        out.append(resp)
        if resp.status_code >= 400:
            break
    return out
