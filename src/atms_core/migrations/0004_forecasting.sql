create table traffic_observation (
  segment text not null,
  ts timestamptz not null,
  speed_kmh double precision not null,
  flow_vph double precision not null,
  primary key (segment, ts)
);
create index traffic_observation_ts_idx on traffic_observation (ts);

create table weather_observation (
  kind text not null,                 -- observed or forecast
  ts timestamptz not null,
  rain_mm_h double precision not null,
  visibility_km double precision not null,
  primary key (kind, ts)
);

create table forecast_log (
  id bigserial primary key,
  segment text not null,
  made_at timestamptz not null,
  horizon_min integer not null,
  target_ts timestamptz not null,
  predicted double precision not null,
  low double precision not null,
  high double precision not null,
  level text not null,
  speed_now double precision not null,
  model_version text not null,
  actual double precision,
  abs_error double precision,
  resolved_at timestamptz,
  unique (segment, made_at, horizon_min)
);
create index forecast_log_open_idx on forecast_log (target_ts) where actual is null;
create index forecast_log_resolved_idx on forecast_log (resolved_at);
