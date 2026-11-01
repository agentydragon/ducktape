# Obsolete Langfuse materialized views

The two local views below were present on both ClickHouse replicas on 2026-11-01. Their source `traces_null` and target `traces_30d_amt`/`traces_7d_amt` tables were absent. Captured before the guarded Flux cleanup, for review and recovery. Their association with the v3-to-v4 migration is suspected, not proven.

```sql
CREATE MATERIALIZED VIEW langfuse.traces_30d_amt_mv TO langfuse.traces_30d_amt
(
    `project_id` String,
    `id` String,
    `timestamp` DateTime64(3),
    `start_time` DateTime64(3),
    `end_time` DateTime64(3),
    `name` Nullable(String),
    `metadata` Map(String, String),
    `user_id` Nullable(String),
    `session_id` Nullable(String),
    `environment` String,
    `tags` Array(String),
    `version` Nullable(String),
    `release` Nullable(String),
    `bookmarked` AggregateFunction(argMax, Nullable(Bool), DateTime64(3)),
    `public` AggregateFunction(argMax, Nullable(Bool), DateTime64(3)),
    `observation_ids` Array(String),
    `score_ids` Array(String),
    `cost_details` Map(String, Decimal(38, 12)),
    `usage_details` Map(String, UInt64),
    `input` AggregateFunction(argMax, String, DateTime64(3)),
    `output` AggregateFunction(argMax, String, DateTime64(3)),
    `created_at` DateTime64(3),
    `updated_at` DateTime64(3)
)
AS SELECT
    tn.project_id AS project_id,
    tn.id AS id,
    min(tn.start_time) AS timestamp,
    min(tn.start_time) AS start_time,
    max(coalesce(tn.end_time, tn.start_time)) AS end_time,
    anyLast(tn.name) AS name,
    maxMap(tn.metadata) AS metadata,
    anyLast(tn.user_id) AS user_id,
    anyLast(tn.session_id) AS session_id,
    anyLast(tn.environment) AS environment,
    groupUniqArrayArray(tn.tags) AS tags,
    anyLast(tn.version) AS version,
    anyLast(tn.release) AS release,
    argMaxState(tn.bookmarked, if(tn.bookmarked IS NOT NULL, tn.event_ts, toDateTime64(0, 3))) AS bookmarked,
    argMaxState(tn.public, if(tn.public IS NOT NULL, tn.event_ts, toDateTime64(0, 3))) AS public,
    groupUniqArrayArray(tn.observation_ids) AS observation_ids,
    groupUniqArrayArray(tn.score_ids) AS score_ids,
    sumMap(tn.cost_details) AS cost_details,
    sumMap(tn.usage_details) AS usage_details,
    argMaxState(tn.input, if(tn.input != '', tn.event_ts, toDateTime64(0, 3))) AS input,
    argMaxState(tn.output, if(tn.output != '', tn.event_ts, toDateTime64(0, 3))) AS output,
    min(tn.created_at) AS created_at,
    max(tn.updated_at) AS updated_at
FROM langfuse.traces_null AS tn
GROUP BY
    project_id,
    id;

CREATE MATERIALIZED VIEW langfuse.traces_7d_amt_mv TO langfuse.traces_7d_amt
(
    `project_id` String,
    `id` String,
    `timestamp` DateTime64(3),
    `start_time` DateTime64(3),
    `end_time` DateTime64(3),
    `name` Nullable(String),
    `metadata` Map(String, String),
    `user_id` Nullable(String),
    `session_id` Nullable(String),
    `environment` String,
    `tags` Array(String),
    `version` Nullable(String),
    `release` Nullable(String),
    `bookmarked` AggregateFunction(argMax, Nullable(Bool), DateTime64(3)),
    `public` AggregateFunction(argMax, Nullable(Bool), DateTime64(3)),
    `observation_ids` Array(String),
    `score_ids` Array(String),
    `cost_details` Map(String, Decimal(38, 12)),
    `usage_details` Map(String, UInt64),
    `input` AggregateFunction(argMax, String, DateTime64(3)),
    `output` AggregateFunction(argMax, String, DateTime64(3)),
    `created_at` DateTime64(3),
    `updated_at` DateTime64(3)
)
AS SELECT
    tn.project_id AS project_id,
    tn.id AS id,
    min(tn.start_time) AS timestamp,
    min(tn.start_time) AS start_time,
    max(coalesce(tn.end_time, tn.start_time)) AS end_time,
    anyLast(tn.name) AS name,
    maxMap(tn.metadata) AS metadata,
    anyLast(tn.user_id) AS user_id,
    anyLast(tn.session_id) AS session_id,
    anyLast(tn.environment) AS environment,
    groupUniqArrayArray(tn.tags) AS tags,
    anyLast(tn.version) AS version,
    anyLast(tn.release) AS release,
    argMaxState(tn.bookmarked, if(tn.bookmarked IS NOT NULL, tn.event_ts, toDateTime64(0, 3))) AS bookmarked,
    argMaxState(tn.public, if(tn.public IS NOT NULL, tn.event_ts, toDateTime64(0, 3))) AS public,
    groupUniqArrayArray(tn.observation_ids) AS observation_ids,
    groupUniqArrayArray(tn.score_ids) AS score_ids,
    sumMap(tn.cost_details) AS cost_details,
    sumMap(tn.usage_details) AS usage_details,
    argMaxState(tn.input, if(tn.input != '', tn.event_ts, toDateTime64(0, 3))) AS input,
    argMaxState(tn.output, if(tn.output != '', tn.event_ts, toDateTime64(0, 3))) AS output,
    min(tn.created_at) AS created_at,
    max(tn.updated_at) AS updated_at
FROM langfuse.traces_null AS tn
GROUP BY
    project_id,
    id;
```
