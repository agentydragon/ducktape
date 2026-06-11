# RBE Docker Test Transient Hangs

**Status**: Open — transient, not deterministic. No fix identified.

## Symptom

Docker-dependent tests (using testcontainers) intermittently hang on RBE workers.
The test starts normally, some tests pass, then a subsequent test hangs until the
Bazel timeout kills it. A different test hangs each time — not reproducible on the
same test.

## Observed Instances

| Date       | Commit     | Failing test                                   | Notes                                        |
| ---------- | ---------- | ---------------------------------------------- | -------------------------------------------- |
| 2026-04-08 | `869425d3` | `//props/db:test_tp_occurrence_credits`        | 6/10 passed, hung on test 7                  |
| 2026-04-08 | `a40a89bd` | `//props/specimens/ducktape/2025-11-22-01:...` | 0/1, hung immediately                        |
| 2026-04-08 | `869425d3` | `//devinfra/claude:test_integration`           | 1/3 passed, hung on test 2 (re-triggered CI) |

All three are Docker-dependent tests using Python testcontainers (PostgreSQL or
mitmproxy containers). The container starts successfully (evidenced by earlier tests
passing), then a later test hangs.

## What We Ruled Out

- **Not a code regression**: Different tests fail on different runs of the same
  commit. The re-triggered CI on `869425d3` failed on a completely different test
  than the original run.
- **Not the `crane.py` symlink bug** (`8d91c1305`): That bug caused `docker load`
  to error (non-zero exit), not hang. And the container starts successfully (first
  tests pass).
- **Not the RBE image change** (`14a2caf7f`): The RBE image change removed qemu/fuse
  packages, not Docker-related packages. It did bust the Bazel cache, forcing
  Docker tests to re-run (exposing the latent flake).
- **Not a database deadlock**: The `test_tp_occurrence_credits` test queries a
  PostgreSQL view chain, but the hang occurs across unrelated tests (mitmproxy
  proxy tests too).

## Hypotheses

1. **Docker daemon on RBE worker becomes intermittently unresponsive**: The
   testcontainer library uses Docker API calls (exec, logs, health checks). If
   the daemon stalls mid-session, subsequent Docker operations block indefinitely.
   The container is already running (earlier tests passed), but new API calls hang.

2. **Resource exhaustion on RBE worker**: Multiple test targets run concurrently
   on the same worker. If one test's Docker containers consume too much memory/CPU,
   the Docker daemon may become sluggish for other tests.

3. **Testcontainers Ryuk reaper interference**: Testcontainers starts a "Ryuk"
   sidecar container for cleanup. If Ryuk crashes or blocks, subsequent container
   operations may hang.

## How to Investigate Further

```bash
# Check if the test is still flaky
bbapi target history --failures-only --label //props/db:test_tp_occurrence_credits

# Re-trigger CI on a specific commit
bbapi workflow run --action CI --commit <sha> --branch devel --async

# Get test log from a failing invocation
bbapi target log <invocation-id> test_tp_occurrence_credits

# Stream test output in real-time (on RBE)
bb remote test //props/db:test_tp_occurrence_credits \
  --config=rbe --test_output=streamed --test_arg=-s \
  --nocache_test_results --run_from_commit=origin/devel
```

## Potential Mitigations

- Add timeouts to testcontainer health checks (currently may wait indefinitely)
- Add Docker daemon health check before test execution
- Reduce test parallelism for Docker-dependent tests (`--local_test_jobs=1`?)
- Log Docker daemon state (`docker info`, `docker ps`) in test setup for postmortem
