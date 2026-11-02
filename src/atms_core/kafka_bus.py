"""Kafka as the bus between services.

Same interface as InMemoryBus. Every event goes to one topic. Each service is a consumer group, so
it has its own committed offsets and sees every event it subscribed to. Delivery is at least once:
an offset is stored only after the handler finished, and handlers are written to be replay safe.

Keys decide ordering. Events about an incident use the incident id, so one incident is handled in
order by each service. Raw edge events use the road segment, so a clip never overtakes its detection.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from confluent_kafka import Consumer, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic

from atms_contracts.models import AccidentDetected, Envelope, parse_event
from atms_contracts.topics import KAFKA_DLQ_TOPIC, KAFKA_EVENTS_TOPIC

log = logging.getLogger("atms.kafka")
Handler = Callable[[Envelope], Awaitable[None]]


class DeliveryError(Exception):
    """Kafka did not confirm every message."""


@dataclass
class _Group:
    name: str
    types: frozenset[str] | None
    handler: Handler
    broadcast: bool
    consumer: Consumer | None = None
    task: asyncio.Task | None = None
    seen: "OrderedDict[uuid.UUID, None]" = field(default_factory=OrderedDict)
    consumed: int = 0           # messages passed, handled or skipped, since start (used by idle())
    assigned: bool = False


class KafkaBus:
    def __init__(self, bootstrap: str, topic: str = KAFKA_EVENTS_TOPIC, dlq_topic: str = KAFKA_DLQ_TOPIC,
                 group_prefix: str = "atms", partitions: int = 6, poll_s: float = 0.1, max_attempts: int = 3,
                 retry_backoff_s: float = 0.2, seen_limit: int = 20000, session_timeout_ms: int = 10000):
        self.bootstrap, self.topic, self.dlq_topic = bootstrap, topic, dlq_topic
        self.group_prefix, self.partitions = group_prefix, partitions
        self.session_timeout_ms = session_timeout_ms     # how long a killed process holds up its group before others take over
        self.poll_s, self.max_attempts, self.retry_backoff_s, self.seen_limit = poll_s, max_attempts, retry_backoff_s, seen_limit
        self._groups: list[_Group] = []
        self._producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all",
                                   "linger.ms": 5, "compression.type": "lz4"})
        self._failed: list[str] = []
        self._delivered = 0
        self._camera_segment: dict[str, str] = {}
        self.log: list[Envelope] = []               # what this bus published, in order
        self.dead: list[tuple[str, Envelope | None, Exception]] = []
        self._started = False

    # ------------------------------------------------------------ subscribing

    def subscribe(self, group: str, types: Iterable[str] | None, handler: Handler, broadcast: bool = False) -> None:
        """broadcast: every instance sees every new event (the realtime gateway), not a shared share of them."""
        self._groups.append(_Group(group, frozenset(types) if types is not None else None, handler, broadcast))

    # ------------------------------------------------------------ publishing

    def _key(self, event: Envelope) -> str:
        if isinstance(event, AccidentDetected):
            d = event.data.site
            seg = f"{d.state}/{d.city}/{d.road}"
            self._camera_segment[d.camera_id] = seg
            return seg
        if event.incident_id:
            return str(event.incident_id)
        camera = getattr(getattr(event, "data", None), "camera_id", None)
        return self._camera_segment.get(camera, camera) or str(event.event_id)

    def publish(self, event: Envelope, key: str | None = None) -> None:
        payload = event.model_dump_json().encode()
        for _ in range(100):
            try:
                self._producer.produce(self.topic, payload, key=key or self._key(event),
                                       headers=[("event_type", event.type)], on_delivery=self._on_delivery)
                break
            except BufferError:
                self._producer.poll(0.05)
        else:
            raise DeliveryError("producer queue stayed full")
        self.log.append(event)
        self._producer.poll(0)

    def _on_delivery(self, err, msg) -> None:
        if err is not None:
            self._failed.append(str(err))
        else:
            self._delivered += 1

    def flush(self, timeout_s: float = 10.0) -> None:
        """Block until every produced message is confirmed. Raises if any was not."""
        left = self._producer.flush(timeout_s)
        if left or self._failed:
            failed, self._failed = self._failed, []
            raise DeliveryError(f"{left} message(s) unconfirmed, errors: {failed[:3]}")

    # ------------------------------------------------------------ lifecycle

    def ensure_topics(self) -> None:
        admin = AdminClient({"bootstrap.servers": self.bootstrap})
        futures = admin.create_topics([NewTopic(self.topic, self.partitions, 1), NewTopic(self.dlq_topic, 1, 1)])
        for name, f in futures.items():
            try:
                f.result(15)
            except KafkaException as exc:
                if "TOPIC_ALREADY_EXISTS" not in str(exc):
                    raise

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.ensure_topics()
        loop = asyncio.get_running_loop()
        for g in self._groups:
            instance = f".{uuid.uuid4().hex[:8]}" if g.broadcast else ""
            g.consumer = Consumer({
                "bootstrap.servers": self.bootstrap, "group.id": f"{self.group_prefix}.{g.name}{instance}",
                "auto.offset.reset": "latest" if g.broadcast else "earliest",
                "enable.auto.commit": True, "auto.commit.interval.ms": 500,
                "enable.auto.offset.store": False,           # stored by us, after the handler succeeded
                "partition.assignment.strategy": "cooperative-sticky",
                "session.timeout.ms": self.session_timeout_ms,
            })
            g.consumer.subscribe([self.topic], on_assign=lambda _c, _p, grp=g: setattr(grp, "assigned", True))
            g.task = loop.create_task(self._work(g))

    async def ready(self, timeout_s: float = 20.0) -> None:
        """Wait until every group has partitions assigned."""
        end = asyncio.get_running_loop().time() + timeout_s
        while not all(g.assigned for g in self._groups):
            if asyncio.get_running_loop().time() > end:
                raise TimeoutError("consumer groups were not assigned partitions in time")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        for g in self._groups:
            if g.task:
                g.task.cancel()
        await asyncio.gather(*[g.task for g in self._groups if g.task], return_exceptions=True)
        for g in self._groups:
            if g.consumer:
                g.consumer.close()          # commits the stored offsets
                g.consumer, g.task = None, None
        self._started = False

    def close(self) -> None:
        """Synchronous cleanup for code that has no running loop. Call after the loop has finished."""
        for g in self._groups:
            if g.consumer:
                g.consumer.close()
                g.consumer = None
        self._producer.flush(2)

    # ------------------------------------------------------------ consuming

    async def _work(self, g: _Group) -> None:
        consumer = g.consumer
        while True:
            msg = await asyncio.to_thread(consumer.poll, self.poll_s)
            if msg is None:
                continue
            if msg.error():
                log.warning("%s: %s", g.name, msg.error())
                continue
            await self._handle(g, msg)
            consumer.store_offsets(message=msg)
            g.consumed += 1

    async def _handle(self, g: _Group, msg) -> None:
        headers = dict(msg.headers() or [])
        etype = headers.get("event_type", b"").decode() or None
        if g.types is not None and etype is not None and etype not in g.types:
            return                                           # not for this service, no need to parse
        event = None
        try:
            event = parse_event(msg.value())
            if g.types is not None and event.type not in g.types:
                return
            if event.event_id in g.seen:
                return                                       # a redelivery in this run
            for attempt in range(1, self.max_attempts + 1):
                try:
                    await g.handler(event)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed on %s (attempt %d): %r", g.name, event.type, attempt, exc)
                    if attempt == self.max_attempts:
                        raise
                    await asyncio.sleep(self.retry_backoff_s * attempt)
            g.seen[event.event_id] = None
            while len(g.seen) > self.seen_limit:
                g.seen.popitem(last=False)
        except Exception as exc:  # noqa: BLE001 - a poison message must not stall the partition
            self.dead.append((g.name, event, exc))
            self._send_to_dlq(g.name, msg, exc)

    def _send_to_dlq(self, group: str, msg, exc: Exception) -> None:
        try:
            self._producer.produce(self.dlq_topic, msg.value(), key=msg.key(),
                                   headers=[("group", group.encode()), ("reason", repr(exc)[:500].encode()),
                                            ("source", f"{msg.topic()}:{msg.partition()}:{msg.offset()}".encode())])
            self._producer.flush(5)
        except Exception:  # noqa: BLE001
            log.exception("could not dead-letter a message from %s", group)

    # ------------------------------------------------------------ settling (tests and shutdown)

    def pending(self) -> bool:
        """True while a group has not consumed everything this bus produced. Only meaningful on a topic of its own."""
        return any(g.consumed < self._delivered for g in self._groups if not g.broadcast)

    async def idle(self, timeout_s: float = 30.0) -> None:
        self.start()
        await self.ready()
        await asyncio.to_thread(self._producer.flush, 5)
        self._producer.poll(0)
        end = asyncio.get_running_loop().time() + timeout_s
        while self.pending():
            if asyncio.get_running_loop().time() > end:
                raise TimeoutError("consumer groups did not catch up")
            await asyncio.sleep(0.01)
