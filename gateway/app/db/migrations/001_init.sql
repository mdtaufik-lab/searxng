-- Search gateway schema. Run this in the Supabase SQL editor.

create extension if not exists pgcrypto;

create table if not exists api_keys (
  id                uuid primary key default gen_random_uuid(),
  key_hash          text not null unique,   -- sha256 hex of the raw key; the raw key is never stored
  key_prefix        text not null,          -- e.g. 'sk_live_a1b2c3' -- for display only
  name              text,
  cx_allowlist      text[],                 -- null = every profile allowed
  rate_limit_rpm    int    not null default 60,
  rate_limit_burst  int    not null default 20,
  quota_period      text   not null default 'month' check (quota_period in ('day','month')),
  quota_limit       bigint,                 -- null = unlimited
  active            boolean not null default true,
  expires_at        timestamptz,
  created_at        timestamptz not null default now(),
  last_used_at      timestamptz
);

-- SHA-256 rather than bcrypt/argon2 is correct here, and is not a shortcut.
-- API keys are 256-bit random (secrets.token_urlsafe(32)), so there is no
-- dictionary to attack; a deliberately slow KDF would add latency to EVERY
-- request and buy nothing. Password-hashing rules do not apply to random secrets.

create table if not exists api_quota_counters (
  api_key_id   uuid not null references api_keys(id) on delete cascade,
  period_start date not null,
  used         bigint not null default 0,
  primary key (api_key_id, period_start)
);

create table if not exists api_usage (
  id             bigserial primary key,
  api_key_id     uuid not null references api_keys(id) on delete cascade,
  ts             timestamptz not null default now(),
  endpoint       text not null,          -- 'customsearch' | 'native'
  cx             text,
  query_hash     text,                   -- sha256 of the normalized query, NOT its text
  query_text     text,                   -- only populated when LOG_RAW_QUERIES=true
  status         int  not null,
  cache_hit      boolean not null default false,
  upstream_pages int  not null default 0,
  latency_ms     int,
  degraded       boolean not null default false
);

create index if not exists api_usage_key_ts_idx on api_usage (api_key_id, ts desc);

-- Deny by default. The gateway connects with the service role and bypasses RLS;
-- these policies exist so that if you later expose these tables to end users via
-- Supabase's client SDK, they cannot read each other's keys or usage.
alter table api_keys            enable row level security;
alter table api_quota_counters  enable row level security;
alter table api_usage           enable row level security;
