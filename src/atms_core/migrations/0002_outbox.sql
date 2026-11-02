-- Events are written in the same transaction as the state change that caused them,
-- then relayed to the bus. A crash between the two loses nothing.
create table outbox (
  id bigserial primary key,
  event jsonb not null,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index outbox_unsent_idx on outbox (id) where sent_at is null;
