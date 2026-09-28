# PG 18 Materialized View + SQL Function Inlining Failure

## Problem Statement

The props backend crash-loops on PG 18 because Alembic migration
`20260224000000_materialize_examples` fails when creating:

```sql
CREATE MATERIALIZED VIEW examples AS
  SELECT ... is_tp_in_expected_recall_scope(...) ...
```

Error: `relation "critic_scopes_expected_to_recall" does not exist`
— with error context: `SQL function "is_tp_in_expected_recall_scope" during inlining`

The function `is_tp_in_expected_recall_scope` is `LANGUAGE sql STABLE` and references
`critic_scopes_expected_to_recall` and other tables using unqualified names.

## Root Cause

**`RestrictSearchPath()` during materialized view creation.**

### The commit chain

1. **`2af07e2f74`** (2024-03-04, Jeff Davis) — "Fix search_path to a safe value during
   maintenance operations" — added `RestrictSearchPath()` to `REFRESH MATERIALIZED VIEW`.
   Present in PG 17+.

2. **`4b74ebf726`** (2024-07-16, Jeff Davis) — "When creating materialized views, use
   REFRESH to load data" — made `CREATE MATERIALIZED VIEW ... WITH DATA` use the REFRESH
   code path, inheriting the restricted search_path. Present in PG 18+.

### The mechanism

In `src/backend/commands/matview.c:194`, `RefreshMatViewByOid()` calls:

```c
RestrictSearchPath();
```

Which is defined in `src/backend/utils/misc/guc.c` as:

```c
#define GUC_SAFE_SEARCH_PATH "pg_catalog, pg_temp"

void RestrictSearchPath(void)
{
    set_config_option("search_path", GUC_SAFE_SEARCH_PATH, ...);
}
```

This forces `search_path = 'pg_catalog, pg_temp'`, stripping `public` entirely.

When the planner then tries to **inline** a `LANGUAGE sql` function, it re-parses the
function body text (`prosrc`) via `transformTopLevelStmt` in
`src/backend/optimizer/util/clauses.c:5112`. Table name resolution uses the **current**
`search_path` GUC — which is now `pg_catalog, pg_temp`. Unqualified references to tables
in `public` fail with "relation does not exist".

The `createas.c` code at lines 291-293 documents this intentionally:

```c
/*
 * For materialized views, reuse the REFRESH logic, which locks down
 * security-restricted operations and restricts the search_path.  This
 * reduces the chance that a subsequent refresh will fail.
 */
```

### Why the inlining code doesn't catch this gracefully

The `inline_function` code in `clauses.c` re-parses the function body and runs parse
analysis (`transformTopLevelStmt`). If the function body references tables, parse analysis
tries to resolve them. Under normal search_path, this succeeds, the resulting querytree
has a non-empty `rtable`, and inlining bails out gracefully (`goto fail`). Under the
restricted search_path, parse analysis throws an ERROR before inlining can bail out.

### Version matrix

| PG Version | `RestrictSearchPath` in REFRESH | CREATE MATVIEW uses REFRESH | Affected?                             |
| ---------- | ------------------------------- | --------------------------- | ------------------------------------- |
| 16         | No                              | No                          | No                                    |
| 17         | Yes                             | No                          | No (CREATE works, REFRESH would fail) |
| 18+        | Yes                             | Yes                         | **Yes**                               |

### Additional findings

- `prosqlbody` is only populated for `BEGIN ATOMIC...END` style functions (PG 14+).
  Old-style `AS $$ ... $$` functions have `prosqlbody = NULL`, so the prosrc re-parsing
  path is always taken during inlining.
- The `inline_function` code in `clauses.c` was essentially unchanged by the PG 18 plan
  caching commit (`0dca5d68d`). The regression comes entirely from the matview code change.

## Fix Applied

Added `SET search_path = public` to `is_tp_in_expected_recall_scope` via
`ALTER FUNCTION ... SET search_path = public` at the start of the
`20260224000000_materialize_examples` migration.

This sets `proconfig` on the function, which causes the optimizer to skip inlining
entirely (check at `clauses.c:5009`: `!heap_attisnull(func_tuple, Anum_pg_proc_proconfig, NULL)`).
The function then executes normally with `search_path = public` regardless of the
caller's restricted search_path.

## Test Coverage

`props/db/migrations/test_materialize_examples.py`:

- `test_pg18_unqualified_table_in_function_fails_in_matview` — documents the PG 18 failure
- `test_set_search_path_fixes_matview_creation` — verifies the fix mechanism
- `test_materialize_examples_migration` — runs the full migration chain on PG 18

## Source Code References

- PG source: `/code/github.com/postgres/postgres`
- Restricted search_path: `src/backend/commands/matview.c:194` (`RestrictSearchPath()`)
- `RestrictSearchPath` definition: `src/backend/utils/misc/guc.c:2121-2126`
- `GUC_SAFE_SEARCH_PATH`: `src/backend/utils/misc/guc.c:75` (`"pg_catalog, pg_temp"`)
- CREATE MATVIEW using REFRESH: `src/backend/commands/createas.c:339-348`
- Inlining code: `src/backend/optimizer/util/clauses.c:4974` (`inline_function`)
- proconfig bail-out: `src/backend/optimizer/util/clauses.c:5009`
- Function body re-parsing: `src/backend/optimizer/util/clauses.c:5104-5112`
