# Sandbox frontend

The Sandbox list (`list.tsx`) and detail pages live here, along with creation, lifecycle,
runtime Session diagnostics, console, status, policy, Sandbox API calls, and live Sandbox views and their tests. The Egress decision log and credential metadata watch live in [`../egress/`](../egress/README.md). The Sandbox Thread list and creation form live in `../sessions/`. Shared HTTP transport lives in `../transport/`, and the Sandbox status model stays in the parent frontend package; Thread status and its indicator are owned by `../sessions/`. Shared binding presentation and readiness logic live in [`../policy/`](../policy/README.md), used here for sandbox policy bindings and by Connections for caller grants. Binding and PolicySet API types are generated from the PolicyService protobufs.

The Bazel targets live in `BUILD.bazel`. The local icon declarations are a
package-local copy because the TypeScript Bazel rule requires target sources to
come from their own package; keep it in sync with `../ui/tabler_icons.d.ts`.
