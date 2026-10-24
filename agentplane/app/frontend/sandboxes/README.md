# Sandbox frontend

The Sandbox list (`list.tsx`) and detail pages live here, along with creation, lifecycle,
session, console, status, policy, and egress views and their tests. The shared API
client and Sandbox status model stay in the parent frontend package; Thread status
and its indicator are owned by `../threads/`. `BindingsTable` stays in the parent
package because Settings also uses it to display caller grants.

The Bazel targets live in `BUILD.bazel`. Shared Tabler icon types come from
`//agentplane/app/frontend:tabler_icon_types`.
