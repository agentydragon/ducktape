@README.md

## Service boundaries

The integration app is a client of the backend services, never their dependency
([dependency rule](docs/service_boundaries.md)).

- Backend code must not import `agentplane.app`, call its APIs, read its private tables, or need its
  process, browser session or app-issued identity — including for v1, startup, background recovery,
  provisioning and backend-required prompt/context construction. Extract what another service needs
  into its owning service or a neutral shared package, never a temporary app-owned backend API.
  Operator approval stays with its backend authority; the app only presents it.
- Backend responsibilities still inside the app are extraction work, not precedent. Distinguish
  planned boundaries from implemented ones.
- Acceptance for an extracted backend path runs with the app unavailable. Never add a command queue,
  event authority or credential issuer as an incidental refactor.

## Settings

Service settings reject unknown keys, and the cdk8s manifests set them: add a setting in code before
a manifest sets it; remove one from the manifests first and the field after rollout.

## Database migrations

PostgreSQL services embed Goose migrations in their serving binary and run the shared
`postgres/go/migrate.Run` lifecycle during normal startup, before workers or request handling.
Use a stable per-database Goose history table and advisory lock. Use versioned SQL or registered
Go migrations; no unversioned startup converters, separate migrator images or migration init
containers. `--migrate-only` in the same binary is available for fixtures and explicit maintenance.
Deploy with rolling updates and a startup probe covering initialization; schema changes must
support overlapping old/new writers or have an explicitly reviewed writer cutover.
See the [migration contract](postgres/go/migrate/README.md) for timeouts, baseline adoption and
the Runner's SQLite journal.

## gRPC services

Servicers, clients, auth, errors and server wiring follow [one pattern](docs/grpc_services.md).
Service-to-service RPC uses native gRPC; browser HTTP and direct-client facades follow the
[HTTP/gRPC boundary policy](docs/service_boundaries.md#http-and-grpc-boundaries).

## System overview diagram

A PR that adds, removes, splits or renames a service or Deployment, or changes traffic between them
(caller/callee, protocol, an edge going live), updates the [README system overview](README.md#system-overview)
and [focused diagrams](docs/system_overview.md) in the same PR. Draw boundaries and traffic, not a
deployment or replica snapshot; dash runner-initiated platform routes.

## Deployed state is disposable

Agentplane has only a staging tier, so its deployed state is disposable (agentplane
only; another component's is not unless its own `AGENTS.md` says so). A schema, CRD or wire change
need not keep existing rows, custom resources or messages readable: change the shape and
delete/recreate whatever no longer parses. No migration, tolerant reader or compatibility field to
carry old data, and no staged rollout to avoid a disagreement window. The schema change itself is
still a new migration; the migrate step fails the rollout when schema and models differ. Root
`AGENTS.md` § Refactoring limits (person-authored data, roll-safety) still bind.

## Preserve staging data during service extractions

Exception to the above: when state ownership moves between services, preserve data by default —
inventory state, retain identities/history and runner storage where feasible, and validate
backup/restore and the cutover before changing staging. Never drop/recreate staging databases,
sandboxes, volumes or session state as a shortcut; unavoidable loss needs the exact loss stated and
operator approval first. Procedure: [staging data preservation](docs/staging_data_preservation.md).

## Changes that can break staging

Staging runs the operator's agents. A change that can take it down (a CRD, policy, egress, RBAC or wire cutover, a
Sandbox template, a database change) merges once the [system tests](system_tests/README.md) that exercise it pass,
and its PR states the rollback: what to revert, and what to check once Flux applies it. Agentplane's Flux, Cilium,
CNPG, External Secrets, cert-manager, Gateway API and Kyverno objects have no system test, so staging is the first
cluster they reach; the PR says so.

## Task DAG maintenance

Before editing `plans/task_dag.md` or `plans/task_freezer.md`, read
[task DAG maintenance](docs/task_dag_maintenance.md). Temporary rollout holds, migration sequencing
and task-specific exceptions go there or in component plans, never in this file.
