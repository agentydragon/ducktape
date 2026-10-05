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
