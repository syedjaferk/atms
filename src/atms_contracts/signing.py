"""Webhook and receipt signatures. Shared by the notifier, the receivers and the simulators.

Webhook header   X-ATMS-Signature: t=<unix seconds>,v1=<hex hmac-sha256 of "<t>.<body>">
The timestamp is inside the signed text and checked against a tolerance, so a captured request cannot be replayed later.
"""
from __future__ import annotations

import hashlib
import hmac
import time

TOLERANCE_S = 300


def sign_webhook(secret: str, body: bytes, now: float | None = None) -> str:
    t = int(now if now is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_webhook(secret: str, header: str | None, body: bytes, now: float | None = None,
                   tolerance_s: int = TOLERANCE_S) -> bool:
    if not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t, given = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def sign_receipt(secret: str, body: bytes) -> str:
    """Delivery receipts from SMS providers: plain hex hmac of the raw body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_receipt(secret: str, signature: str | None, body: bytes) -> bool:
    return bool(signature) and hmac.compare_digest(sign_receipt(secret, body), signature)
