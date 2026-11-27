# Retained k3s infrastructure

This package owns the node that a system-test deployment runs on: k3s startup and shutdown,
bootstrap, networking, pinned release loading, PKI controllers, and baseline compatibility and
cleanliness checks. `baseline.py` manages the retained node; `defs.bzl` selects its artifacts and
execution properties for each suite.

Application charts, databases, certificates issued for a particular run, the pytest deployment
adapter, and workload cleanup belong to [`deployment/`](../deployment/README.md). Optional
KubeVirt controllers, VM volumes and guest evidence belong to [`vm/`](../vm/README.md).

Retention requires the executor-injected `AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1` permission;
`AGENTPLANE_K3S_RETAIN_RUNNING=0` keeps only the image store instead (`retain_running_baseline`). A
missing, unhealthy, dirty or incompatible baseline rebuilds cold. Infrastructure fingerprints
include their actual helpers and pinned releases/images; per-action workload changes do not enter
that compatibility boundary. Snapshot publication uses BuildBuddy's default save policy.

`AGENTPLANE_K3S_DATA_DIR` can select an absolute data directory and participates
in compatibility checks. Docker recycling experiments use a container-private
path so other actions cannot reset a paused runner's live state through the
worker's shared image-cache mount. `discard_executor_runner()` requests removal
of the current authorized BuildBuddy runner after output upload.

### Native image import profiles

Native containerd CPU and runtime profiles around guest image imports are opt-in. The strict
`AGENTPLANE_PROFILE_IMAGE_IMPORTS` test environment variable defaults to `0`; set it to exactly
`1` to collect them. Values other than `0` and `1` fail stack setup. Ordinary image import spans,
setup tracing, JUnit durations, watchdogs and failure diagnostics run with either setting.

On a cold baseline rebuild, the CPU and runtime captures each request 420 seconds and start before
the first baseline image import. On a warm baseline, the legacy guest image captures request 15
seconds for CPU and 5 seconds for runtime. An opt-in profile can therefore extend a normal short
fixture teardown until its requested window ends. If fixture failure closes it early, the client is
cancelled and sidecar metadata records `status: cancelled` and `complete: false`; the partial bytes
must not be treated as a completed profile.

The profile's native duration describes the sampler request. `capture_started_unix_ns` is the test
host wall-clock time immediately before launching `k3s ctr pprof`; the image-import phase timestamps
use the same host wall clock. The server does not acknowledge its sampling start, so phase offsets
measure overlap with the client request window rather than prove exact sample coverage.
`process_exit_observed_after_seconds` is when the test observes process exit and can lag sampling
completion while the scenario runs. `context_exit_wait_seconds` measures only the wait for that
process at context exit; it excludes output drain and metadata writes.

For example, collect the native profiles on a VM suite with
`bbr test --test_env=AGENTPLANE_PROFILE_IMAGE_IMPORTS=1 //agentplane/system_tests/suites:test_vm`.

See the [deployment guide](../deployment/README.md) for the composed lifecycle, image handling,
diagnostics and suite behavior, and [persistence evidence](debug/retained_baseline/k3s_persistence.md) for the
original measurements.
