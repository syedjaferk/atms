"""Fake SMS gateway with delivery receipts.

POST /p/{provider}/send   {to, text, idempotency_key, callback_url?}
  202 {message_id}            accepted, a signed receipt follows at callback_url
  400                         bad number, a permanent refusal
  503                         transient failure
  no answer                   slow gateway (the handler sleeps)
A repeated idempotency_key returns the first message id, so a retry never sends a second text.
Force an outcome with X-Sim-Behavior: ok | transient | permanent | timeout | undelivered | silent.
undelivered accepts the message and reports it undeliverable. silent accepts and never sends a receipt.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from atms_contracts.signing import sign_receipt

NUMBER = re.compile(r"^\+\d{10,15}$")


@dataclass
class Profile:
    p_transient: float = 0.0
    p_undelivered: float = 0.0
    receipt_delay_s: tuple[float, float] = (0.05, 0.3)
    timeout_sleep_s: float = 30.0


class SendRequest(BaseModel):
    to: str
    text: str
    idempotency_key: str
    callback_url: str | None = None


@dataclass
class _State:
    rng: random.Random
    profiles: dict[str, Profile]
    receipt_secret: str
    messages: dict[tuple[str, str], dict] = field(default_factory=dict)    # (provider, key) -> message
    receipts: list[dict] = field(default_factory=list)                     # what was posted back
    counter: int = 0
    tasks: set = field(default_factory=set)


def create_app(seed: int | None = None, profiles: dict[str, Profile] | None = None, receipt_secret: str = "dev-receipt-secret",
               callback_client: httpx.AsyncClient | None = None) -> FastAPI:
    state = _State(random.Random(seed), profiles or {}, receipt_secret)
    app = FastAPI(title="ATMS SMS gateway simulator")
    app.state.sim = state

    async def send_receipt(provider: str, msg: dict, url: str, status: str, reason: str | None) -> None:
        await asyncio.sleep(state.rng.uniform(*state.profiles.get(provider, Profile()).receipt_delay_s))
        body = json.dumps({"message_id": msg["message_id"], "status": status, **({"reason": reason} if reason else {})}).encode()
        state.receipts.append({"provider": provider, "message_id": msg["message_id"], "status": status})
        client = callback_client or httpx.AsyncClient()
        try:
            await client.post(url, content=body, headers={"X-Signature": sign_receipt(state.receipt_secret, body),
                                                          "Content-Type": "application/json"}, timeout=5)
        except httpx.HTTPError:
            pass                                    # a real gateway would retry the callback
        finally:
            if callback_client is None:
                await client.aclose()

    @app.post("/p/{provider}/send")
    async def send(provider: str, req: SendRequest, x_sim_behavior: str | None = Header(default=None)):
        key = (provider, req.idempotency_key)
        if key in state.messages:
            return JSONResponse({"message_id": state.messages[key]["message_id"]}, status_code=202)
        profile = state.profiles.get(provider, Profile())
        behavior = x_sim_behavior
        if behavior is None:
            roll = state.rng.random()
            behavior = "transient" if roll < profile.p_transient else "undelivered" if roll < profile.p_transient + profile.p_undelivered else "ok"
        if behavior not in ("ok", "transient", "permanent", "timeout", "undelivered", "silent"):
            raise HTTPException(400, f"unknown X-Sim-Behavior {behavior!r}")
        if behavior == "timeout":
            await asyncio.sleep(profile.timeout_sleep_s)
            raise HTTPException(504, "gateway timeout")
        if behavior == "transient":
            raise HTTPException(503, "gateway busy")
        if behavior == "permanent" or not NUMBER.match(req.to):
            raise HTTPException(400, "invalid destination")
        state.counter += 1
        msg = {"message_id": f"{provider}-{state.counter:05d}", "to": req.to, "text": req.text, "behavior": behavior}
        state.messages[key] = msg
        if req.callback_url and behavior != "silent":
            status, reason = ("undelivered", "handset unreachable") if behavior == "undelivered" else ("delivered", None)
            task = asyncio.get_running_loop().create_task(send_receipt(provider, msg, req.callback_url, status, reason))
            state.tasks.add(task)
            task.add_done_callback(state.tasks.discard)
        return JSONResponse({"message_id": msg["message_id"]}, status_code=202)

    @app.get("/messages")
    async def messages():
        return list(state.messages.values())

    return app
