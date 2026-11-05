# Identities, workload authentication and Kubernetes accounts

Status: **proposal, 2026-11-05.** The operator selected three directions that day (recorded in
[`NETWORK_WORKLOAD_IDENTITY`](task_dag.md#network_workload_identity--spiffe-as-the-one-way-agent-workloads-authenticate)
and [`AGENT_PRINCIPAL_MODEL_DESIGN`](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)):
agent workloads authenticate to the Runner Gateway and the central egress proxy with SPIFFE SVIDs,
not ServiceAccount tokens; Agentplane owns agent identity and no agent is provisioned a Kubernetes
ServiceAccount by default; a Kubernetes account is a resource of its own that several identities may
act as, with grants attached by bindings rather than fields, so Flux and runtime writers can share
one account. Everything below the direction (kind names, field shapes, impersonation versus
TokenRequest, the migration order) is this plan's proposal and waits on the open questions at the
end. Policy objects stay Kubernetes objects.

## What this separates

Today one object, the Sandbox-owned ServiceAccount, plays four roles: the workload credential the
egress proxy and first-party services authenticate by TokenReview, the `kubectl` identity (the Pod's
own projected token, substituted by `projectedWorkloadToken`), the `PolicyBinding` subject, and the
execution principal History and Dispatcher pin by UID. Its lifetime is the Sandbox's. The proposal
gives each role its own object:

| Role                        | Today                                  | Proposed                                                           |
| --------------------------- | -------------------------------------- | ------------------------------------------------------------------ |
| Who the agent is            | ServiceAccount name and UID            | `Identity`                                                         |
| Proof of a workload         | Projected SA token, TokenReview        | SVID on the egress hop; SA token only for workloads without one    |
| Policy subject              | ServiceAccount                         | `Identity`, or `KubernetesAccount` for Kubernetes rules            |
| Kubernetes reach            | The Pod's own SA, granted RoleBindings | `KubernetesAccount`, acted as through the proxy; none unless bound |
| Execution principal records | ServiceAccount UID                     | Identity name and UID, plus the workload incarnation as provenance |

## Objects

### `Identity`

Namespaced in the policy namespace. The spec is a description; the identity is a subject and
nothing else, and must not grow harness, model or environment fields
([principles](../docs/principles.md) § no opaque bundles). Status is written by controllers:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: Identity
metadata:
  name: agent-123
  namespace: agentplane-staging
  ownerReferences: [{ kind: Sandbox, name: agent-123, uid: ... }] # Flux-owned for a standing agent
spec:
  description: Daily CI agent
status:
  workloadCredentials: # what authenticates as this identity; controller-written, never caller-supplied
    - spiffeID: spiffe://agentplane/environment-runtime/agentplane-staging/<sandbox-uid>/<credential-id>
    - serviceAccount: { namespace: agentplane-staging, name: openclaw } # workloads with no SVID only
```

Who creates it decides its lifetime: the Sandbox Controller for a Sandbox-created agent (owned by the
Sandbox, as its SA is today), Flux for a standing agent, Dispatcher if Session-managed identities
land ([Session/environment model](session_environment_model.md#sessions-serviceaccounts-and-environments)).

### `KubernetesAccount`

A ServiceAccount on one apiserver that Agentplane keeps, in a namespace dedicated to this Agentplane
instance there. The spec holds only what is intrinsic to the account; roles and users reach it by
binding.

```yaml
kind: KubernetesAccount
metadata:
  name: shared-diagnostics # Flux-authored; no expiry
spec:
  apiServer: local # a target the Policy Controller and proxy are configured with
---
kind: KubernetesAccount
metadata:
  name: agent-123-extra # app-authored on an approved request
  ownerReferences: [{ kind: Identity, name: agent-123 }]
spec:
  apiServer: local
  expiresAt: "2026-11-05T12:34:00Z"
status:
  serviceAccount: { namespace: agentplane-k8s-accounts, name: agent-123-extra, uid: ... }
  conditions: [{ type: Ready, ... }]
```

### `PolicyBinding` subject kinds

`spec.subject` becomes a oneof of `identity` and `kubernetesAccount`; the ServiceAccount subject
goes with the cutover. The rest of the binding, its sets, `expiresAt` and ownership, is unchanged,
so one expiry mechanism and one admission surface cover every grant.

```yaml
kind: PolicyBinding # Flux: standing roles on the shared account
spec: { subject: { kubernetesAccount: shared-diagnostics }, policySets: [cluster-diagnostics] }
---
kind: PolicyBinding # app: every agent gets more for an hour
spec:
  subject: { kubernetesAccount: shared-diagnostics }
  policySets: [cnpg-read]
  expiresAt: "2026-11-05T13:00:00Z"
---
kind: PolicyBinding # who may act as it; the basic set carries this for every agent
spec: { subject: { identity: agent-123 }, policySets: [k8s-account-shared-diagnostics] }
```

Which rule types mean something on which subject:

| Rule type                                                    | `identity` subject | `kubernetesAccount` subject |
| ------------------------------------------------------------ | ------------------ | --------------------------- |
| `egress`, `action_auto_approve`, `session_inbox_access`      | enforced           | ignored, reported           |
| `kubernetes_role_binding`, `kubernetes_cluster_role_binding` | ignored, reported  | enforced                    |

"Ignored, reported" means the reader's condition names the inapplicable rule rather than staying
silent; admission can reject once the `serviceAccount` subject is gone.

### Generated per-account objects

For each `KubernetesAccount` the Policy Controller emits, owned by it:

- an `EgressCredential` of the same name with source `kubernetesAccount: {name}`, targets
  `Authorization: Bearer`, whose placeholder is therefore `agentplane-credential-<account>`;
- a `PolicySet` `k8s-account-<name>` holding one `egress` rule to that apiserver's host with that
  `credentialRef`.

Binding an identity to that set is the whole act of letting it act as the account. The controller
writing a `PolicySet` is new authority; an admission rule limits it to this one shape, as
`orchestration_v1` launcher grants are limited today.

## Workload authentication

The SVIDs already exist: the Sandbox Controller requests one cert-manager `Certificate` per
incarnation with URI SAN `spiffe://agentplane/environment-runtime/<ns>/<sandbox-uid>/<credential-id>`
([Sandbox Controller SPEC](../sandbox_controller/SPEC.md)), Environment Runtime presents it on the
control connection, and the Firecracker launcher serves it over the Workload API. The egress sidecar
or relay presents the same SVID on its hop to the central proxy in place of the proxy-audience
projected token; the proxy verifies it with the Gateway's transport package
(`environment_runtime_transport`) and resolves the Sandbox UID in the path to the Sandbox's
`Identity`. No SPIRE, Cilium mutual authentication or mesh is involved.

Rules then key on the identity. Everything the egress SPEC says about a subject holds with
"ServiceAccount" read as "Identity": a subject no binding names reaches no rule, and the connection
address is not identity evidence.

Workloads without an SVID, such as the public coder's OpenClaw fronted by the proxy, keep presenting
a projected SA token; `status.workloadCredentials` maps that account to its identity. The MCP edge
binds a Connection to an `Identity` instead of a ServiceAccount.

## Kubernetes execution

**Policy Controller, per `KubernetesAccount`:** ensure the ServiceAccount on the target apiserver and
the RoleBindings that the Kubernetes rules of every live binding naming the account expand to,
exactly as it expands them for an SA subject today (additive, one native binding per binding, role
and namespace, pruned when no live binding grants them). Delete the SA with the account at its
`expiresAt` or owner deletion; tokens for a deleted SA are rejected by UID, so nothing else needs
revoking. Reconcile on start covers controller downtime, as it does now.

**Egress proxy, acting as the account.** Two options, both bounded to the accounts namespace:

- **Impersonation (recommended).** The proxy's own apiserver credential gets `impersonate` on
  `serviceaccounts` in the accounts namespace. Per request it replaces the placeholder with its own
  bearer and adds `Impersonate-User: system:serviceaccount:<ns>/<account>`; the apiserver adds the
  `system:serviceaccounts` groups itself. No token is minted or cached, revocation is the binding
  going, enforced on the next request, and the audit log records both the account and the proxy as
  original user. Impersonating ServiceAccounts rather than users keeps OIDC users bound by name out
  of reach.
- **TokenRequest.** `create serviceaccounts/token` in the same namespace, short TTL, cached per
  account. Needed only against an apiserver where impersonation is not on offer.

A remote apiserver is a named target in both the controller's and the proxy's settings carrying its
kubeconfig, the way tofu-controller holds Forgejo credentials.

**What the agent sees.** One kubeconfig with a context per account its bindings currently grant,
each context's token the account's placeholder; the rules endpoint already lists the credentials a
subject may present. The preset picks the default context; `kubectl auth whoami` shows the account.
Nothing inside the sandbox ever holds a real token.

## Lifetimes

| Case                                  | Identity           | Kubernetes account                            | Grants                                     |
| ------------------------------------- | ------------------ | --------------------------------------------- | ------------------------------------------ |
| Standing agent (Haku, daily CI agent) | Flux               | Flux-authored shared account, or none         | Flux bindings                              |
| Sandbox-created agent                 | Sandbox Controller | None, or the shared account via `basic`       | Sandbox-owned binding from the launch sets |
| Agent with no Kubernetes need         | as above           | None; no object anywhere                      | No Kubernetes rule reaches it              |
| Temporary extra permissions           | unchanged          | App-authored, owned by the identity, expiring | Role and use bindings owned by the account |
| Widening a shared account for an hour | unchanged          | unchanged                                     | One expiring binding on the account        |
| Identity deleted                      | gone               | Owned accounts garbage-collected              | Owned bindings garbage-collected           |

## Cutover

One incompatible cutover, not a staged migration (operator, 2026-11-05: downtime in existing
services until images converge is acceptable; [`AGENTS.md`](../AGENTS.md) § Deployed state is
disposable). No dual readers, no `serviceAccount` binding subject, no tolerant decoding of SA-keyed
rows. The change set lands together, and the PR states the rollback: revert the Flux pins and
delete the objects of the new kinds.

- **CRDs and catalog:** `Identity`, `KubernetesAccount`, the new `PolicyBinding` subject oneof and
  the `kubernetesAccount` credential source; Flux-rendered identities and accounts for the static
  agents (`claude-ai`, `haku-agent`) in place of their ServiceAccounts, `shared-diagnostics` with
  its roles binding, every identity binding rewritten from its SA subject, and the `basic` set
  carrying the use binding for the shared account.
- **Controllers:** the Sandbox Controller writes an `Identity` and its binding per Sandbox and no
  ServiceAccount, and records the SPIFFE ID on the identity; the Policy Controller reconciles
  accounts and identity subjects and nothing else.
- **Data plane:** the egress sidecar and the Firecracker relay present the SVID on the hop; the
  proxy accepts SVIDs, resolves identities, and drops the proxy-audience and Kubernetes-audience
  projected tokens; the kubeconfig carries a context per account.
- **Services:** History, Dispatcher, Notification, the Action Service, LLM ingress and the central
  sampler authenticate, authorize and record by identity. Rows that pin an SA UID are rewritten by
  the migration where the identity is known and marked historical where it is not, as History's
  migration 8 did for harness provenance; same-principal reads of historical rows stay unavailable
  and operator access covers them.

What stops during convergence: every running Sandbox loses egress and platform routes from the
moment the proxy drops projected tokens until its sidecar image carries the SVID hop, and `kubectl`
until the kubeconfig and the shared account exist. The Gateway path is unaffected, since it
verifies SVIDs already. Running Sandboxes are replaced rather than patched: a Pod's ServiceAccount
cannot change in place, and the new launch writes the identity.

## Open questions and tricky bits

1. **Platform routes without a bearer.** Notification, Dispatcher, runner ingress, the Action
   Service and LLM ingress authenticate per-audience projected tokens today, substituted by
   `projectedWorkloadToken` or forwarded by `authenticatedWorkloadToken`. An SVID gives the proxy
   nothing to forward. The options are TLS passthrough so each service verifies the SVID itself,
   which removes the proxy's rule check from those routes, or the proxy as actor asserting the
   identity under the [delegated authorization contract](../docs/delegated_authorization.md), which
   concentrates trust in the proxy. Decide per route before any projected token is removed.
2. **One key, two holders.** Environment Runtime holds the SVID for the control connection; the
   relay would hold it for the hop. Either both read one Secret, or the controller issues a second
   certificate under the same Sandbox path for the relay. Agent code that can read Runtime's key can
   already act as the workload at the Gateway; presenting it at the proxy adds no reach beyond that
   identity's rules, but say so where the SPEC states what the certificate authenticates.
3. **SVID to identity.** The path carries the Sandbox UID and credential ID, not the identity. The
   proxy needs the Sandbox-to-identity fact from somewhere it trusts: the Sandbox Controller writing
   `status.workloadCredentials` with the current SPIFFE ID is the proposal, and the rotation race on
   incarnation replacement needs the same fencing the Gateway applies.
4. **Impersonation versus TokenRequest** is an operator decision, and which apiservers the proxy and
   controller may reach with what credential.
5. **Kubeconfig contexts at runtime.** The kubeconfig is rendered at launch; contexts change as
   bindings come and go. Either an exec credential plugin that reads the rules endpoint, or agent
   instructions to add a context, or a single placeholder with the account chosen another way.
6. **Central sampler as an identity.** Delegation today carries an SA UID as the effective caller;
   it must carry an identity reference, and the sampler's own account must never become the agent's.
7. **Stored principals.** History's `execution_principal` and Dispatcher rows pin SA UIDs. The
   cutover migration rewrites rows whose Sandbox still exists to its identity and marks the rest
   historical; confirm that is enough for the retained Sessions the operator wants readable, since
   same-principal reads of historical rows are unavailable.
8. **Same-environment invariant** ([identity contract](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission))
   becomes "same identity"; co-resident Sessions share one SVID, so it holds by construction, and
   admission must still refuse a Session whose identity differs.
9. **Revocation granularity.** Removing an identity's binding stops substitution on the next request;
   an established TLS connection to the apiserver carrying earlier requests is not cut. State whether
   the proxy ends tunnels on rule loss, as the egress SPEC does for other credentials.
10. **Admission for generated sets.** The controller writing `PolicySet`s needs a rule that limits it
    to the one-egress-rule shape, and the per-account set must not be bindable to a different host.
11. **Second control plane.** Accounts live on target apiservers regardless of where Agentplane's
    records live, so this plan is neutral across the [architecture directions](architecture_overview.md);
    `Identity` and `KubernetesAccount` move with the Sandbox family under D3.

## Exit

A reviewed decision on items 1 and 4, then the migration steps as DAG nodes with these acceptance
checks: an SVID-authenticated egress hop and one platform route in the system tests; a Sandbox with
no Kubernetes binding has no ServiceAccount and `kubectl` fails closed; two identities act as one
shared account and one of them loses it without the other noticing; a temporary account expires and
its SA, RoleBindings and both bindings are gone with no controller restart; deleting an identity
removes everything it owned. When those land, this file's content moves to `docs/` and the file is
deleted.
