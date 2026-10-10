@README.md

# Agentplane service boundaries

The integration app is a user-facing composition layer, not a backend dependency.
See [the dependency rule](docs/service_boundaries.md) and [Sandbox Service plan](plans/sandbox_service.md).

- Do not add backend-service imports of `agentplane.app`, calls to its APIs, reads of its private
  tables, or requirements for its process, browser session, or app-issued identity to be available.
  Backend functionality needed by another service must be extracted into its owning service or a
  neutral shared package, not exposed through a temporary app-owned backend API.
- The app calls independent services; those services do not call back into the app to operate.
  This includes v1, startup, background recovery, provisioning, and backend-required prompt/context
  construction. Operator approval still belongs to its backend authority; the app presents it.
- Notification v1 depends on the minimum Sandbox Service extraction, not the integration app.
  The Sandbox Service owns sandbox lifecycle and access to runner sessions; notifications owns
  subscriptions/inboxes, and runners own native execution and canonical command/Event evidence.
- Current backend responsibilities inside the app are extraction work, not a precedent for new
  dependencies. Distinguish planned boundaries from what is already implemented.
- Acceptance for an extracted backend path must exercise it with the integration app unavailable.
  Do not add another command queue, event authority, or credential issuer as an incidental refactor.

## Deployed state is disposable

Agentplane has no production tier, only staging and testing, so its deployed state is disposable.
This holds for agentplane only: another component's deployed state is not disposable unless its
own `AGENTS.md` says so. A schema, CRD or wire change does not have to keep existing rows, custom
resources or messages readable: change the shape, and delete and recreate whatever no longer
parses. Do not write a migration, a tolerant reader, or a compatibility field to carry old data
forward, and do not stage a rollout to avoid a window where the two disagree. The schema change
itself is still a new migration, and the migrate step fails the rollout when the schema differs
from the models. The limits in the root [`AGENTS.md`](../AGENTS.md) § Refactoring
(person-authored data, roll-safety) still bind.

## Preserve staging data during the Sandbox Service extraction

For this extraction, data-preserving migration is the default despite the disposable-state rule
above. Inventory existing state, retain identities/history and runner
storage where feasible, and validate backup/restore and the cutover before changing staging.
Do not drop/recreate staging databases, sandboxes, volumes, or session state as a shortcut. If
preservation cannot be achieved, explain the exact loss/disruption and obtain operator approval
before proceeding. See the [migration plan](plans/sandbox_service.md#staging-data-preservation).

## Task DAG maintenance

`plans/task_dag.md` is a dispatch map for unfinished work, not a catalog of every possible
improvement or an acceptance-history ledger. `plans/task_freezer.md` holds deliberately deferred
ideas with a concrete reason to revisit them. Component plans own detailed designs; the DAG owns
status and dependencies. Keep their links and decisions consistent when changing either.

- Give each node one independently finishable outcome, its state, prerequisites and proportional
  exit evidence. Split larger plans at meaningful design, implementation and rollout boundaries;
  use a capstone only when downstream work genuinely needs the combined result. Do not create a
  node for every test or enumerate speculative implementation phases before a design is chosen.
- Make operator decisions explicit nodes: identify the alternatives, recommendation and question
  requiring review. Downstream implementation waits for that decision; drafting alternatives can
  proceed in parallel. Do not silently choose transport, authority or RBAC while rewriting plans.
- Draw real prerequisite edges, including shared foundations and migration sequencing holds.
  Label technical dependencies separately from operational holds and conditional branches. Edges
  constrain starting mutating work where stated, not merely its final acceptance. No priority
  inference from graph position, and no blanket permission to start a blocked implementation.
- Record in-flight work and who/source reported it, with a timestamp and scope. A partial backfill,
  merged implementation or successful unit test is not a completed rollout. Do not invent current
  state from an old log. Remove finished nodes; keep evidence in the owning component docs.
- Keep temporary rollout holds, migration sequencing and task-specific exceptions in the task DAG
  and owning component plans, not in `AGENTS.md`. This file contains durable contribution rules.
- Acceptance must address the changed contract and credible risk. Preserve security-denial,
  revocation, isolation, data-preservation and ordinary retry/reconnect/concurrency tests. Prefer
  deterministic automated coverage; require live checks only for an identified deployment-specific
  uncertainty, with a bounded scenario and stopping condition. Do not require seeing a provider
  outage in production, repeat operator-accepted work, or add compound-disaster drills by default.
- Missing live evidence alone is not unfinished work when suitable automated evidence exists.
  Remove redundant/waived gates; do not move them into the freezer as mandatory future chores.
  Unlikely hardening without a concrete need is omitted, or frozen with an incident/measurement/
  product-decision trigger. Frozen items neither block the main DAG nor imply a dispatch priority.
- Check graph nodes/edges, cycles, linked anchors and duplicated outcomes before publishing a DAG
  refactor. Explain material gate removals; do not bury unresolved security or migration risks as
  “over-conservative” merely to shorten the list.
