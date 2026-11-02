"""Where the edge sends its events. MQTT for real runs, in memory for tests."""
from __future__ import annotations

from typing import Protocol


class PublishError(Exception):
    """The broker did not acknowledge the message. The outbox keeps it."""


class Publisher(Protocol):
    def publish(self, topic: str, payload: bytes) -> None: ...


class InMemoryPublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.fail = False

    def publish(self, topic: str, payload: bytes) -> None:
        if self.fail:
            raise PublishError("simulated broker outage")
        self.sent.append((topic, payload))


class MqttPublisher:
    """QoS 1 publisher. publish() returns only after the broker's PUBACK."""

    def __init__(self, host: str, port: int = 1883, username: str | None = None,
                 password: str | None = None, client_id: str = "atms-edge", ack_timeout_s: float = 5.0):
        import paho.mqtt.client as mqtt

        self._ack_timeout_s = ack_timeout_s
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self._client.username_pw_set(username, password)
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def publish(self, topic: str, payload: bytes) -> None:
        info = self._client.publish(topic, payload, qos=1)
        try:
            info.wait_for_publish(timeout=self._ack_timeout_s)
        except (ValueError, RuntimeError) as exc:
            raise PublishError(str(exc)) from exc
        if not info.is_published():
            raise PublishError(f"no PUBACK within {self._ack_timeout_s}s for {topic}")

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
