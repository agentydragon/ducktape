# Props Tests Performance Analysis

Generated: 2026-02-09

## Summary

**Total: 37 tests, 158s wall-clock time on RBE (BuildBuddy)**

Key findings from OTel traces:

| Operation                 | Total Time | Count | Mean  | Max   |
| ------------------------- | ---------- | ----- | ----- | ----- |
| e2e_registry startup      | 370.5s     | 13    | 28.5s | 76.3s |
| load_image (docker load)  | 361.2s     | 46    | 7.9s  | 17.6s |
| PostgresContainer startup | 67.3s      | 23    | 2.9s  | 4.6s  |
| recreate_database         | 34.7s      | 116   | 0.3s  | 0.6s  |
| sync_all                  | 20.7s      | 105   | 0.2s  | 0.3s  |
| ensure_database_exists    | 5.4s       | 116   | 0.05s | 0.3s  |

**Biggest bottleneck:** `e2e_registry startup` (370s) — the Docker registry container used by E2E tests.

## Test Execution Times

### Slowest Tests (RBE)

| Test                                          | Time   | Traced Overhead | Notes                         |
| --------------------------------------------- | ------ | --------------- | ----------------------------- |
| `//props/agents/critic_dev:test_e2e`          | 155.7s | 128.0s          | 3 sub-tests, registry: 104.7s |
| `//props/agents/critic_dev/optimize:test_e2e` | 104.3s | 85.1s           | registry: 66.8s               |
| `//props/agents/critic_dev/improve:test_e2e`  | 97.7s  | 70.4s           | registry: 49.3s               |
| `//props/agents/critic:test_e2e`              | 89.0s  | 68.9s           | registry: 49.7s               |
| `//props/agents/grader:test_e2e_sleep_wake`   | 80.9s  | 55.8s           | registry: 34.6s               |
| `//props/agents/grader:test_e2e`              | 79.5s  | 54.5s           | registry: 33.2s               |
| `//props/agents/grader:test_e2e_clustering`   | 77.0s  | 52.2s           | registry: 32.2s               |

### Database Tests

| Test                                         | Time  | Traced Overhead | Notes        |
| -------------------------------------------- | ----- | --------------- | ------------ |
| `//props/db:test_split_based_rls`            | 51.7s | 25.9s           | 13 sub-tests |
| `//props/db:test_agent_queries`              | 50.3s | 25.7s           | 8 sub-tests  |
| `//props/db:test_pydantic_column_null`       | 46.5s | 22.1s           | 4 sub-tests  |
| `//props/db:test_tp_occurrence_credits`      | 40.8s | —               |              |
| `//props/db:test_view_extracts_grade_fields` | 34.8s | —               |              |

### Fast Tests (<10s)

- `//props/core:test_agent_types` — 0.8s
- `//props/core:test_agent_workspace` — 0.5s
- `//props/db/migrations:test_migration_sequence` — 2.0s
- `//props/backend:test_auth` — 2.3s
- `//props/frontend:svelte_check_test` — 5.1s
- `//props/core:test_rationale` — 7.3s

## Root Causes

### 1. E2E Registry Startup (370s total, 28s mean)

The Docker registry container (`registry:2`) takes 28-76s to start on RBE. Each test function that uses `e2e_registry` fixture pays this cost separately.

**Key finding:** `critic_dev:test_e2e` has 3 test functions, each starting its own registry (76s + 19s + 9s = 104s just for registry startup).

The registry is function-scoped because each test needs to push images and record them in its own database. This trades isolation for speed.

Timeline for a typical E2E test:

```
 0.0s  load_image (ryuk)      1.2s
 1.2s  load_image (postgres) 16.3s
17.5s  PostgresContainer      3.2s
20.7s  ensure_database        0.1s
21.1s  recreate_database      0.3s
21.4s  sync_all               0.3s
21.6s  e2e_registry startup  33.2s  ← happens LAST, after DB ready
```

### 2. Image Loading (361s total, 7.9s mean)

Loading Bazel-bundled tarballs via `docker load`:

- `postgres:16` tarball: ~15s per load
- `ryuk` tarball: ~1s per load

On RBE, the Docker daemon starts with an empty cache, so every test session pays the full load cost.

### 3. PostgreSQL Container Startup (67s total, 2.9s mean)

Session-scoped PostgreSQL container startup. Each pytest session (Bazel test target) pays this once.

### 4. Per-Test Database Setup (~0.5s per test)

Fast operations:

- `ensure_database_exists`: ~50ms
- `recreate_database` (Alembic migrations): ~300ms
- `sync_all` (fixture sync): ~200ms

## Optimization Options

### High Impact

#### Option 1: Session-Scoped Registry for E2E Tests

**Impact:** Save ~300s (85% of registry overhead)

Change `e2e_registry` from function-scoped to session-scoped. All E2E tests in a target share one registry.

```python
@pytest.fixture(scope="session")
def e2e_registry() -> Generator[DockerContainer]:
    ...
```

**Trade-off:** Tests must handle image name collisions or use unique tags. Database isolation already exists (per-test DB), so registry sharing is safe if images use run-specific tags.

**Estimated savings:** For `critic_dev:test_e2e` alone: 104s → ~35s

#### Option 2: Runner Recycling (RBE)

**Impact:** Save ~300s on image loading

Enable `test.recycle-runner` in exec_properties to preserve Docker daemon state between test runs:

```python
DOCKER_EXEC_PROPERTIES = {
    "test.recycle-runner": "true",
}
```

This keeps the Docker image cache warm, reducing `docker load` from 15s to <1s.

**Trade-off:** Potential test pollution if tests don't clean up properly.

#### Option 3: Split E2E Tests Into Separate Bazel Targets

**Impact:** Reduce wall-clock time via parallelization

Currently `//props/agents/critic_dev:test_e2e` runs 3 tests sequentially (155s). Splitting into 3 targets allows parallel execution on RBE.

**Trade-off:** More Bazel targets to maintain. Total CPU time unchanged, but wall-clock time reduced.

### Medium Impact

#### Option 4: Preload Images in Session Fixture

**Impact:** Deduplicate image loading

Move image loading to a session-level `_preload_images` autouse fixture. Already done for E2E tests in `e2e_infra.py`, but DB tests still load per-session.

#### Option 5: Module-Scoped Synced Database

**Impact:** Save ~20s on sync_all

For read-only tests, share the synced database across test functions.

### Lower Impact

#### Option 6: Smaller Base Images

Reduce tarball sizes for faster `docker load`.

#### Option 7: Podman Instead of Docker

BuildBuddy RBE includes podman. May have faster cold-start (no daemon).

## Recommended Priority

1. **Session-scoped registry** — Highest ROI, minimal code change
2. **Runner recycling** — Needs testing for hermetic isolation
3. **Split E2E tests** — Good for CI parallelism
4. **Module-scoped synced DB** — Requires test refactoring
