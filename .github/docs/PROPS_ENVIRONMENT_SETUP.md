# Props Environment Setup for GitHub Copilot Agents

This document provides instructions for GitHub Copilot agents on how to set up and operate the props testing environment.

## Overview

The props ecosystem is a code evaluation system that requires Docker infrastructure (PostgreSQL, Docker registry, and proxy services) to run E2E tests. This guide documents the complete setup process.

## Prerequisites

- Docker installed and running
- Bazel/Bazelisk available
- Sufficient disk space (images ~500MB + containers)
- PostgreSQL client tools (pg_isready)

## Environment Setup Steps

### 1. Generate Environment Variables

```bash
export PGPASSWORD=$(openssl rand -base64 24)
export OPENAI_API_KEY=test-key-not-used  # Dummy key for tests
```

### 2. Build Docker Images

Build the registry proxy and LLM proxy images:

```bash
cd /home/runner/work/ducktape/ducktape
bazel run //props/registry_proxy:load
bazel run //props/llm_proxy:load
```

**Expected time:** 60-90 seconds total
**Expected output:** "Loaded image: props-registry-proxy:latest" and "Loaded image: props-llm-proxy:latest"

### 3. Pull Infrastructure Images

```bash
docker pull postgres:16
docker pull registry:2
```

**Expected time:** 30-60 seconds (cached after first pull)

### 4. Start Infrastructure Services

```bash
cd props
docker compose up -d
```

**Services started:**
- PostgreSQL (port 5433)
- Docker Registry (port 5050)
- Registry Proxy (port 5051)
- LLM Proxy (port 5052)

**Networks created:**
- props-internal (for service-to-service communication)
- props-agents (for agent containers to reach services)

### 5. Wait for Services to Be Ready

```bash
# Wait for PostgreSQL
until pg_isready -h 127.0.0.1 -p 5433 -U postgres 2>/dev/null; do sleep 1; done

# Wait for registry
until curl -sf http://127.0.0.1:5050/v2/ 2>/dev/null; do sleep 1; done

# Wait for registry proxy (returns 200 or 401)
for i in {1..30}; do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5051/v2/ 2>/dev/null || echo "000")
  if [[ "$HTTP" =~ ^(200|401)$ ]]; then
    echo "Registry proxy ready"
    break
  fi
  sleep 1
done
```

### 6. Initialize Database

Set environment variables and create database schema:

```bash
export PGHOST=127.0.0.1
export PGPORT=5433
export PGUSER=postgres
export PGDATABASE=eval_results
export ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/testing/fixtures/testdata/specimens"

bazel run //props/cli:cli -- db recreate -y
```

**Expected output:**
- Creates database schema
- Syncs 4 test snapshots
- Syncs snapshot files and issues
- **Expected time:** 60-90 seconds

### 7. Push Agent Images to Registry

```bash
bazel run //props/critic:push
bazel run //props/grader:push
bazel run //props/critic_dev/improve:push
bazel run //props/critic_dev/optimize:push
```

**Expected time:** 10-20 seconds (after initial build)
**Expected output:** "localhost:5050/[agent-name]:latest: digest: sha256:..."

## Running Props E2E Tests

### Test Environment Variables

```bash
export PGHOST=127.0.0.1
export PGPORT=5433
export PGUSER=postgres
export PGDATABASE=eval_results
export AGENT_PGHOST=127.0.0.1
export PROPS_REGISTRY_PROXY_HOST=127.0.0.1
export PROPS_REGISTRY_PROXY_PORT=5051
export PROPS_DOCKER_NETWORK=props-agents
export PROPS_E2E_HOST_HOSTNAME=172.17.0.1
```

### Run E2E Tests

```bash
bazel test --keep_going \
  --test_output=errors \
  //props/critic:test_e2e \
  //props/critic_dev/improve:test_e2e \
  //props/critic_dev/optimize:test_e2e \
  //props/core:test_agent_pkg_e2e
```

## Known Issues

### Database Foreign Key Violations (Current Issue)

**Symptom:** Tests fail with `IntegrityError: insert or update on table "occurrence_ranges" violates foreign key constraint "fk_occurrence_range_snapshot_file"`

**Root Cause:** Test fixture data sync issue - occurrence_ranges references snapshot_files that weren't synced properly during test setup.

**Status:** Known issue in test fixtures, not related to infrastructure setup. Tests set up their own database state but the fixture sync logic has a race condition or missing dependency.

**Impact:** All 4 E2E test targets currently fail with this error during test setup phase.

## Cleanup

```bash
cd props
docker compose down         # Stop all services
docker compose down -v      # Stop and remove volumes (full cleanup)
```

## Troubleshooting

### Service Not Starting

Check logs:
```bash
docker logs props-postgres
docker logs props-registry-proxy
docker logs props-llm-proxy
```

### Port Already in Use

Check if ports 5433, 5050, 5051, 5052 are available:
```bash
netstat -tuln | grep -E "5433|5050|5051|5052"
```

### Database Connection Issues

Verify PostgreSQL is accessible:
```bash
PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -p 5433 -U postgres -d eval_results -c "SELECT 1"
```

### Registry Issues

Test registry connectivity:
```bash
curl http://127.0.0.1:5050/v2/
curl http://127.0.0.1:5051/v2/
```

## Performance Notes

- **Full setup time:** 3-5 minutes (first time with image builds)
- **Subsequent setups:** 1-2 minutes (images cached)
- **Test execution:** Varies by test, E2E tests take 20-40 seconds each
- **Disk usage:** ~1GB (images + volumes)

## Future Improvements Needed

1. Fix test fixture data sync to resolve foreign key violations
2. Register pytest marks (integration, requires_postgres, requires_docker, timeout, slow)
3. Investigate why occurrence_ranges sync happens before snapshot_files sync
4. Consider adding retry logic or proper dependency ordering in fixture sync

## Last Updated

2026-01-22 - Initial documentation based on props E2E setup and test run
