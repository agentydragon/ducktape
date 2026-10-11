# ADR: Action Service Sandboxes through the Sandbox Service

- **Status:** Proposed (draft for the operator's review)
- **Date:** 2026-10-11
- **Scope:** the `sandbox__*` Actions ([`action_service/sandbox/`](../action_service/sandbox/README.md))
  and the [Sandbox Service API](../sandbox_service/API.md)

## Context

The Action Service's `sandbox__*` Actions create, exec into and delete agent-sandbox `Sandbox`
objects themselves: their own inventory, labels and template allowlist, with Sandbox CRUD and
namespace-wide `pods/exec` on the Action Service's ServiceAccount. That is a second provisioner
beside the Sandbox Service, against the target of one provisioner.

The two kinds of box share the CRD, templates, the namespace quota and the egress sidecar. They
differ in everything the Sandbox Service's `CreateSandbox` does around the CRD:

| | LLM Sandbox (Sandbox Service today) | Action Sandbox (Action Service today) |
| --- | --- | --- |
| Runs as | a ServiceAccount minted per Sandbox, owned by it | the caller's own static ServiceAccount |
| Egress, Action policies, Kubernetes grants | granted per Sandbox at create | the caller's standing bindings; none created |
| Runner | `runner` container wired to the Runner Gateway and History ingress | none dialled; commands arrive by `pods/exec` |
| Teardown | seal every Session, then delete | delete |
| Owner key | requested name | (caller, name); names are per caller |
| Lifetime | operator / app | caller's `dispose`; `TODO(sandbox-lifetime)` |

## Decision

### 1. One `CreateSandbox`, two workload shapes

`CreateSandboxRequest` carries a `oneof workload`:

```proto
message CreateSandboxRequest {
  string name = 1;
  string template = 2;
  oneof workload {
    AgentWorkload agent = 3;        // today's egress_policies, action_policy_sets,
                                     // kubernetes_grants, session_defaults, bootstrap
    CallerWorkload caller = 4;
  }
}
message CallerWorkload {
  ServiceAccount run_as = 1;        // the Pod's serviceAccountName; no grants, no runner flags
}
```

`Sandbox` reports which shape it is (`oneof workload` with the same two messages), and
`ListSandboxes` takes a shape filter, so the app's fleet view and the controller's runner paths
never meet a box with no runner. A field set that admits nonsense (a caller box with
`session_defaults`, an agent box with `run_as`) is not representable.

Rejected: **a second RPC (`CreateCallerSandbox`)**. Suspend, resume, delete, get and list are the
same operations on the same CR; a second create would bring a second set of those or a shape check
in each. Rejected: **options on the flat request** (`run_as` beside `egress_policies`), which is the
flag-plus-optional combination STYLE.md rules out.

### 2. The caller's identity carries over as an asserted `run_as`, gated twice

Today the Sandbox Service never forwards an identity: a destination's owner is a selector, and any
allowlisted service caller may operate on any owner. A caller box needs one: the Action Service has
authenticated the human's OAuth grant to a static ServiceAccount (`ExecutionRequest.caller`) and the
box must run as exactly that account.

- **Who may assert it:** a separate `run_as_callers` allowlist in the Sandbox Service settings
  (the Action Service's account only). The app, which is allowlisted for agent boxes, cannot create
  a caller box.
- **Who may be run as:** an account carrying `agentplane.allegedly.works/use-action-service`, the
  label that already admits it to the Action Service, in the Sandbox namespace. The Sandbox Service
  reads the ServiceAccount before stamping. So a compromised Action Service can stamp boxes only as
  accounts that could call it anyway, which is the authority its own Sandbox CRUD gives it today,
  minus every other account in the namespace.
- **Ownership:** caller boxes are keyed by (`run_as`, name). Every RPC naming a caller box must
  name the same `run_as`, and the Sandbox Service checks it against the stored label, as the Action
  inventory's `ForeignSandboxError` does now. Agent boxes keep their UID-pinned destinations.

Rejected: **the Action Service forwards the caller's token**: there is none at dispatch (submission
and dispatch are separated by a Decision and possibly a restart; see the Action sandbox README).

### 3. The Sandbox Service runs `exec`

New RPC `ExecSandbox(ExecSandboxRequest) returns (ExecSandboxResponse)` with the script, `cwd`,
timeout and output ceiling, on caller boxes only; the bounded result (`util/exec` models) comes
back whole. The Action Service keeps the agent-facing schema, the per-deployment ceilings and the
lease renewal around the call.

With this the Action Service holds no RBAC on `Sandbox`, `SandboxTemplate` or Pods at all. The
Sandbox Service already holds Sandbox CRUD and ServiceAccount creation in the namespace, so
`pods/exec` there concentrates Pod authority in the component that already has the larger share.

Rejected: **exec stays in the Action Service.** Kubernetes RBAC cannot scope `pods/exec` by label,
so it keeps namespace-wide exec into every LLM Sandbox as well. Rejected: **a runner in caller
boxes, commands through the Runner Gateway.** It would give caller boxes Sessions, history and a
seal they have no use for, and the gateway's command path admits harness commands, not scripts.

### 4. Templates

The offered-template allowlist and descriptions move with the provisioning: `ListTemplates` takes
the shape and returns each template's description annotation, and the Sandbox Service refuses a
template not offered for that shape. A template offered for caller boxes must keep
`automountServiceAccountToken: false` (the shared-account invariant in the Action sandbox README);
the Sandbox Service refuses one that does not, at startup.

**Open question:** staging offers the app's `runner` and `runner-ducktape` templates to caller
boxes, today stamped with no runner flags. Under this design the Sandbox Service does not inject
runner flags into a caller box, so those offers keep working as they do; drop them instead if
nobody uses them.

### 5. Lifetime

Caller boxes delete directly (no seal, no holds). `SANDBOX_IDLE_EXPIRY` stays separate; the
`TODO(sandbox-lifetime)` moves into the Sandbox Service with the stamping code.

## Rollout

Clean cutover, like the runner dial-out's: the Action Service's own boxes carry
`sandbox-actions.agentplane.allegedly.works/*` labels the Sandbox Service would not adopt. The
operator disposes the existing ones (or the provisioning PR lists them first), then the release
that switches the Action Service also removes its Sandbox and `pods/exec` RBAC.

## Consequences

- One provisioner; the Action Service becomes a Sandbox Service client like the app.
- The Sandbox Service gains a forwarded identity, bounded by the two allowlists above, and
  `pods/exec`.
- `ACTION_SANDBOX_PROVISIONING` implements this; `SANDBOX_IDLE_EXPIRY` covers both shapes.
