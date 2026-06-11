# E2E Testing with Bazel: Design and Recommendations

This document outlines the design for running complex E2E tests that require external infrastructure (PostgreSQL, Docker, registries) within a Bazel-based CI system.

## Current State

### Test Tag Usage

Tests declare infrastructure requirements via Bazel tags:

| Tag                      | Meaning                         | Current Usage                              |
| ------------------------ | ------------------------------- | ------------------------------------------ |
| `requires_docker`        | Needs Docker daemon             | `agent_server/`, `editor_agent/`, `props/` |
| `requires_postgres`      | Needs PostgreSQL                | `props/`, `gatelet/`                       |
| `requires_runtime_image` | Needs pre-built container image | `agent_server/`                            |
| `e2e`                    | Full end-to-end test            | `props/` E2E tests                         |
| `visual`                 | Visual regression test          | `props/frontend/`                          |
| `manual`                 | Excluded from `//...`           | Various                                    |

### Docker Test Infrastructure

Docker test utilities are consolidated in `//test_util`:

```python
from test_util.docker import (
    load_bazel_image,       # Load OCI image from Bazel oci_load target
    python_slim_image,       # Session fixture for python-slim image
    pytest_runtest_setup,    # Hook for skipping unavailable Docker tests
)
```

**Pattern for Docker tests:**

1. Add `tags = ["requires_docker"]` to the Bazel test target
2. Import `pytest_runtest_setup` in conftest.py (auto-skips if Docker unavailable)
3. Use fixtures from `test_util.docker` or `mcp_infra/testing/docker_fixtures.py`

### Props E2E Tests (Testcontainers)

Props E2E tests use **testcontainers** for hermetic infrastructure. See `props/testing/fixtures/e2e_infra.py`:

```python
@pytest.fixture(scope="session")
def e2e_registry() -> Generator[DockerContainer]:
    """Session-scoped Docker registry for e2e tests."""
    with DockerContainer("registry:2").with_exposed_ports(5000) as registry:
        wait_for_logs(registry, "listening on")
        yield registry

@pytest.fixture
def e2e_env(e2e_registry_config: E2ERegistryConfig, monkeypatch) -> dict[str, str]:
    """Apply e2e environment variables for a test."""
    env_vars = e2e_registry_config.as_env_vars()
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars
```

This eliminates the need for docker-compose and CI workflow infrastructure setup - tests are fully hermetic.

### PostgreSQL Tests

- `bazel-test.yml`: Uses GitHub service container (`PGHOST=localhost`, `PGPORT=5432`)
- Props tests may use testcontainers in the future

### Workflow Dispatch

Current approach in `ci.yml`:

1. `compute-targets` job computes affected targets and sets boolean flags
2. Individual workflow files are called based on flags
3. Each E2E workflow has its own setup/teardown logic

**Improvements made:**

- Docker tests now share utilities via `//test_util`
- Props E2E tests use testcontainers for hermetic infrastructure
- Non-Docker tests no longer depend on Docker fixtures

**Remaining issues:**

1. **Inconsistent env vars**: Same tag (`requires_postgres`) maps to different ports
2. **No tag validation**: Nothing prevents tests from having tags without matching CI support

## Industry Patterns

### Option 1: Testcontainers (Per-Test Infrastructure)

Each test spins up its own infrastructure via [Testcontainers](https://www.docker.com/blog/revolutionize-your-ci-cd-pipeline-integrating-testcontainers-and-bazel/):

```python
# In test file
@pytest.fixture
async def postgres():
    async with PostgresContainer("postgres:16") as pg:
        yield pg.connection_string()
```

**Pros**:

- Hermetic: each test is isolated
- No coordination needed between CI and tests
- Works locally and in CI identically
- Bazel cache works correctly (inputs = Docker image tag)

**Cons**:

- Slower: container startup per test (or test suite)
- Requires Docker-in-Docker or Testcontainers Cloud
- Not "Bazel-pure": containers started outside Bazel's control
- JVM-centric (Python support exists but less mature)

**Recommendation**: Good for truly isolated tests. Consider for new test suites.

### Option 2: GitHub Service Containers

Use GitHub Actions' built-in service containers:

```yaml
services:
  postgres:
    image: postgres:16
    ports: [5432:5432]
```

**Pros**:

- Simple, native to GitHub Actions
- Fast (containers run alongside job)
- Well-documented, widely used

**Cons**:

- Can't do complex orchestration (e.g., "start backend after schema init")
- Single fixed port (conflicts if tests expect different ports)
- Not reproducible locally without manual setup

**Recommendation**: Good for simple requirements. Already used for `bazel-test.yml`.

### Option 3: Docker Compose Pre-Setup

Run docker-compose before tests (current `props-e2e-test.yml` approach):

```yaml
- run: docker compose up -d postgres registry
- run: bazel test //props/...
- run: docker compose down
```

**Pros**:

- Full control over orchestration
- Reproducible locally (`docker compose up && bazel test`)
- Can handle complex dependencies (backend needs schema first)

**Cons**:

- Non-hermetic (compose runs outside Bazel)
- Harder to parallelize (shared state)
- More CI YAML maintenance

**Recommendation**: Good for complex multi-service tests. Keep for props E2E.

### Option 4: rules_itest (Bazel-Native Service Orchestration)

[rules_itest](https://github.com/dzbarsky/rules_itest) is a modern Bazel ruleset (available on [Bazel Central Registry](https://registry.bazel.build/modules/rules_itest)) for hermetic service provisioning:

```python
# MODULE.bazel
bazel_dep(name = "rules_itest", version = "0.0.41")
```

```python
# BUILD.bazel
load("@rules_itest//itest:itest.bzl", "itest_service", "itest_task", "service_test")

itest_service(
    name = "postgres",
    exe = "@rules_postgresql//:postgres",
    autoassign_port = True,
    health_check = "//infra:pg_healthcheck",
)

itest_task(
    name = "db_migrate",
    exe = "//scripts:migrate",
    deps = [":postgres"],
    env = {"DB_PORT": "$${@@//:postgres}"},
)

service_test(
    name = "integration_test",
    test = ":_test_impl",
    services = [":postgres", ":db_migrate"],
)
```

**Key features**:

- Automatic port assignment with `$${PORT}` substitution
- Health checks verified before test starts
- Service control HTTP API for dynamic start/stop during tests
- `ibazel` integration for hot-reload during development
- Port information exposed via `ASSIGNED_PORTS` env var (JSON)

**Pros**:

- Fully hermetic - services managed by Bazel
- Fresh service instances per test
- Works with remote execution
- Active development (v0.0.41 as of 2024)

**Cons**:

- Learning curve for new rule syntax
- Need to package services as Bazel targets
- Less mature than docker-compose for complex orchestration

### Option 5: rules_postgresql (Hermetic PostgreSQL)

[rules_postgresql](https://github.com/jacobshirley/rules_postgresql) downloads PostgreSQL binaries hermetically:

```python
# Downloads postgres binaries for Linux/macOS/Windows (x86_64/arm64)
postgresql_server_test(
    name = "db_test",
    srcs = ["test_db.py"],
    # Creates isolated cluster with separate data directory
)
```

**Pros**:

- Zero local setup - PostgreSQL downloaded by Bazel
- Isolated clusters per test
- Cross-platform (Linux, macOS, Windows)

**Cons**:

- Only `postgresql_server_test` currently supported
- Limited to PostgreSQL (no Redis, etc.)

**Recommendation**: Consider rules_itest for new hermetic tests, especially if remote execution becomes important. Keep docker-compose for existing complex E2E flows.

## Recommended Approach: Tag-Based Environment Dispatch

### Design

1. **Standardize tags → environment contracts**
2. **Single source of truth for tag → env mapping**
3. **Validate consistency at CI time**

### Tag Contracts

Define explicit contracts for each infrastructure tag:

```python
# tools/ci/test_environments.py

TAG_CONTRACTS = {
    "requires_postgres": {
        "env_vars": {
            "PGHOST": "127.0.0.1",
            "PGPORT": "5432",
            "PGUSER": "postgres",
            "PGPASSWORD": "postgres",
            "PGDATABASE": "test",
        },
        "setup": "service_container",  # or "compose"
    },
    "requires_docker": {
        "env_vars": {},  # Just needs daemon
        "setup": "native",  # GitHub runners have Docker
    },
    "requires_registry": {
        "env_vars": {
            "PROPS_REGISTRY_PROXY_HOST": "127.0.0.1",
            "PROPS_REGISTRY_PROXY_PORT": "8000",
        },
        "setup": "compose",
    },
    "e2e": {
        "env_vars": {},
        "setup": "dedicated_workflow",  # Complex, needs own workflow
    },
}
```

### Standardize Port Allocation

**Problem**: `props/` tests use port 5433, `gatelet/` uses 5432.

**Solution**: Pick one port and migrate:

| Service        | Standard Port                               |
| -------------- | ------------------------------------------- |
| PostgreSQL     | 5432                                        |
| Registry proxy | 8000                                        |
| Backend        | 8000 (same as registry via unified backend) |

**Migration**:

1. Update `props/` BUILD.bazel files to use port 5432
2. Update `props/compose.yaml` to expose 5432
3. All `_POSTGRES_TEST_ENV` definitions use same values

### CI Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      ci.yml                                  │
├─────────────────────────────────────────────────────────────┤
│  compute-targets job:                                        │
│    1. Compute affected Bazel targets                         │
│    2. Query tags on affected test targets                    │
│    3. Group tests by required environment                    │
│    4. Output: { env_name: [targets] }                        │
└─────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  basic-tests    │  │  docker-tests   │  │  e2e-tests      │
│  (no infra)     │  │  (Docker only)  │  │  (full stack)   │
├─────────────────┤  ├─────────────────┤  ├─────────────────┤
│ service:        │  │ setup:          │  │ setup:          │
│   postgres:5432 │  │   (native)      │  │   compose up    │
│                 │  │                 │  │   build images  │
│ bazel test      │  │ bazel test      │  │   init schema   │
│   --test_tag_   │  │   --test_tag_   │  │   start backend │
│   filters=-e2e  │  │   filters=      │  │                 │
│   -requires_    │  │   requires_     │  │ bazel test      │
│   docker        │  │   docker        │  │   --test_tag_   │
│                 │  │   -e2e          │  │   filters=e2e   │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

### Implementation Plan

#### Phase 1: Standardize Ports (Low Risk)

1. Create `tools/ci/test_environments.py` with tag contracts
2. Update `props/` tests to use standard port 5432
3. Update `props/compose.yaml` port mapping
4. Verify all tests pass locally and in CI

#### Phase 2: Tag Validation

Add Bazel query to detect tag inconsistencies:

```bash
# Find all tests with requires_postgres
bazel query 'attr(tags, "requires_postgres", //...)'

# Verify they all use consistent env expectations
# (Check BUILD.bazel files for env = {...} declarations)
```

Add CI check that fails if tests use tags without matching env declarations.

#### Phase 3: Unified Compute-Targets

Extend `bazel_diff.py` to:

1. Query tags on affected test targets
2. Group by environment requirements
3. Output structured JSON for matrix dispatch

```python
# Output example
{
    "basic": ["//adgn:test_foo", "//mcp_infra:test_bar"],
    "postgres": ["//props/db:test_sync", "//gatelet:test_db"],
    "docker": ["//agent_server:test_exec"],
    "e2e": ["//props/critic:test_e2e"]
}
```

#### Phase 4: Matrix Dispatch

Use [GitHub Actions dynamic matrix](https://devopsdirective.com/posts/2025/08/advanced-github-actions-matrix/):

```yaml
jobs:
  compute:
    outputs:
      matrix: ${{ steps.compute.outputs.matrix }}
    steps:
      - run: python tools/ci/compute_test_matrix.py
        id: compute

  test:
    needs: compute
    strategy:
      matrix: ${{ fromJson(needs.compute.outputs.matrix) }}
    uses: ./.github/workflows/test-env-${{ matrix.env }}.yml
    with:
      targets: ${{ matrix.targets }}
```

### Consistency Validation

Add pre-commit or CI check to ensure tag contracts are honored:

```python
# tools/ci/validate_test_tags.py

def validate():
    """Ensure tests with infrastructure tags have correct env declarations."""
    for target in bazel_query('kind("py_test", //...)'):
        tags = get_tags(target)
        env = get_env(target)

        for tag in tags:
            if tag in TAG_CONTRACTS:
                expected = TAG_CONTRACTS[tag]["env_vars"]
                for key, value in expected.items():
                    if env.get(key) != value:
                        fail(f"{target}: tag {tag} requires {key}={value}")
```

## Open Questions

1. **Should we migrate to Testcontainers for new tests?**
   - Pro: More hermetic, easier local dev
   - Con: Learning curve, slower tests

2. **How to handle tests needing multiple infra (postgres + docker)?**
   - Option A: Composite tags (`requires_postgres_and_docker`)
   - Option B: Multiple tags, environment provides superset

3. **Remote execution compatibility?**
   - Current compose-based tests won't work with remote execution
   - Accept this limitation, or invest in Bazel-native services?

## References

### Bazel Service Testing

- [rules_itest](https://github.com/dzbarsky/rules_itest) - Modern Bazel rules for hermetic service provisioning (databases, servers, mocks)
- [rules_itest on Bazel Central Registry](https://registry.bazel.build/modules/rules_itest) - Official BCR entry
- [rules_itest API docs](https://github.com/dzbarsky/rules_itest/blob/master/docs/itest.md) - itest_service, service_test, port assignment
- [rules_postgresql](https://github.com/jacobshirley/rules_postgresql) - Hermetic PostgreSQL binaries for Bazel

### Container Testing

- [Testcontainers + Bazel integration](https://www.docker.com/blog/revolutionize-your-ci-cd-pipeline-integrating-testcontainers-and-bazel/) - Docker's guide to Testcontainers with Bazel
- [Migrating Docker Compose Tests to Bazel](https://blog.aspect.build/integration-testing-oci) - Aspect Build's comparison of approaches
- [rules_oci](https://github.com/bazel-contrib/rules_oci) - Official OCI container rules for Bazel

### CI/GitHub Actions

- [GitHub Actions matrix strategy](https://devopsdirective.com/posts/2025/08/advanced-github-actions-matrix/) - Advanced dynamic matrix patterns
- [GitHub Actions dynamic matrix with fromJSON](https://docs.github.com/en/actions/learn-github-actions/expressions#fromjson) - Official docs
