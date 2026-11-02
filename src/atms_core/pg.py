"""Postgres and PostGIS: migrations, the repository, and facility search."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from atms_contracts.models import Agency, AssignmentState, IncidentState, Location, Severity

from .geo import AVG_SPEED_MS, ROAD_FACTOR, SEED, Facility, Ranked
from .repo import Batch, Loaded, StaleWrite
from .store import Assignment, Contact, Incident, Notification, Plan, TimelineEntry

log = logging.getLogger("atms.pg")
MIGRATIONS = Path(__file__).parent / "migrations"


def _options(schema: str | None) -> str | None:
    return f"-c search_path={schema},public" if schema else None


def migrate(url: str, schema: str | None = None) -> list[str]:
    """Apply pending migrations in order. Safe to run on every start. Returns the versions applied.

    A schema name isolates one copy of the tables, used by tests. PostGIS itself is installed once in public.
    """
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("create extension if not exists postgis schema public")
        if schema:
            c.execute(f'create schema if not exists "{schema}"')
    applied: list[str] = []
    with psycopg.connect(url, options=_options(schema)) as c:
        c.execute("create table if not exists schema_migrations (version text primary key, applied_at timestamptz not null default now())")
        done = {r[0] for r in c.execute("select version from schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in done:
                continue
            c.execute(path.read_text())
            c.execute("insert into schema_migrations(version) values (%s)", (path.stem,))
            applied.append(path.stem)
        c.commit()
    return applied


def seed_facilities(url: str, schema: str | None = None, facilities: list[Facility] | None = None) -> None:
    with psycopg.connect(url, options=_options(schema)) as c:
        for f in facilities or SEED:
            c.execute(
                "insert into facility(id, name, agency, lat, lon) values (%s,%s,%s,%s,%s) "
                "on conflict (id) do update set name=excluded.name, agency=excluded.agency, lat=excluded.lat, lon=excluded.lon",
                (f.id, f.name, f.agency.value, f.location.lat, f.location.lon))
        c.commit()


def seed_contacts(url: str, schema: str | None = None, contacts: list[Contact] | None = None) -> None:
    from .notifications import network_contacts

    with psycopg.connect(url, options=_options(schema)) as c:
        for k in contacts or network_contacts():
            c.execute(
                "insert into contact(id,name,channel,address,role,agency,road_segment,events,min_severity,secret) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set name=excluded.name, "
                "channel=excluded.channel, address=excluded.address, role=excluded.role, agency=excluded.agency, "
                "road_segment=excluded.road_segment, events=excluded.events, min_severity=excluded.min_severity, "
                "secret=excluded.secret",
                (k.id, k.name, k.channel, k.address, k.role, k.agency.value if k.agency else None, k.road_segment,
                 k.events, k.min_severity.value, k.secret))
        c.commit()


def drop_schema(url: str, schema: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'drop schema if exists "{schema}" cascade')


class PostgresRepo:
    def __init__(self, url: str, schema: str | None = None):
        self.url, self.schema = url, schema
        self._conn: psycopg.Connection | None = None

    def _c(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.url, options=_options(self.schema), row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def _retry(self, fn):
        """One reconnect for a dropped connection. Anything else, including StaleWrite, goes to the caller."""
        try:
            return fn(self._c())
        except psycopg.OperationalError:
            log.warning("database connection lost, reconnecting")
            self.close()
            return fn(self._c())

    # ------------------------------------------------------------ writing

    def write(self, b: Batch) -> None:
        def go(c: psycopg.Connection):
            with c.transaction():              # all or nothing for one operation
                self._write(c, b)
        self._retry(go)

    def _write(self, c: psycopg.Connection, b: Batch) -> None:
        for i in b.incidents:
            cur = c.execute(
                "insert into incident(id,state,severity,confidence,lat,lon,heading_deg,road_segment,opened_at,"
                "last_detection_at,trace_id,lanes,version,resolved_at,plan_id,plan_reverted,report_ref) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "on conflict (id) do update set state=excluded.state, severity=excluded.severity, "
                "confidence=excluded.confidence, last_detection_at=excluded.last_detection_at, lanes=excluded.lanes, "
                "version=excluded.version, resolved_at=excluded.resolved_at, plan_id=excluded.plan_id, "
                "plan_reverted=excluded.plan_reverted, report_ref=excluded.report_ref "
                "where incident.version <= excluded.version",
                (i.id, i.state.value, i.severity.value, i.confidence, i.location.lat, i.location.lon, i.heading_deg,
                 i.road_segment, i.opened_at, i.last_detection_at, i.trace_id, i.lanes, i.version, i.resolved_at,
                 i.plan_id, i.plan_reverted, i.report_ref))
            if cur.rowcount == 0:
                raise StaleWrite(f"incident {i.id} has a newer version stored")
        for event_id, incident_id, camera in b.detections:
            c.execute("insert into detection(event_id, incident_id, camera_id) values (%s,%s,%s) on conflict (event_id) do nothing",
                      (event_id, incident_id, camera))
        for event_id in b.low_confidence:
            c.execute("insert into low_confidence_detection(event_id) values (%s) on conflict do nothing", (event_id,))
        for iid, entries in b.timeline.items():
            for t in entries:
                c.execute("insert into timeline_entry(incident_id, at, kind, summary, actor) values (%s,%s,%s,%s,%s)",
                          (iid, t.at, t.kind, t.summary, t.actor))
        for iid, items in b.evidence.items():
            for e in items:
                c.execute("insert into evidence(incident_id, kind, ref, camera_id, sha256) values (%s,%s,%s,%s,%s)",
                          (iid, e["kind"], e["ref"], e.get("camera_id"), e.get("sha256")))
        for a in b.assignments:
            c.execute(
                "insert into assignment(id,incident_id,agency,state,requested_at,attempt,facility,unit_ref,eta_s,acked_at) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set state=excluded.state, "
                "attempt=excluded.attempt, facility=excluded.facility, unit_ref=excluded.unit_ref, eta_s=excluded.eta_s, "
                "acked_at=excluded.acked_at",
                (a.id, a.incident_id, a.agency.value, a.state.value, a.requested_at, a.attempt, a.facility, a.unit_ref,
                 a.eta_s, a.acked_at))
        for p in b.plans:
            c.execute("insert into traffic_plan(id,incident_id,lanes,messages,reverted) values (%s,%s,%s,%s,%s) "
                      "on conflict (id) do update set messages=excluded.messages, reverted=excluded.reverted",
                      (p.id, p.incident_id, p.lanes, Jsonb(p.messages), p.reverted))
        for ref, iid, body in b.reports:
            c.execute("insert into incident_report(ref, incident_id, body) values (%s,%s,%s) on conflict (ref) do nothing",
                      (ref, iid, Jsonb(body)))
        for n in b.notifications:
            c.execute(
                "insert into notification(id,incident_id,source_event_id,contact_id,contact_name,channel,address,kind,body,"
                "status,attempts,provider,provider_ref,last_error,created_at,next_attempt_at,sent_at,delivered_at) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                (n.id, n.incident_id, n.source_event_id, n.contact_id, n.contact_name, n.channel, n.address, n.kind, n.body,
                 n.status, n.attempts, n.provider, n.provider_ref, n.last_error, n.created_at, n.next_attempt_at,
                 n.sent_at, n.delivered_at))
            c.execute(
                "update notification set status=%s, attempts=%s, provider=%s, provider_ref=%s, last_error=%s, "
                "next_attempt_at=%s, sent_at=%s, delivered_at=%s where id=%s",
                (n.status, n.attempts, n.provider, n.provider_ref, n.last_error, n.next_attempt_at, n.sent_at,
                 n.delivered_at, n.id))
        for ev in b.events:
            c.execute("insert into outbox(event) values (%s)", (Jsonb(json.loads(ev.model_dump_json())),))
        for r in b.audit:
            c.execute("insert into audit_log(event_id,type,incident_id,producer,at,trace_id) values (%s,%s,%s,%s,%s,%s) "
                      "on conflict (event_id) do nothing",
                      (r["event_id"], r["type"], r["incident_id"], r["producer"], r["at"], r["trace_id"]))

    # ------------------------------------------------------------ loading

    def load(self, closed_within_days: int) -> Loaded:
        def go(c: psycopg.Connection) -> Loaded:
            out = Loaded()
            with c.transaction():
                rows = c.execute(
                    "select * from incident where state <> 'closed' or opened_at > now() - make_interval(days => %s)",
                    (closed_within_days,)).fetchall()
                ids = [r["id"] for r in rows]
                for r in rows:
                    out.incidents.append(Incident(
                        id=r["id"], state=IncidentState(r["state"]), severity=Severity(r["severity"]),
                        confidence=r["confidence"], location=Location(lat=r["lat"], lon=r["lon"]),
                        heading_deg=r["heading_deg"], road_segment=r["road_segment"], opened_at=r["opened_at"],
                        last_detection_at=r["last_detection_at"], trace_id=r["trace_id"], lanes=list(r["lanes"]),
                        version=r["version"], resolved_at=r["resolved_at"], plan_id=r["plan_id"],
                        plan_reverted=r["plan_reverted"], report_ref=r["report_ref"]))
                by_id = {i.id: i for i in out.incidents}
                for t in c.execute("select * from timeline_entry where incident_id = any(%s) order by id", (ids,)):
                    by_id[t["incident_id"]].timeline.append(TimelineEntry(t["at"], t["kind"], t["summary"], t["actor"]))
                for e in c.execute("select * from evidence where incident_id = any(%s) order by id", (ids,)):
                    by_id[e["incident_id"]].evidence.append(
                        {"kind": e["kind"], "ref": e["ref"], "camera_id": e["camera_id"], "sha256": e["sha256"]})
                out.detections = [(d["event_id"], d["incident_id"], d["camera_id"]) for d in c.execute(
                    "select event_id, incident_id, camera_id from detection where incident_id = any(%s) order by seq", (ids,))]
                out.assignments = [Assignment(
                    id=a["id"], incident_id=a["incident_id"], agency=Agency(a["agency"]), state=AssignmentState(a["state"]),
                    requested_at=a["requested_at"], attempt=a["attempt"], facility=a["facility"], unit_ref=a["unit_ref"],
                    eta_s=a["eta_s"], acked_at=a["acked_at"])
                    for a in c.execute("select * from assignment where incident_id = any(%s) order by requested_at", (ids,))]
                out.plans = [Plan(p["id"], p["incident_id"], list(p["lanes"]), p["messages"], p["reverted"])
                             for p in c.execute("select * from traffic_plan where incident_id = any(%s)", (ids,))]
                out.reports = {r["ref"]: r["body"] for r in c.execute(
                    "select ref, body from incident_report where incident_id = any(%s)", (ids,))}
                out.notifications = [Notification(
                    id=n["id"], incident_id=n["incident_id"], source_event_id=n["source_event_id"], contact_id=n["contact_id"],
                    contact_name=n["contact_name"], channel=n["channel"], address=n["address"], kind=n["kind"], body=n["body"],
                    status=n["status"], created_at=n["created_at"], next_attempt_at=n["next_attempt_at"],
                    attempts=n["attempts"], provider=n["provider"], provider_ref=n["provider_ref"],
                    last_error=n["last_error"], sent_at=n["sent_at"], delivered_at=n["delivered_at"])
                    for n in c.execute("select * from notification where incident_id = any(%s)", (ids,))]
                out.contacts = [Contact(
                    id=k["id"], name=k["name"], channel=k["channel"], address=k["address"], role=k["role"],
                    events=list(k["events"]), agency=Agency(k["agency"]) if k["agency"] else None,
                    road_segment=k["road_segment"], min_severity=Severity(k["min_severity"]), secret=k["secret"])
                    for k in c.execute("select * from contact where enabled")]
                out.low_confidence = [r["event_id"] for r in c.execute(
                    "select event_id from low_confidence_detection order by recorded_at desc limit 1000")]
            return out
        return self._retry(go)

    # ------------------------------------------------------------ outbox

    def outbox_pending(self, limit: int):
        from atms_contracts.models import parse_event

        rows = self._retry(lambda c: c.execute(
            "select id, event from outbox where sent_at is null order by id limit %s", (limit,)).fetchall())
        return [(r["id"], parse_event(r["event"])) for r in rows]

    def outbox_mark_sent(self, ids: list[int]) -> None:
        self._retry(lambda c: c.execute("update outbox set sent_at = now() where id = any(%s)", (ids,)))

    def outbox_purge(self, older_than_days: int) -> int:
        return self._retry(lambda c: c.execute(
            "delete from outbox where sent_at < now() - make_interval(days => %s)", (older_than_days,)).rowcount)

    # ------------------------------------------------------------ idempotency

    def idempotency_get(self, key: tuple[str, str, str, str]) -> tuple[int, dict] | None:
        row = self._retry(lambda c: c.execute(
            "select status, body from idempotency_key where principal=%s and method=%s and path=%s and key=%s", key).fetchone())
        return (row["status"], row["body"]) if row else None

    def idempotency_put(self, key: tuple[str, str, str, str], status: int, body: dict) -> None:
        def go(c: psycopg.Connection):
            with c.transaction():
                c.execute("insert into idempotency_key(principal,method,path,key,status,body) values (%s,%s,%s,%s,%s,%s) "
                          "on conflict do nothing", (*key, status, Jsonb(body)))
        self._retry(go)


class PostgresFacilities:
    """Nearest facilities with PostGIS. Distances are on the spheroid, so they differ slightly from the haversine seed."""

    def __init__(self, repo: PostgresRepo):
        self.repo = repo

    def all(self) -> list[Facility]:
        rows = self.repo._retry(lambda c: c.execute("select id, name, agency, lat, lon from facility order by id").fetchall())
        return [Facility(r["id"], r["name"], Agency(r["agency"]), Location(lat=r["lat"], lon=r["lon"])) for r in rows]

    def nearest(self, agency: Agency, where: Location, n: int = 3) -> list[Ranked]:
        rows = self.repo._retry(lambda c: c.execute(
            "select id, name, lat, lon, ST_Distance(geog, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography) as d "
            "from facility where agency = %(agency)s "
            "order by geog <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography limit %(n)s",
            {"lon": where.lon, "lat": where.lat, "agency": agency.value, "n": n}).fetchall())
        return [Ranked(Facility(r["id"], r["name"], agency, Location(lat=r["lat"], lon=r["lon"])), r["d"],
                       round(r["d"] * ROAD_FACTOR / AVG_SPEED_MS)) for r in rows]
