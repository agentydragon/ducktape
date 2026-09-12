# Action policy bindings

Status: **decided 2026-09-12; the Action Service side has landed (CRDs, informer, ServiceAccount
callers, evaluation, evidence, the acceptance scenario); `SBPOLICY` is the step below and the deny
lists are deferred as `DENY_LISTS` in the task DAG.**
The single operator configures bounded auto-approval for both external MCP connections and
harnesses running in Threads inside Sandboxes. Both use the canonical Action Service and Decision
lifecycle. Identity/OAuth/Connection authority is implemented independently of policy
representation; this work does not block the human-approved Claude.ai connection.

## Model

Two namespaced Kubernetes resources in `agentplane.allegedly.works/v1alpha1`, watched by the
Action Service, plus ordinary ServiceAccounts as the external-caller principal. Each object is
owned either by Git through Flux or by a runtime writer (the integration app, or the operator with
kubectl); the split is per object, never per kind.

| Kind                  | Spec                                                                                               | Replaces                                       |
| --------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `ActionPolicySet`     | `autoApproveIf`, `autoDenyIf`, `autoDenyUnless`: lists of typed policies                           | `fixture_auto_allow`                           |
| `ActionPolicyBinding` | `subject` (`serviceAccount {namespace, name}` or `sandbox {name, uid}`), `policySets`, `expiresAt` | nothing; today external callers are human-only |

An **external caller is a Kubernetes ServiceAccount**, replacing the `identities:` map in the
Action Service settings. One principal then carries both native permissions through ordinary
RoleBindings and Action permissions through an `ActionPolicyBinding`; Sandbox callers are already
ServiceAccount-backed principals, so there is one identity vocabulary. A ServiceAccount is eligible
as a caller when it carries the label `agentplane.allegedly.works/action-caller: "true"`; the app's
consent UI lists eligible ServiceAccounts and the operator picks one, because MCP clients speak
OAuth and that "OAuth, then pick a principal" path stays. The Connection stores the ServiceAccount's
namespace and name in the Action Service's PostgreSQL. A missing or unlabeled ServiceAccount refuses
resolution at admission and at the dispatch claim, as a missing configured Identity does today;
removing the label or the object is the disable. A client that can hold a ServiceAccount token with
a dedicated audience may later authenticate by TokenReview, as Sandboxes do, without OAuth.

A **policy** is one typed evaluator: YAML carries `type` and parameters, Python owns the
semantics, as in the Haku console's `auto_approval_policies`. Adding a constraint the YAML cannot
express means adding a kind, never a DSL. Four kinds exist:

- `exact_actions`: `{group: [action, ...]}`, matches by name alone.
- `argument_schema`: `actions` plus a JSON Schema the arguments must satisfy, with plain JSON
  Schema semantics: the policy says which properties are required and which may be absent, so
  "absent or an integer" is expressible and `properties` alone never implies presence. This is the
  hostexec host/`run_as` allow-list and the fixture's bounded `echo`.
- `github_repository`: `actions` plus `owner`/`repository`; the console's fixed-repository check,
  including the search-qualifier boundaries a JSON Schema cannot state.
- `github_public_repository`: `actions` only; the target must be confirmed public by a live
  unauthenticated GitHub lookup.

Kinds that consult state outside the arguments ("the caller can already do this directly" in
Kubernetes) arrive with the Actions that need them, as `github_public_repository` did.

A **policy set** is the shared unit and the only thing a subject ever references. Its three lists
mean exactly what they say:

- `autoApproveIf`: a request matching any policy here is auto-approved.
- `autoDenyIf`: a request matching any policy here is auto-denied.
- `autoDenyUnless`: a request matching none of the policies here is auto-denied.

Deny wins over approve; a request matching nothing takes the human path. The first slice
implements `autoApproveIf` only; the other two lists are schema now, behavior later, and a set with
only `autoApproveIf` is the v1 object.

A **binding** joins one subject to sets, by reference only; a one-off grant is a small set of its
own. A subject may have many bindings; the effective policy is the union of the unexpired bindings'
sets, evaluated with the precedence above. `expiresAt` makes an expired binding equivalent to an
absent one. Sandbox subjects pin the live UID, so a binding whose Sandbox is gone is inert.

## Ownership

- Policy sets and caller ServiceAccounts normally live in Git next to the environment's Action
  Service settings; one created at runtime by the app or kubectl is simply not Flux-owned.
- The integration app writes one `ActionPolicyBinding` per Sandbox it creates, alongside the
  `EgressBinding` it already writes, with an `ownerReference` to the Sandbox so both are garbage
  collected with it. Which sets a preset selects is the app's knowledge, kept in the app's own
  labels and annotations; the Action Service reads `spec` only and never sees preset language.
- Widening one Sandbox later is another binding for the same subject, written through the app or
  kubectl, usually with `expiresAt`. Re-resolving a preset touches only the bindings the app owns.
- ServiceAccount bindings are ordinarily Git-managed; a runtime, expiring one works the same way.
- No caller-controlled field selects a binding: subjects resolve from the authenticated
  `SandboxPrincipal` or the Connection's ServiceAccount, never from `origin`, `correlation`,
  Thread ID, or a claimed type.

## Evaluation and evidence

- **Admission**: resolve the caller's bindings and sets from the informer, evaluate deny lists,
  then run `autoApproveIf` policies through the existing `DecisionProvider` aggregation. A request
  matching no list stays on the human path.
- **Evaluate once.** Policies are evaluated at admission against the objects as they are then,
  and never again for that Action. A later edit, expiry, or deletion changes the next Action's
  Decision, not this one's, the same as a human approval is not withdrawn by a later change of
  mind. Dispatch keeps the caller-authority checks that exist today, an active grant and an
  eligible ServiceAccount, and adds no policy re-evaluation. Stopping approved-but-unclaimed work is an operator
  cancel, a separate general feature, not a policy concern.
- **Evidence**: the Decision records binding names and `resourceVersion`s, set generations, and
  the leaf policy that matched, so a Decision explains itself after the objects change.
- **Freshness**: until the informer has synced, every caller is human-only. A set or binding that
  fails Python validation contributes nothing and reports `Ready=False` with the message in its
  status, so a bad runtime edit is visible in `kubectl get`.
- **Context**: `DecisionContext` gets a typed caller, `SandboxCaller` or `ServiceAccountCaller`
  with its grant revision, plus the resolved bindings, replacing the optional `agent_identity`.

## Worked example

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicySet
metadata: { name: public-coder, namespace: agentplane-staging }
spec:
  autoApproveIf:
    - type: exact_actions
      actions: { github: [get_file_contents, search_code, list_commits] }
    - type: argument_schema
      actions: { github: [create_issue] }
      schema:
        properties:
          owner: { const: agentydragon }
          repo: { const: ducktape }
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: claude-code-web
  namespace: agentplane-staging
  labels: { agentplane.allegedly.works/action-caller: "true" }
---
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicyBinding
metadata: { name: claude-code-web-public-coder, namespace: agentplane-staging }
spec:
  subject: { serviceAccount: { namespace: agentplane-staging, name: claude-code-web } }
  policySets: [public-coder]
---
# Written by the integration app when it creates Sandbox coder-7f3a from its preset.
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicyBinding
metadata:
  name: coder-7f3a-public-coder
  namespace: agentplane-staging
  labels: { app.agentplane.allegedly.works/managed-by: integration-app }
  ownerReferences:
    - { apiVersion: agentplane.allegedly.works/v1alpha1, kind: Sandbox, name: coder-7f3a, uid: 2c1d9e1a-… }
spec:
  subject: { sandbox: { name: coder-7f3a, uid: 2c1d9e1a-… } }
  policySets: [public-coder]
---
# The operator widens that one Sandbox for the afternoon.
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicyBinding
metadata:
  name: coder-7f3a-ducktape-push-20260912
  namespace: agentplane-staging
  ownerReferences: [same owner]
spec:
  subject: { sandbox: { name: coder-7f3a, uid: 2c1d9e1a-… } }
  policySets: [ducktape-push]
  expiresAt: "2026-09-12T20:00:00Z"
```

A `search_code` in `agentydragon/ducktape` auto-approves for the Sandbox and for the OAuth client
alike; removing that action from `public-coder` makes the next one wait for the operator for both,
with no binding edited; at 20:01 the push grant is gone and the Decisions it produced still name
the binding revision they used.

## Steps

1. **Integration app.** Write the Sandbox binding at creation from the preset's set list, and show
   what a Sandbox can currently do: its unexpired bindings, their sets, and the resulting lists.
   Read-only; no runtime editing surface yet.

## Console policies the Action Service cannot express yet

The Haku console's `auto_approval_policies` (`cluster/k8s/haku/console/config.yaml`) is the
reviewed authority this model replaces. Its GitHub policies exist here as sets. What remains, each
with what it needs; an entry leaves when its set can be written.

- **`exact_tools` for servers with no ActionGroup**: `gmail_reads`, `google_calendar_reads`,
  `grocy_reads` (`grocy-sf`), `tana_safe_tools` (`tana-rw`), `postscanmail_reads`
  (`postscanmail-mcp`), `home_assistant_reads` (`home-assistant`), and the console's own
  in-process `sandbox` (`haku_sandbox_control`) and `grants` servers (`kubernetes_reads`,
  `grants_whoami`, `grants_own_revoke`). Each is a plain `exact_actions` set once the backend is an
  ActionGroup in the Action Service settings, with its executor credential (operator OAuth
  linkage for Google, a static bearer or in-cluster route for the rest) and network-policy egress.
  `sandbox` and `grants` are console-internal servers with no Action Service counterpart at all;
  they need an equivalent surface before a set can name them.
- **`home_assistant_entity_control`** (`home_assistant_desk_light_control`): every Home Assistant
  write is one generic `ha_call_service`, so the console's evaluator allow-lists the argument keys
  it has reviewed and admits one entity with its listed services. Argument-only, so once a
  `home-assistant` ActionGroup exists this is either an `argument_schema` set (`const` entity,
  `enum` services, `additionalProperties: false` over the reviewed keys) or a kind if the
  configured entity map stays the operator's vocabulary.
- **`gmail_label_namespace`** (`managed_gmail_labels`): `labels_patch`/`labels_delete` name a
  label by id, so the evaluator resolves the id to a name through the Gmail API before checking
  the prefix. A kind with an injected Gmail client, arriving with the `gmail` ActionGroup and its
  operator-linked credential.
- **`grant_self_list` and grants self-introspection** (`grants_own_list`,
  `grants_self_introspection`): `list_grants(principal=self)` is argument-only, an
  `argument_schema` set over a `grants` ActionGroup; but the grant model itself is console-owned,
  so this waits on the Action Service having its own grant surface, not on a kind.
- **Schema auto-denial** (`autoDenyIf` equivalent): the console records a call whose arguments
  fail the registered tool schema as born-denied. The Action Service refuses such a request at
  admission before persisting anything, so the audit row the console keeps does not exist here;
  matching it needs `autoDenyIf` semantics (`DENY_LISTS` in the task DAG) and a recorded, denied
  Decision for the schema miss.
- **Kubectl passthrough redundancy check** (`kubectl_passthrough_redundancy_check`, commented out
  in the console): auto-deny a `kubectl-passthrough-mcp` call the caller's own Kubernetes identity
  already covers by SubjectAccessReview, pointing at the direct path. A kind with an injected
  authorization service and a caller-to-Kubernetes-identity mapping, on `autoDenyIf`. The console
  keeps it disabled because direct access is not yet an equivalent substitute (its kubeconfig
  cannot execute the POST/SPDY transport the passthrough carries); the same condition gates it
  here.

## Later

- A runtime editing surface for sets and bindings; kubectl is the first slice's editor.
- Agent-initiated, operator-approved privilege changes for both caller classes: an Action that
  requests an additional binding for the caller's own subject, or the removal of one, rendered for
  the operator as that specific request rather than a generic approval card. Optionally, creating a
  labeled ServiceAccount during enrollment instead of a Git edit.
- A per-task identity fork, so one thread of a wide OAuth-bound identity can hold a permission its
  siblings do not.
- A TokenReview admission path for clients that hold a ServiceAccount token.

## Acceptance

- A harness in a Sandbox Thread submits a configured Action via workload authentication; its
  binding and in-list arguments produce auto-approval and one Execution. The same request from a
  Sandbox with a different binding does not inherit that allow; an argument miss takes the human
  path. Same-preset Sandboxes retain separate reads and idempotency.
- Several `public-coder` Sandboxes and the `claude-code-web` ServiceAccount reference one set; one
  edit reaches both caller classes without touching bindings, and the Decisions record which set
  generation they evaluated.
- An expiring binding auto-approves an Action admitted before `expiresAt` and not one admitted
  after; the earlier Action still executes if it is claimed after expiry.
- Forged type/preset/identity fields, an invalid set, an unsynced informer, and a deleted binding
  grant nothing; a ServiceAccount whose binding names a missing set is human-only, never allowed.
- The Sandbox slice is proven independently of external enrollment; the first human-approved
  external connection does not wait on it.
