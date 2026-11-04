# Retained k3s infrastructure

This package owns the node that a system-test deployment runs on: k3s startup and shutdown,
bootstrap, networking, pinned release loading, PKI controllers, and baseline compatibility and
cleanliness checks. `baseline.py` manages the retained node; `defs.bzl` selects its artifacts and
execution properties for each suite.

Application charts, databases, certificates issued for a particular run, the pytest deployment
adapter, and workload cleanup belong to [`deployment/`](../deployment/README.md). Optional
KubeVirt controllers, VM volumes and guest evidence belong to [`vm/`](../vm/README.md).

Retention requires the executor-injected `AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1` permission. A
missing, unhealthy, dirty or incompatible baseline rebuilds cold. Infrastructure fingerprints
include their actual helpers and pinned releases/images; per-action workload changes do not enter
that compatibility boundary. Snapshot publication uses BuildBuddy's default save policy.

See the [deployment guide](../deployment/README.md) for the composed lifecycle, image handling,
diagnostics and suite behavior, and [persistence evidence](../../debug/k3s_persistence.md) for the
original measurements.
