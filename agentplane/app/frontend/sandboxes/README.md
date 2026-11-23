# Sandbox frontend

The Sandbox list (`list.tsx`) and detail pages live here, along with creation, lifecycle,
runtime Session diagnostics, console, status, policy, Sandbox API calls, and live Sandbox views and their tests. The Egress decision log and credential metadata watch live in [`../egress/`](../egress/README.md). The Sandbox Thread list and creation form live in `../sessions/`. Shared HTTP transport lives in `../transport/`; Thread status and its indicator are owned by `../sessions/`. Shared binding presentation and readiness logic live in [`../policy/`](../policy/README.md), used here for sandbox policy bindings and by Connections for caller grants. Binding and PolicySet API types are generated from the PolicyService protobufs.

The Bazel targets live in `BUILD.bazel`. Shared icon imports and ambient Tabler declarations
are owned by `../ui/`.
