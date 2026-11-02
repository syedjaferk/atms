create table contact (
  id text primary key,
  name text not null,
  channel text not null,
  address text not null,
  role text not null,
  agency text,
  road_segment text,
  events text[] not null,
  min_severity text not null default 'minor',
  secret text,
  enabled boolean not null default true
);

create table notification (
  id uuid primary key,
  incident_id uuid not null references incident (id),
  source_event_id uuid not null,
  contact_id text not null,
  contact_name text not null,
  channel text not null,
  address text not null,
  kind text not null,
  body text not null,
  status text not null,
  attempts integer not null default 0,
  provider text,
  provider_ref text,
  last_error text,
  created_at timestamptz not null,
  next_attempt_at timestamptz not null,
  sent_at timestamptz,
  delivered_at timestamptz,
  unique (source_event_id, contact_id)
);
create index notification_incident_idx on notification (incident_id);
create index notification_open_idx on notification (status) where status in ('pending', 'sent');
create index notification_receipt_idx on notification (provider, provider_ref);
