-- Журналы (шаг 9): решения политики и план хода связаны сквозным trace_id.
alter table policy_events add column if not exists trace_id text;
create index if not exists policy_events_trace_idx on policy_events(trace_id);

create table if not exists turn_plans(
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    trace_id   text,
    session_id text,
    plan       jsonb not null,
    gated      jsonb not null default '[]',
    dropped    jsonb not null default '[]'
);
create index if not exists turn_plans_trace_idx on turn_plans(trace_id);
