"""Where notices go. SMS providers and signed webhooks, both behind small interfaces."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from atms_contracts.signing import sign_webhook


class ProviderError(Exception):
    """transient means trying again may work (timeout, 5xx). Otherwise the provider refused for good."""

    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class SmsResult:
    provider_ref: str


class SmsProvider(Protocol):
    name: str

    async def send(self, to: str, text: str, idempotency_key: str) -> SmsResult: ...


class HttpSmsProvider:
    """Talks to an SMS gateway over HTTP. The idempotency key makes a retry after a timeout safe."""

    def __init__(self, name: str, client: httpx.AsyncClient, timeout_s: float = 10.0, callback_url: str | None = None):
        self.name, self.client, self.timeout_s, self.callback_url = name, client, timeout_s, callback_url

    async def send(self, to: str, text: str, idempotency_key: str) -> SmsResult:
        body = {"to": to, "text": text, "idempotency_key": idempotency_key, "callback_url": self.callback_url}
        try:
            resp = await self.client.post(f"/p/{self.name}/send", json=body, timeout=self.timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name} unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise ProviderError(f"{self.name} answered {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} refused: {resp.status_code} {resp.text[:80]}", transient=False)
        return SmsResult(resp.json()["message_id"])


class WebhookSender:
    def __init__(self, client: httpx.AsyncClient, timeout_s: float = 10.0):
        self.client, self.timeout_s = client, timeout_s

    async def send(self, url: str, body: bytes, secret: str | None) -> None:
        if not secret:
            raise ProviderError("no signing secret configured for this webhook", transient=False)
        headers = {"Content-Type": "application/json", "X-ATMS-Signature": sign_webhook(secret, body)}
        try:
            resp = await self.client.post(url, content=body, headers=headers, timeout=self.timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("webhook timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"webhook unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise ProviderError(f"webhook answered {resp.status_code}")
        if resp.status_code >= 300:
            raise ProviderError(f"webhook refused: {resp.status_code}", transient=False)


class RecordingSms:
    """Accepts everything and remembers it. The default when no gateway is configured, and handy in tests."""

    def __init__(self, name: str = "primary"):
        self.name, self.sent = name, []

    async def send(self, to: str, text: str, idempotency_key: str) -> SmsResult:
        self.sent.append((to, text, idempotency_key))
        return SmsResult(f"{self.name}-{len(self.sent)}")


class RecordingWebhook:
    def __init__(self):
        self.sent: list[tuple[str, bytes, str | None]] = []

    async def send(self, url: str, body: bytes, secret: str | None) -> None:
        self.sent.append((url, body, secret))
