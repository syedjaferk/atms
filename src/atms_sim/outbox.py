"""Durable local outbox. A detection is written here before any publish attempt."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class OutboxRow:
    event_id: str
    topic: str
    payload: bytes
    attempts: int


class Outbox:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path)
        self._db.execute(
            "create table if not exists outbox ("
            " event_id text primary key, topic text not null, payload blob not null,"
            " created_at real not null, attempts integer not null default 0)"
        )
        self._db.commit()

    def add(self, event_id: str, topic: str, payload: bytes) -> None:
        # insert or ignore keeps a repeated add of the same event_id harmless
        self._db.execute(
            "insert or ignore into outbox(event_id, topic, payload, created_at) values (?,?,?,?)",
            (event_id, topic, payload, time.time()),
        )
        self._db.commit()

    def pending(self, limit: int = 100) -> list[OutboxRow]:
        rows = self._db.execute(
            "select event_id, topic, payload, attempts from outbox order by created_at, rowid limit ?",
            (limit,),
        ).fetchall()
        return [OutboxRow(*r) for r in rows]

    def mark_attempt(self, event_id: str) -> None:
        self._db.execute("update outbox set attempts = attempts + 1 where event_id = ?", (event_id,))
        self._db.commit()

    def delete(self, event_id: str) -> None:
        self._db.execute("delete from outbox where event_id = ?", (event_id,))
        self._db.commit()

    def depth(self) -> int:
        return self._db.execute("select count(*) from outbox").fetchone()[0]
