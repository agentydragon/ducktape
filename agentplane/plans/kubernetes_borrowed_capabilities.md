# What Agentplane borrows from the Kubernetes API: design exploration

Status: **design exploration, 2026-10-18.** Nothing is scheduled. It authorizes no second apiserver,
store, API or migration. If the idea is revived, what survives graduates to a doc under `docs/`.

[API aggregation](api_aggregation_exploration.md) asks which service-owned records to present
through the Kubernetes API. This asks the inverse: for what Agentplane already keeps in Kubernetes
or takes from it, what would a replacement, or a second control plane (its own apiserver on a
dedicated etcd or on Postgres through kine), have to supply, and what cannot leave the cluster.

## The cut

"The Kubernetes API" is two things here. The **cluster** is kubelet, Pods, ServiceAccount tokens and
RBAC over real objects. The **record machinery** is CRDs, conflict-checked writes, watches, Leases
and garbage collection. A second plane or an own implementation can only take over the second, and
some of it is tied to the first by ownership.

## What is borrowed

### Anchored to the cluster

None of these can move to a plane that has no kubelet.

- **Workload objects.** The Sandbox Controller creates a Pod or KubeVirt VM, the state and workspace
  PVCs, the launch ConfigMap, the ServiceAccount and the headless `<sandbox>-control` Service for
  each Sandbox (`sandbox_controller/`); the Runner Gateway writes the Service's EndpointSlice. They
  need scheduling, image pulls, kubelet mounts, cluster DNS and Cilium NetworkPolicy.
- **Workload identity.** Pods get kubelet-projected, Pod-bound tokens: 600 s in Sandbox Pods
  (`cluster/cdk8s/agentplane/sandbox_pod.py`), 3600 s in service Deployments. Every service
  TokenReviews each bearer, uncached ([workload authentication](../docs/workload_authentication.md)),
  and the review includes the bound Pod's existence and UID, so a deleted Pod fails it
  (`workload_auth/principal.py`). A VM guest holds no token; the relay in its launcher Pod does.
- **RBAC.** Agents run `kubectl` against the real apiserver as their Sandbox's ServiceAccount through
  the egress proxy ([Sandbox Actions](../action_service/sandbox/README.md)): product behavior, not a
  record. `PolicySet` `kubernetes_*` rules become native RoleBindings
  ([policy sets](../docs/adr_policy_sets.md)). The `egress` and `action_auto_approve` rules are
  evaluated by Agentplane's own readers, not by the apiserver.
- **Secrets.** Egress credentials are Secrets in an isolated namespace, populated by External
  Secrets and SOPS, listed and watched by the egress proxy.
- **KubeVirt.** virt-controller and virt-handler on KVM nodes, the VM on the same apiserver as the
  Sandbox's other children, and the console through the `subresources.kubevirt.io` aggregated API.
- **Admission.** The Kyverno policy `<namespace>-launcher-relay` authenticates a launcher Pod by
  walking its owner chain, launcher Pod to VMI to VM to Sandbox to ServiceAccount, by UID, and
  injects the relay (`cluster/cdk8s/agentplane/launcher_relay.py`).

### Record machinery

- **Five CRD kinds in one group:** `Sandbox`, `SandboxTemplate`, `PolicySet`, `PolicyBinding`,
  `EgressCredential` (`crds/`). The apiserver validates schema, defaults and the CEL rules (idle
  suspend consistency, name length, exactly one of Pod or VM), tested against a real apiserver
  (`crds/test_sandbox_admission.py`). The embedded Pod, VM and PVC specs are schemaless, so the
  apiserver validates them only when the controller creates the Pod or VM.
- **Conflict-checked writes.** Exclusive create (409), merge patches carrying `uid` and
  `resourceVersion` (`sandboxes/inventory.py`, the Dispatcher's wake in
  `runner_gateway/dispatcher/sandbox_kubernetes.go`), delete preconditions, and server-side apply
  with one field manager per reader on `PolicyBinding` status (`policy_sets.py`). `resourceVersion`
  is object-wide, so a status write loses to a concurrent spec patch, which the idle-suspend wake
  race relies on (`sandbox_controller/idle.go`).
- **Status and conditions.** A status subresource with its own RBAC verb, conditions with
  `observedGeneration`, and a per-reader condition on each `PolicyBinding`.
- **List-then-watch.** One Python engine (`util/kubernetes_watch.py`) serves egress, the Action
  Service, the app's live index and `policy_watch.py`: list, watch from that version, relist on any
  failure, no bookmarks; a stale view fails closed. The Go services use controller-runtime caches
  (Sandbox Controller, Policy Controller, the Dispatcher's Sandbox cache) and a client-go informer
  on Leases (Gateway).
- **Leases.** The connection Lease `<sandbox>-runner` fences with `leaseTransitions` as the epoch,
  names the holding Gateway replica, and carries Sandbox-level runner facts as annotations that the
  controller reads ([session control](../docs/adr_sandbox_session_control.md) D5). The Gateway
  renews it every 15 s while a runner is connected. The Dispatcher's `dispatcher-<sandboxUID>`
  Lease is renewed every 10 s and paired with a Postgres fence row. Both controllers elect a leader
  through a Lease.
- **Ownership and deletion.** The Sandbox owns every child. A finalizer holds its deletion until
  the runner has quiesced, and only the Pod or VM is deleted explicitly; garbage collection removes
  the rest. Ownership is also identity: the Gateway accepts a Pod as a Sandbox's by its owner UID
  (`runner_gateway/gateway/sandboxes.go`), and the relay policy above walks the same chain.
- **Provenance.** The Flux label `kustomize.toolkit.fluxcd.io/name` marks git-owned `PolicyBinding`s,
  a Sandbox owner marks Sandbox-owned ones, and the rest are an operator's
  (`sandboxes/egress_views.py`). `managedFields` records which actor wrote what.
- **Delivery.** Flux applies the generated manifests. `agentplane-staging` carries the Agentplane
  records together with Deployments, Roles, Cilium policies and ExternalSecrets. The cluster-scoped
  CRDs sit in `agentplane-crds` with `prune: false` and are shared by staging and testing.

## Size today

Read on staging, 2026-10-18, with `kubectl` as `claude-ai` in a build Sandbox, as list JSON
including `managedFields`:

| Kind              | Objects | Bytes   |
| ----------------- | ------- | ------- |
| `Sandbox`         | 3       | 16.7 KB |
| `SandboxTemplate` | 3       | 46.2 KB |
| `PolicySet`       | 40      | 91.7 KB |
| `PolicyBinding`   | 7       | 20.2 KB |
| `Lease`           | 9       | 11.9 KB |

That is about 190 KB in staging and about 50 KB in `agentplane-testing`. `claude-ai` cannot list
`EgressCredential`s; the generator renders 22 for staging. etcd itself was not read, so its load and
headroom are unknown.

The records that grow with the number of Sandboxes and are written continuously are the Leases.
The three staging runner Leases were last renewed 17 to 25 hours earlier, so no runner was
connected, while the three Dispatcher Leases renewed every 10 s regardless. By the intervals above
that is 0.1 write per second per Sandbox the Dispatcher has claimed, and 0.17 while its runner is
connected, so 1,000 Sandboxes would write 100 to 170 times per second. That is arithmetic, not a
measurement at scale. Whether the Dispatcher Lease is still needed beside its Postgres row was not
checked.

## What an own implementation must supply

Costs are a judgment, relative to each other.

| Behavior                                                                                                 | Cost        | Note                                                                                                                                                                                                                                                        |
| -------------------------------------------------------------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Exclusive create, `uid` and version preconditions                                                        | Low         | A version column and `UPDATE ... WHERE version =`.                                                                                                                                                                                                          |
| Schema, defaults, enums, CEL on five kinds                                                               | Medium      | Port or drop each rule at the API edge.                                                                                                                                                                                                                     |
| Status split, conditions, generation, per-reader condition merge                                         | Medium      | A separate write path and authorization for status.                                                                                                                                                                                                         |
| List plus a resumable watch with a "gone, relist" signal                                                 | High        | The one hard piece. Postgres watches here send a snapshot then changes and cannot resume ([API aggregation](api_aggregation_exploration.md)). Python needs only a client of the same shape; the Go controllers need a controller-runtime-compatible source. |
| Label selectors, field indexes                                                                           | Low         | A label filters the controller caches; the controllers index on `spec.template`.                                                                                                                                                                            |
| Finalizers and cascade deletion                                                                          | Medium–High | A sweeper that deletes by label for each child kind, and a held deletion.                                                                                                                                                                                   |
| Lease semantics: compare-and-set epoch with TTL, fan-out, runner facts to the controller, holder routing | Medium      | The Dispatcher half is already Postgres. The Service and EndpointSlice route to the holding replica and stay unless routing is replaced.                                                                                                                    |
| Provenance and audit                                                                                     | Low–Medium  | A writer column would replace `managedFields`.                                                                                                                                                                                                              |
| The client ecosystem                                                                                     | —           | `kubectl`, Flux and `kubernetes_asyncio` (about 27 non-test Python files) work against a Kubernetes-shaped plane. An own API loses them all.                                                                                                                |

## Ways to do it

### A. A second kube-apiserver, no nodes

Own etcd, or kine on the shared CNPG cluster, hosting some of the five kinds. A nodeless apiserver
with these CRDs already runs in tests (`testing/kubernetes_apiserver.py`); `x/k3s_env` is a whole
separate cluster for the test tier, not a second plane.

- **Gains.** Etcd load, backup and upgrade cadence are separated from the cluster's. The protocol
  is unchanged; a service that touches both planes (the Sandbox Controller, Policy Controller,
  Gateway and app) holds a second client. [kine](https://github.com/k3s-io/kine), per its README,
  implements the etcd API subset the apiserver needs (Lease, Watch, KV with Txn) over SQLite, MySQL,
  Postgres, NATS or T4, so no Raft.
- **Does not gain RAM.** The
  [kube-apiserver reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)
  says `--watch-cache` defaults to true and that `--watch-cache-sizes` does not apply to CRDs or
  aggregated resources. Whether
  `--watch-cache=false` covers custom resources was not checked. No Session data is in these
  records, so the question does not arise for them; History needs the facade in the aggregation
  exploration.
- **Identity stays on the primary.** A second apiserver holds no Pods, so it cannot perform the
  bound-Pod check of a TokenReview. Callers keep reviewing against the primary. Accepting the same
  bearers on the second plane is the general-Kubernetes route of webhook token authentication to the
  primary; it was not tried here. The second plane's RBAC is a separate set of Roles to maintain.
- **Ownership does not cross.** General Kubernetes behavior, not tried here: the garbage collector
  resolves an owner within its own apiserver, so a Pod on the primary whose `ownerReference` names a
  Sandbox that exists only on the second plane is collected as an orphan. The Sandbox Controller
  would drop those references and delete each child kind explicitly. The Gateway's Pod-to-Sandbox
  check and the relay policy would need another binding than an owner UID.
- **A second control plane to run:** certificates, HA, upgrades, backups, monitoring.

### B. Which kinds could move

A kind stays on the primary when it owns primary objects, is owned by them, or protects them.

- **The Sandbox family** (`Sandbox`, its `PolicyBinding`, its Leases) can move together: the
  binding and Leases are owned only by the Sandbox, and the Policy Controller already keys native
  RoleBindings by binding UID in an annotation. The family grows with Sandbox count and holds the
  hot writes. Its cost concentrates in one link, the Sandbox's ownership of its workload children
  (§ A, ownership does not cross).
- **The catalog kinds** (`PolicySet`, `EgressCredential`, `SandboxTemplate`, and the static
  `PolicyBinding`s) own nothing and are owned by nothing, so they move cheaply. They are the
  Flux-written kinds: `SandboxTemplate`s and `PolicySet`s are about 140 KB in staging, static between
  deploys, and gain nothing from moving except that a reader then watches one plane.
- **Never:** Pods, VMs, PVCs, Services, EndpointSlices, ServiceAccounts, Secrets, RBAC and
  admission.

The kinds with a reason to move are tied down by ownership; the kinds that move freely have no
reason to.

### C. An own API on Postgres

The Dispatcher is the precedent: its own database, gRPC and a Postgres-backed watch. Hot fields
and per-Session rates are unconstrained, and a record shares a transaction with its service's
tables. It has to supply every row of the table above, the watch included, and loses the client
ecosystem and Flux's apply.

### D. Facade apiserver

Covered in [API aggregation](api_aggregation_exploration.md). It answers where per-Session data
could present as Kubernetes objects, not where Sandbox records live.

## GitOps glue

Only the Flux-written kinds that move need glue. Presets need none: they are app settings
(`cluster/cdk8s/agentplane/app_settings.py`), and a Sandbox carries only a `preset` label. The
shared permission sets are `PolicySet`s: 39 in staging and 11 in testing, plus one in each delivery
manifest.

1. **Flux applies to the second plane.** `spec.kubeConfig` makes a Kustomization apply, health
   check, prune and delete on the cluster that kubeconfig names. Its `secretRef` must be in the
   Kustomization's namespace and is rotated by updating the Secret; `configMapRef` builds a
   kubeconfig from workload identity for `aws`, `azure`, `gcp` or `generic`; with `serviceAccountName`
   Flux impersonates that account on the target, and the Kustomization's namespace must exist on
   both clusters
   ([Flux Kustomization reference](https://fluxcd.io/flux/components/kustomize/kustomizations/)).
   The typed wrapper has the field (`cluster/cdk8s/providers/flux`), `flux_kustomization` has no keyword for it yet
   (`cluster/cdk8s/flux.py`), and no manifest sets it. The Agentplane records would leave
   `agentplane-staging` for a Kustomization of their own, with a twin of `agentplane-crds` on the
   second plane. The reference does not say how `dependsOn` or SOPS decryption behave across
   clusters.
2. **A mirror reconciler.** Flux keeps applying the catalog to the primary, which stays the reviewed
   source. A controller copies labelled objects to the second plane with server-side apply under its
   own field manager and prunes by label. The second plane is then a disposable replica, drift stays
   visible in Flux, and the cost is one more controller and a staleness window.
3. **Readers watch both planes.** The catalog stays on the primary, runtime records go to the
   second, and each reader holds two sources and a freshness gate per source. The app already
   rejects a binding that names an unknown `PolicySet` at creation. The Python engine is
   instantiated twice; the Go readers need a second cache.
4. **A git-to-API importer, for an own API.** The precedent for repo-declared state reaching a
   system that is not Kubernetes is tofu-controller (`tf/gitops/`: Authentik providers, Forgejo
   tokens), which keeps its state in CNPG. A new target means a provider or an apply job with its
   own prune rule.

Option 3 pairs with moving only the Sandbox family, option 1 with moving the catalog as well, and
option 4 only with C.

## Records that outlive their Sandbox

Where a record lives does not change what deleting its Sandbox does to it: on a second plane the
Sandbox is still deleted, and whatever names it still dangles. What matters is each kind's lifetime
and what a reference needs at read time.

- Retained history is read through History's own record of the writing Sandbox: namespace, name
  and, once kept, the UID. A grant matched on that record needs no Sandbox object, so direct and
  `allSandboxes` archive grants keep working after deletion wherever their rules are stored. A rule
  that names a Sandbox is a string in a `PolicySet`, not a reference the apiserver resolves
  ([deleted-Sandbox history](task_dag.md#history_access_after_deletion--who-reads-a-sandboxs-history-once-it-is-deleted),
  [archive apart from live objects](multiagent_communication.md#option-authorize-the-archive-apart-from-live-objects)).
- What dangles are the kinds the Sandbox owns: membership CRs, bindings, Leases. Garbage collection
  removes them with it, which is the intended result for a membership that covers live members only.
- A membership that must keep covering dead Sandboxes lives in a database that outlives them, keyed
  by Sandbox UID. The owner is undecided here; History Service is the default, since it already
  records the writing Sandbox. Static sets stay authored in git and a one-way reconciler copies them
  into that database (the mirror controller or git-to-API importer from the GitOps glue section).
  Its input is the authored object, and only an explicit delete of that object removes a
  membership, so it never has to infer anything from a Sandbox disappearing. Launch enrollment is
  written to the database directly and never passes through a CR, so there is no Sandbox deletion
  to capture.
- Keeping the Sandbox so its references stay valid costs a record family that never goes away. The
  Dispatcher's delivery worker lists every Sandbox on each 10 s tick and acquires or renews a Lease
  for each, suspended or not (`runner_gateway/dispatcher/delivery.go`), and each keeps its
  ServiceAccount, `PolicyBinding` and an entry in the cache of every service that watches Sandboxes.
  At the rate above, 1,000 retained Sandboxes write 100 times per second, so a retired state would
  need the Dispatcher to skip it.

## Take

Keep the records in the primary cluster's API for now. About 0.2 MB of records and no Session data
give no sign of pressure on etcd, though etcd itself was not read, and what a move would hit
hardest, the watch, ownership and identity, is what the cluster provides.

If a second plane is ever wanted, A with the Sandbox family and an explicit-deletion controller is
the shape, and its GitOps glue is option 3, or option 1 if the catalog moves too. C is worth its
watch only if the facade needs that watch anyway.

Revisit when one of these is measured: etcd pressure attributable to the Leases or other Agentplane
records, a second workload cluster, or a Sandbox count at which the Lease write rate above matters.
