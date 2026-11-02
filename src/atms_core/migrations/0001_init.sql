create table incident (
  id uuid primary key,
  state text not null,
  severity text not null,
  confidence double precision not null,
  lat double precision not null,
  lon double precision not null,
  geog geography(Point, 4326) generated always as (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) stored,
  heading_deg double precision not null,
  road_segment text not null,
  opened_at timestamptz not null,
  last_detection_at timestamptz not null,
  trace_id text not null,
  lanes text[] not null,
  version integer not null,
  resolved_at timestamptz,
  plan_id uuid,
  plan_reverted boolean not null default false,
  report_ref text
);
create index incident_state_idx on incident (state);
create index incident_road_idx on incident (road_segment, last_detection_at);
create index incident_geog_idx on incident using gist (geog);

create table detection (
  seq bigserial primary key,
  event_id uuid not null unique,
  incident_id uuid not null references incident (id),
  camera_id text not null
);
create index detection_incident_idx on detection (incident_id);

create table low_confidence_detection (
  event_id uuid primary key,
  recorded_at timestamptz not null default now()
);

create table evidence (
  id bigserial primary key,
  incident_id uuid not null references incident (id),
  kind text not null,
  ref text not null,
  camera_id text,
  sha256 text
);
create index evidence_incident_idx on evidence (incident_id);

create table timeline_entry (
  id bigserial primary key,
  incident_id uuid not null references incident (id),
  at timestamptz not null,
  kind text not null,
  summary text not null,
  actor text
);
create index timeline_incident_idx on timeline_entry (incident_id, id);

create table assignment (
  id uuid primary key,
  incident_id uuid not null references incident (id),
  agency text not null,
  state text not null,
  requested_at timestamptz not null,
  attempt integer not null,
  facility text,
  unit_ref text,
  eta_s integer,
  acked_at timestamptz
);
create index assignment_incident_idx on assignment (incident_id);

create table traffic_plan (
  id uuid primary key,
  incident_id uuid not null unique references incident (id),
  lanes text[] not null,
  messages jsonb not null,
  reverted boolean not null default false
);

create table incident_report (
  ref text primary key,
  incident_id uuid not null references incident (id),
  body jsonb not null,
  created_at timestamptz not null default now()
);

create table audit_log (
  event_id uuid primary key,
  type text not null,
  incident_id uuid,
  producer text not null,
  at timestamptz not null,
  trace_id text not null
);
create index audit_incident_idx on audit_log (incident_id, at);

create table idempotency_key (
  principal text not null,
  method text not null,
  path text not null,
  key text not null,
  status integer not null,
  body jsonb not null,
  created_at timestamptz not null default now(),
  primary key (principal, method, path, key)
);

create table facility (
  id text primary key,
  name text not null,
  agency text not null,
  lat double precision not null,
  lon double precision not null,
  geog geography(Point, 4326) generated always as (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) stored
);
create index facility_geog_idx on facility using gist (geog);
create index facility_agency_idx on facility (agency);
