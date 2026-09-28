# Bazel Benchmark — 2026-03-24

## Environments

- **GitHub-hosted**: `ubuntu-latest` (4 vCPU, 16GB RAM), ephemeral
- **Self-hosted**: wyrm2 (Proxmox), StatefulSet in `arc-runners` namespace,
  `ghcr.io/actions/actions-runner:latest`, 8GB RAM / 2-8 CPU, 50Gi `local-path`
  PVC for `~/.cache`, non-ephemeral (persistent Bazel JVM across jobs)
- **RBE**: BuildBuddy remote execution + remote cache for all environments

## Results

| Step                   | GitHub-hosted | Self-hosted (cold) | Self-hosted (warm) |
| ---------------------- | ------------- | ------------------ | ------------------ |
| `bazel build -k //...` | 448s (7m 28s) | 325s (5m 25s)      | **44s**            |
| `bazel test -k //...`  | 133s (2m 13s) | 94s (1m 34s)       | **93s**            |
| **Total job**          | ~10m          | 9m 8s              | **4m 7s**          |

Build step: **10x faster** warm vs GitHub-hosted.

## Runs

| Run                    | Environment   | Invocation                                                                            |
| ---------------------- | ------------- | ------------------------------------------------------------------------------------- |
| GitHub-hosted build    | ubuntu-latest | [a72847a1](https://app.buildbuddy.io/invocation/a72847a1-95d6-4598-b3df-495d6a6e4d64) |
| GitHub-hosted test     | ubuntu-latest | [ef1667c0](https://app.buildbuddy.io/invocation/ef1667c0-aefa-4337-9d91-cb1616a9dad5) |
| Self-hosted cold build | wyrm2         | [7b851167](https://app.buildbuddy.io/invocation/7b851167-2c47-4f1c-ac26-a43a6d608293) |
| Self-hosted warm build | wyrm2         | (second run, same pod)                                                                |

GitHub Actions runs:

- GitHub-hosted: [23471637355](https://github.com/agentydragon/ducktape/actions/runs/23471637355)
- Self-hosted cold: [23474647048](https://github.com/agentydragon/ducktape/actions/runs/23474647048)
- Self-hosted warm: [23474938392](https://github.com/agentydragon/ducktape/actions/runs/23474938392)

## Analysis

### Build (10x improvement warm)

The 448s → 44s improvement is almost entirely **Skyframe analysis cache**. On
GitHub-hosted runners, 93% of build time (418s) was Skyframe analysis — loading
and analyzing all packages from scratch on every run. The persistent runner keeps
the Bazel server JVM alive between jobs, so the analysis cache stays in memory.

Cold self-hosted (325s) is faster than GitHub-hosted (448s) due to wyrm2 having
more CPU available.

### Test (30% improvement)

Test time improvement (133s → 93s) comes from faster analysis (same Skyframe
benefit) plus wyrm2's faster CPU for the small number of local actions. Most
test execution happens on BuildBuddy RBE workers, so the runner hardware matters
less for tests.

Warm vs cold test time is similar (~93-94s) because the test step reuses the
already-warm JVM from the build step in the same job.

## GitHub-hosted detailed breakdown

### Build (7m 28s)

**Analysis phase dominated: 418s out of 448s wall time (93%).**

Cold runner = no warm Skyframe cache. Bazel loaded 6,341 packages, configured
247,846 targets, and evaluated 307K+ Skyframe nodes from scratch.

| Category                       | Count  | Notes                                      |
| ------------------------------ | ------ | ------------------------------------------ |
| Total actions executed         | 31,624 |                                            |
| Internal (local, fast)         | 17,941 | Symlinks, manifests, file writes           |
| Remote cache hits              | 13,486 | Pre-built artifacts served from BuildBuddy |
| Remote actual executions (RBE) | 197    | Real compilation/lint work                 |
| Local action cache hits        | 7      | Effectively cold                           |

### Slowest individual RBE actions (top 10)

| Duration | Action                                                  |
| -------- | ------------------------------------------------------- |
| 21.6s    | `Rustc` — `crates/...` (largest crate)                  |
| 18.2s    | `mypy` — `tana/export/export_node_subset`               |
| 17.3s    | `mypy` — `devinfra/claude/hook_daemon` (test variant)   |
| 16.5s    | `mypy` — `devinfra/claude/hook_daemon/session_start`    |
| 16.3s    | `mypy` — `devinfra/claude/hook_daemon` (test variant 2) |
| 15.3s    | `Rustc` — second largest crate                          |
| 15.1s    | `mypy` — `devinfra/claude/statusline` (test variant)    |
| 14.6s    | `mypy` — `devinfra/claude/hook_daemon/main_lib`         |
| 14.2s    | `mypy` — `sysrw/cli`                                    |
| 13.5s    | `mypy` — `tana/export/tana_issues_to_tanapaste`         |

### Test (2m 13s)

Analysis was fast (42s) because Skyframe was already warm from the preceding
build. 423 tests, 99% cache hit rate (848/860).

## BuildBuddy Remote Bazel (`bb remote`) — Failed

Attempted `bb remote build -k //...` which runs the entire Bazel server on
BuildBuddy's infrastructure.

**Attempt 1** (default disk): Failed with **"No space left on device"** during
npm package extraction (`aw_webui` dependencies).
Run: [23475419756](https://github.com/agentydragon/ducktape/actions/runs/23475419756)

**Attempt 2** (`EstimatedFreeDiskBytes=50GB`): Disk issue resolved, but hit
the 1h timeout. Build ran ~55min before cancellation. Failures:

- `openssl-sys` Cargo build script failed (missing OpenSSL dev headers on runner)
- Visibility error in `validate_cluster` BUILD target
  Run: [23476180420](https://github.com/agentydragon/ducktape/actions/runs/23476180420)

Not viable for this repo — the runner environment lacks system dependencies
(OpenSSL headers), and the cold Bazel server offers no speed advantage over
GitHub-hosted runners (same ephemeral JVM problem). Would need a custom
`--container_image` with all system deps installed.

## Key insight

Bazel's Skyframe analysis cache is **in-memory only** — it lives in the JVM
heap and cannot be saved to disk or remote cache. The only way to preserve it
is to keep the Bazel server process alive between builds. This is why ephemeral
runners (GitHub-hosted or ARC `gha-runner-scale-set`) pay the full analysis cost
on every job, and why BuildBuddy uses Firecracker microVMs to snapshot/restore
the JVM.
