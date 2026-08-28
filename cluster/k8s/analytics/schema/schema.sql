CREATE DATABASE IF NOT EXISTS aiquota ON CLUSTER analytics;

CREATE TABLE IF NOT EXISTS aiquota.raw_http_observations ON CLUSTER analytics
(
  event_id UUID,
  schema_version UInt16,
  dataset LowCardinality(String),
  source LowCardinality(String),
  observed_at DateTime64(3, 'UTC'),
  ingested_at DateTime64(3, 'UTC'),
  status_code UInt16,
  content_type LowCardinality(String),
  raw_body_base64 String CODEC(ZSTD(6)),
  raw_body_sha256 FixedString(64),
  raw_body_size_bytes UInt32,
  raw_body_truncated Bool,
  quota_windows Array(Tuple(
    window_name String,
    used_percent Float64,
    remaining_percent Float64,
    reset_at Nullable(DateTime64(3, 'UTC')),
    reset_seconds Float64,
    window_seconds Float64,
    extra_spend_enabled Nullable(Bool),
    extra_spend_limit_usd Nullable(Float64),
    extra_spend_used_usd Nullable(Float64),
    extra_spend_utilization Nullable(Float64)
  )),
  token_activity Array(Tuple(
    start_date Date,
    tokens Int64
  )),
  reset_credits Array(Tuple(
    credit_id String,
    reset_type String,
    status String,
    granted_at DateTime64(3, 'UTC'),
    expires_at Nullable(DateTime64(3, 'UTC'))
  )),
  normalized_body String CODEC(ZSTD(6)),
  error String CODEC(ZSTD(3))
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/aiquota/raw_http_observations', '{replica}')
PARTITION BY toYYYYMM(observed_at)
ORDER BY (dataset, source, observed_at, event_id)
TTL observed_at + INTERVAL 1 YEAR DELETE;

-- Columns added after the table already existed; the CREATE above carries them
-- for a fresh install, these ALTERs for an existing one.
ALTER TABLE aiquota.raw_http_observations ON CLUSTER analytics
  ADD COLUMN IF NOT EXISTS token_activity Array(Tuple(
    start_date Date,
    tokens Int64
  )) AFTER quota_windows;

ALTER TABLE aiquota.raw_http_observations ON CLUSTER analytics
  ADD COLUMN IF NOT EXISTS reset_credits Array(Tuple(
    credit_id String,
    reset_type String,
    status String,
    granted_at DateTime64(3, 'UTC'),
    expires_at Nullable(DateTime64(3, 'UTC'))
  )) AFTER token_activity;

CREATE TABLE IF NOT EXISTS aiquota.aiquota_windows ON CLUSTER analytics
(
  event_id UUID,
  observed_at DateTime64(3, 'UTC'),
  provider LowCardinality(String),
  window_name LowCardinality(String),
  used_percent Float64,
  remaining_percent Float64,
  reset_at Nullable(DateTime64(3, 'UTC')),
  reset_seconds Float64,
  window_seconds Float64,
  extra_spend_enabled Nullable(Bool),
  extra_spend_limit_usd Nullable(Float64),
  extra_spend_used_usd Nullable(Float64),
  extra_spend_utilization Nullable(Float64)
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/aiquota/aiquota_windows', '{replica}')
PARTITION BY toYYYYMM(observed_at)
ORDER BY (provider, window_seconds, window_name, observed_at, event_id)
TTL observed_at + INTERVAL 5 YEAR DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.aiquota_windows_mv ON CLUSTER analytics
TO aiquota.aiquota_windows
AS SELECT
  event_id,
  observed_at,
  source AS provider,
  w.window_name AS window_name,
  w.used_percent AS used_percent,
  w.remaining_percent AS remaining_percent,
  w.reset_at AS reset_at,
  w.reset_seconds AS reset_seconds,
  w.window_seconds AS window_seconds,
  w.extra_spend_enabled AS extra_spend_enabled,
  w.extra_spend_limit_usd AS extra_spend_limit_usd,
  w.extra_spend_used_usd AS extra_spend_used_usd,
  w.extra_spend_utilization AS extra_spend_utilization
FROM aiquota.raw_http_observations
ARRAY JOIN quota_windows AS w;

-- Every poll of a history endpoint restates the whole series, so these keep one
-- row per (day, observation) rather than collapsing: the repeated readings of
-- the current day are what shows usage accruing within it. For the settled
-- total of a past day, take argMax(tokens, observed_at) grouped by start_date.
CREATE TABLE IF NOT EXISTS aiquota.token_activity_daily ON CLUSTER analytics
(
  event_id UUID,
  observed_at DateTime64(3, 'UTC'),
  provider LowCardinality(String),
  start_date Date,
  tokens Int64
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/aiquota/token_activity_daily', '{replica}')
PARTITION BY toYYYYMM(observed_at)
ORDER BY (provider, start_date, observed_at, event_id)
TTL observed_at + INTERVAL 5 YEAR DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.token_activity_daily_mv ON CLUSTER analytics
TO aiquota.token_activity_daily
AS SELECT
  event_id,
  observed_at,
  source AS provider,
  t.start_date AS start_date,
  t.tokens AS tokens
FROM aiquota.raw_http_observations
ARRAY JOIN token_activity AS t;

-- A credit's status changes as it is granted, redeemed, or expires, so the row
-- per observation is the point: it dates the transition.
CREATE TABLE IF NOT EXISTS aiquota.reset_credits ON CLUSTER analytics
(
  event_id UUID,
  observed_at DateTime64(3, 'UTC'),
  provider LowCardinality(String),
  credit_id String,
  reset_type LowCardinality(String),
  status LowCardinality(String),
  granted_at DateTime64(3, 'UTC'),
  expires_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/aiquota/reset_credits', '{replica}')
PARTITION BY toYYYYMM(observed_at)
ORDER BY (provider, credit_id, observed_at, event_id)
TTL observed_at + INTERVAL 5 YEAR DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.reset_credits_mv ON CLUSTER analytics
TO aiquota.reset_credits
AS SELECT
  event_id,
  observed_at,
  source AS provider,
  c.credit_id AS credit_id,
  c.reset_type AS reset_type,
  c.status AS status,
  c.granted_at AS granted_at,
  c.expires_at AS expires_at
FROM aiquota.raw_http_observations
ARRAY JOIN reset_credits AS c;
