# Sandbox frontend

The Sandbox list (`list.tsx`) and detail pages live here, along with creation, lifecycle,
runtime Session diagnostics, console, status, policy, egress, Sandbox API calls, and live Sandbox views and their tests. The Sandbox Thread list and creation form live in `../threads/`. Shared HTTP transport and the Sandbox status model stay in the parent frontend package; Thread status and its indicator are owned by `../threads/`. Shared binding presentation and readiness logic live in [`../bindings/`](../bindings/README.md), used here for sandbox policy bindings and by Connections for caller grants. Binding API types remain owned by this package's client.

The Bazel targets live in `BUILD.bazel`. The local icon declarations are a
package-local copy because the TypeScript Bazel rule requires target sources to
come from their own package; keep it in sync with the parent declaration file.
