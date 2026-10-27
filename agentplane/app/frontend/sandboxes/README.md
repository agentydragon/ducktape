# Sandbox frontend

The Sandbox list (`list.tsx`) and detail pages live here, along with creation, lifecycle,
runtime Session diagnostics, console, status, policy, egress, Sandbox API calls, and live Sandbox views and their tests. The Sandbox Thread list and creation form live in `../threads/`. Shared HTTP transport and the Sandbox status model stay in the parent frontend package; Thread status and its indicator are owned by `../threads/`. `BindingsTable` stays in the parent package because Settings also uses it to display caller grants.

The Bazel targets live in `BUILD.bazel`. The local icon declarations are a
package-local copy because the TypeScript Bazel rule requires target sources to
come from their own package; keep it in sync with the parent declaration file.
