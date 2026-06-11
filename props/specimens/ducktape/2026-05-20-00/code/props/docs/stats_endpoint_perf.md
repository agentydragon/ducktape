# Stats Endpoint Performance Problem

**Date**: 2026-02-23
**Endpoint**: `GET /api/stats/definitions/{image_digest}`
**Measured**: 17.9 seconds execution time for 3 result rows

## Root Cause: Non-Materialized View Chain

The endpoint queries `recall_by_definition_split_kind`, which is a 3-level deep chain of
non-materialized PostgreSQL views. Every request recomputes the entire aggregation from
scratch:

```
recall_by_definition_split_kind          ← endpoint queries this
  ├─ CTE run_stats    → recall_by_definition_example  (1st expansion)
  └─ CTE example_counts → recall_by_definition_example  (2nd expansion)
       └─ recall_by_definition_example
            └─ recall_by_run
                 └─ examples (UNION of snapshots + file_sets)
                      └─ is_tp_in_expected_recall_scope()  ← hot path
```

`recall_by_definition_split_kind` references `recall_by_definition_example` in **two
separate CTEs** (`run_stats` and `example_counts`). PostgreSQL expands the full view
chain independently for each, doubling the work.

## Bottleneck: `is_tp_in_expected_recall_scope()` Function

The `examples` view computes `recall_denominator` for each example by calling
`is_tp_in_expected_recall_scope()` as a correlated subplan. For `file_set` examples,
this runs once per `(file_set, true_positive_occurrence)` pair:

- 671 file_sets x ~7 true_positive_occurrences per snapshot = ~4,697 function calls
  **per expansion** of the examples view
- The view is expanded multiple times (once per CTE, once per `recall_by_run` join path)
- Total: ~14,000 function calls across the full query

## EXPLAIN ANALYZE Key Metrics

| Metric             | Value      |
| ------------------ | ---------- |
| Execution time     | 17,886 ms  |
| Planning time      | 19 ms      |
| Shared buffer hits | 15,806,835 |
| Disk reads         | 0          |
| Result rows        | 3          |
| `agent_runs` rows  | 7          |
| `examples` rows    | 687        |

The data is tiny and 100% cached in shared buffers. The cost is entirely CPU from
recomputing the view chain and running `is_tp_in_expected_recall_scope()` thousands
of times.

## Fix Options

### 1. Materialized Views (recommended)

Convert `recall_by_run` or `recall_by_definition_example` to `MATERIALIZED VIEW`.
Refresh after each batch of critic runs completes:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY recall_by_definition_example;
```

Pros: Reads become instant table scans. `CONCURRENTLY` allows reads during refresh.
Cons: Requires unique index on the materialized view. Stats are stale until refresh.

### 2. Application-Level Cache

Cache the endpoint response keyed by `(image_digest, max_agent_run_updated_at)`. Stats
only change when new runs complete, so cache invalidation is straightforward.

Pros: No schema changes. Cons: First request after new runs still slow.

### 3. Denormalize `recall_denominator`

Pre-compute and store `recall_denominator` on the `examples` table instead of
recalculating via `is_tp_in_expected_recall_scope()` on every read. Update it when
ground truth changes (rare).

Pros: Eliminates the hot-path function calls entirely.
Cons: Requires migration and trigger/app logic to keep denormalized column in sync.

### 4. Combine CTEs in `recall_by_definition_split_kind`

Rewrite the view to scan `recall_by_definition_example` once instead of twice (merge
`run_stats` and `example_counts` into a single pass). This halves the work but doesn't
address the fundamental cost of recomputing the full view chain.

## Reproduction

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM recall_by_definition_split_kind
WHERE critic_image_digest = 'sha256:...'
AND split IN ('train', 'valid');
```
