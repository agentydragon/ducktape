# Architecture overview: where Agentplane's objects and data live

Status: **no direction chosen, 2026-10-19.** Written at the operator's request for one overview
before the design lanes go further (01:12 UTC: "some kind of unified idea/overview/plan"), about
"where do what objects live" (01:37), including "should we run on k8s, should our cp be the same as
the outer cluster's cp, or should we have some database for our objects that just reconciles via
e.g. tofu or something" (01:58). The operator is "uncomfortable with making definite decisions" about
this cluster of questions and wants "to go towards some point where i'll be comfortable picking a
direction" (02:14). So this file asks for no pick. It lays out whole directions, walks the
operator's workflows through each, prices building and backing out, and lists the moves that are
right under every direction and the experiments that would narrow the choice. Each plan and
[task DAG](task_dag.md) node stays authoritative until a direction is chosen; the chosen parts then
land in their owning docs and DAG nodes, and this file is deleted.

## Why the questions are one cluster

Each of these changes the others:

- whether a Sandbox's history outlives it;
- which control plane holds Agentplane's records: the outer cluster's Kubernetes API, Agentplane's
  own, or a database;
- where collection membership lives;
- whether History records the Sandbox UID, and keys Sessions by it;
- whether reads of retained history carry grants of their own.

The first two are the axes, and the other three follow from where a direction puts them. On the
first, the operator: keeping grants for Sandboxes that no longer exist is "mostly an artifact of my
workflow being a weird hybrid", and "one other consistent option would be e.g. 'deleting a Sandbox
also deletes its history records'" (02:04). The operator then made "Sandbox" concrete (09:42): its
workspace PVC "should be disposable without history disappearing", while its harness state PVC,
"whose size is O(history)", and its metadata are parts "I do not really need to dispose
independently, I think". So the lifetime axis is about what is left once the compute and the
workspace are gone, which suspending and a [workspace drop](#right-under-every-direction) dispose of
on their own. Only suspending exists today: the Sandbox owns its workspace PVC and a missing one
blocks the next start, so deleting the Sandbox is the one way to free its storage. Sandbox workloads
run on Kubernetes in every direction
([principles](../docs/principles.md): Kubernetes is a shared baseline); what differs is where records
and retained data live.

Where a record lives does not change what deleting its Sandbox does to it. On a second plane or in a
database, whatever names a deleted Sandbox still dangles unless the store keeps a record of that
Sandbox, so lifetime is an axis of its own rather than a consequence of the store.

## Four directions

- **D1. The Sandbox is the unit.** History dies with its Sandbox. A finished Sandbox is retired
  rather than deleted: stopped with `spec.wakeOnSend: false` (both fields exist today), its
  workspace dropped (not possible yet). Deleting it is the one act that drops its history. Every
  record stays in the outer cluster's API, and anything that names a Sandbox, such as collection
  membership, is a Kubernetes object it owns.
- **D2. Kubernetes for live objects, databases for what outlives them.** Today's workflow made
  consistent: a finished Sandbox is deleted, and its history, collection membership and archive
  grants stay in service databases keyed by Sandbox UID, with git-authored sets reconciled in. Live
  grants stay in `PolicySet`s.
- **D3. Agentplane's own control plane.** A nodeless kube-apiserver per environment, on kine over the
  CNPG cluster, holds the Agentplane kinds; workloads stay on the cluster. Either lifetime works, and
  retiring comes naturally, since retired Sandboxes then cost the cluster's control plane nothing.
- **D4. Database-backed Agentplane.** A service owns Agentplane's records as rows behind its own API,
  as the Dispatcher owns its queue, and tofu-controller or an importer reconciles the git-authored
  ones in, as `tf/gitops/` does for Authentik and Forgejo. A controller still writes the workload
  objects into the cluster. Either lifetime works, and outliving comes naturally, since a deleted
  Sandbox can stay a row.

D1 and D2 differ only in lifetime; D3 and D4 change the store.

## The operator's workflows in each

| Workflow                                                                                                                         | D1                                                                                                                                      | D2                                                                       | D3                                                                                                                    | D4                                                                                           |
| -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Launch a Sandbox                                                                                                                 | As today                                                                                                                                | As today                                                                 | The app writes it to the Agentplane plane; the controller creates its children on the cluster and deletes them itself | The app calls the records service; the controller follows its watch and creates the children |
| Finish with a Sandbox                                                                                                            | Retire it and drop its workspace                                                                                                        | Delete it; its history, membership and grants stay                       | Either                                                                                                                | Either; retired or deleted is a row state                                                    |
| Haku moves to a fresh Sandbox ([story 4](user_stories.md#4-haku-itself-lives-here): "the sandbox is disposable and Haku is not") | The old Sandbox's threads go if it is deleted, so it stays retired                                                                      | The old threads stay readable                                            | As D1 or D2                                                                                                           | The old threads stay readable                                                                |
| A coordinator audits its agents, finished ones included                                                                          | Grants on Sandboxes that still exist                                                                                                    | Grants matched on History's record of the writer, apart from live grants | As D1 or D2                                                                                                           | Grants and membership are rows beside the Sandbox rows                                       |
| Grant a git-authored collection                                                                                                  | Flux applies `PolicySet`s; membership is CRs owned by Sandboxes (#10987), or tags on them                                               | Flux applies `PolicySet`s; a reconciler copies the sets into History     | Flux applies to the second plane (`spec.kubeConfig`)                                                                  | tofu-controller or an importer writes rows                                                   |
| Try a CRD change on testing first                                                                                                | Not possible: one cluster-scoped CRD set from `devel` serves both                                                                       | Not possible                                                             | Possible: each plane has its own CRDs                                                                                 | Possible: each environment has its own database                                              |
| Inspect records with `kubectl`                                                                                                   | Yes                                                                                                                                     | Live ones; retained data through History's API                           | Yes, with the second plane's kubeconfig                                                                               | No; the records service's HTTP API                                                           |
| 100 Sandboxes a day for a year (the operator, 09:50)                                                                             | 36,500 retired ones stay in the cluster's etcd, about 365 MB, each with its state PVC ([below](#keeping-finished-sandboxes-as-records)) | Only live ones stay                                                      | Off the cluster's etcd, still in every watcher's cache                                                                | Off the cluster's etcd                                                                       |

The 365 MB is arithmetic from staging's object sizes ([size today](#size-today)): a Sandbox with its
`PolicyBinding` and ServiceAccount, `managedFields` included, is about 10 KB. It is not a
measurement.

## What each costs, how hard it is to back out of, and what it bets on

|     | Build from today                                                                                                                                                                                                                                                                                                                                                                                         | Back out later                                                    | Bets that                                                                                                                                           |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | A delete cascades, held by the Sandbox Controller's finalizer: a History delete RPC (none exists) keyed by a recorded Sandbox UID, Notification's inboxes and entries, and app Threads from the app's own watch, since no service calls the app. History's and the app's documented promise that history outlives the Sandbox changes                                                                    | Cheap: stop cascading. History deleted meanwhile is gone          | The operator never wants a deleted Sandbox's history back, and retired Sandboxes stay affordable in the cluster's etcd and their state PVCs on disk |
| D2  | The Sandbox UID recorded in History; a membership table with a git reconciler; archive grants apart from live grants; possibly the History re-key, which touches 11 of its 12 tables and the Electric shapes                                                                                                                                                                                             | Moderate: membership and grants move back into Kubernetes objects | Kubernetes and the databases stay consistent through UID keys and a reconciler                                                                      |
| D3  | A control plane per environment to run. Children deleted explicitly, since ownership does not cross apiservers. A new Pod-to-Sandbox binding for the Gateway and the launcher relay policy. Token authentication through the primary (not tried). A second client in services that read records and touch cluster objects, such as the app, both controllers, the Gateway, egress and the Action Service | Moderate: the protocol is the same; owner references come back    | Running a control plane costs less than what it buys: a separate etcd, CRDs per environment, CNPG backups                                           |
| D4  | The records service with a resumable watch, status, conflict checks, cascade deletion, authorization and audit, the watch being the hard piece ([below](#record-machinery-and-what-d3-and-d4-do-with-it)); every reader ported; a tofu provider or importer. Reverses the [2026-10-14 decision](../docs/adr_sandbox_session_control.md#decisions) that there is no Sandbox Service                       | Expensive: re-create the CRDs and port every reader back          | Rebuilding the Kubernetes record machinery pays for itself in lifetime and schema freedom                                                           |

D3 does not by itself keep records out of memory: the
[kube-apiserver reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)
says `--watch-cache` defaults to true and `--watch-cache-sizes` does not apply to CRDs, and whether
`--watch-cache=false` covers custom resources was not checked. The documented promises are
in the [History README](../history_service/README.md) ("its history outlives the Sandbox") and the
[app README](../app/README.md).

## What Agentplane borrows from Kubernetes

"The Kubernetes API" is two things here. The **cluster** is kubelet, Pods, ServiceAccount tokens and
RBAC over real objects, and every direction keeps it. The **record machinery** is CRDs,
conflict-checked writes, watches, Leases and garbage collection: D3 runs a second copy of it and D4
replaces it. The capabilities were traced to their call sites on 2026-10-18 by the borrowed-Kubernetes
exploration (#10990, closed after this section absorbed it).

### Anchored to the cluster in every direction

- **Workload objects.** The Sandbox Controller creates a Pod or KubeVirt VM, the state and workspace
  PVCs, the launch ConfigMap, the ServiceAccount and the headless `<sandbox>-control` Service for
  each Sandbox (`sandbox_controller/`); the Runner Gateway writes the Service's EndpointSlice. They
  need scheduling, image pulls, kubelet mounts, cluster DNS and Cilium NetworkPolicy.
- **Workload identity.** Pods get kubelet-projected, Pod-bound tokens: 600 s in Sandbox Pods
  (`cluster/cdk8s/agentplane/sandbox_pod.py`), 3600 s in service Deployments. Every service
  TokenReviews each bearer, the Go ones reusing an acceptance for 30 s
  ([workload authentication](../docs/workload_authentication.md)), and the review includes the
  bound Pod's existence and UID, so a deleted Pod fails it (`workload_auth/principal.py`). A VM guest holds no token; the relay in its launcher Pod does. A
  second apiserver holds no Pods, so it cannot do the bound-Pod check: callers keep reviewing
  against the primary.
- **RBAC.** Agents run `kubectl` against the real apiserver as their Sandbox's ServiceAccount through
  the egress proxy ([Sandbox Actions](../action_service/sandbox/README.md)): product behavior, not a
  record. `PolicySet` `kubernetes_*` rules become native RoleBindings
  ([policy sets](../docs/adr_policy_sets.md)); the `egress` and `action_auto_approve` rules are
  evaluated by Agentplane's own readers, not by the apiserver.
- **Secrets.** Egress credentials are Secrets in an isolated namespace, populated by External
  Secrets and SOPS, listed and watched by the egress proxy.
- **KubeVirt.** virt-controller and virt-handler on KVM nodes, the VM on the same apiserver as the
  Sandbox's other children, and the console through the `subresources.kubevirt.io` aggregated API.
- **Admission.** The Kyverno policy `<namespace>-launcher-relay` authenticates a launcher Pod by
  walking its owner chain, launcher Pod to VMI to VM to Sandbox to ServiceAccount, by UID, and
  injects the relay (`cluster/cdk8s/agentplane/launcher_relay.py`).

### Record machinery, and what D3 and D4 do with it

D1 and D2 keep all of it as it is. D4's costs are a relative judgment.

| Behavior                                                                                                                                                                                                                                                                                                                                          | D3                                                                                                                                                                              | D4                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Five CRD kinds with schema, defaults and CEL rules (idle-suspend consistency, name length, exactly one of Pod or VM), tested against a real apiserver (`crds/test_sandbox_admission.py`)                                                                                                                                                          | Same, with CRDs per plane                                                                                                                                                       | Port or drop each rule at the API edge; medium                                                                                                                                                                                   |
| Conflict-checked writes: exclusive create, `uid` and `resourceVersion` preconditions (`sandboxes/inventory.py`, the Dispatcher's wake), server-side apply with one field manager per reader on `PolicyBinding` status (`policy_sets.py`). The idle-suspend wake race relies on `resourceVersion` being object-wide (`sandbox_controller/idle.go`) | Same                                                                                                                                                                            | A version column; low                                                                                                                                                                                                            |
| Status subresource with its own RBAC verb, conditions with `observedGeneration`, a per-reader condition on each `PolicyBinding`                                                                                                                                                                                                                   | Same                                                                                                                                                                            | A separate write path and authorization for status; medium                                                                                                                                                                       |
| List-then-watch: one Python engine (`util/kubernetes_watch.py`) for the app, relisting on any failure and failing closed when stale; client-go informers and controller-runtime caches in the Go services (the Action Service, both controllers, the Gateway with the Dispatcher, egress)                                                         | Same, with a second client per plane                                                                                                                                            | A resumable watch with a "gone, relist" signal; high, the one hard piece, since Postgres watches here send a snapshot then changes and cannot resume ([API aggregation](api_aggregation_exploration.md))                         |
| Leases: `<sandbox>-runner` fences by `leaseTransitions`, names the holding Gateway replica and carries runner facts the controller reads; `dispatcher-<sandbox UID>` beside a Postgres fence; leader election for both controllers                                                                                                                | Move with the Sandbox family                                                                                                                                                    | Compare-and-set epoch with TTL and holder routing; medium. The Dispatcher's Postgres fence has no expiry, so it stops stale writers but cannot pick a live owner; `DISPATCHER_OWNERSHIP_IN_POSTGRES` (unscheduled) would add one |
| Ownership and deletion: the Sandbox owns every child, a finalizer holds deletion until the runner has quiesced, garbage collection removes the rest; the Gateway (`runner_gateway/gateway/sandboxes.go`) and the relay policy identify a Pod by its owner chain                                                                                   | Ownership does not cross apiservers (general Kubernetes behavior, not tried): the controller deletes each child kind itself, and the Pod-to-Sandbox check needs another binding | A sweeper per child kind and a held deletion; medium to high                                                                                                                                                                     |
| Provenance: the Flux label `kustomize.toolkit.fluxcd.io/name` marks git-owned `PolicyBinding`s and a Sandbox owner marks Sandbox-owned ones (`sandboxes/egress_views.py`); `managedFields` records writers                                                                                                                                        | Same                                                                                                                                                                            | A writer column; low to medium                                                                                                                                                                                                   |
| Clients: `kubectl`, Flux, `kubernetes_asyncio` in about 28 non-test Python files, client-go and controller-runtime in the Go services                                                                                                                                                                                                             | Kept, with a second kubeconfig                                                                                                                                                  | Lost                                                                                                                                                                                                                             |

D3's apiserver would be [kine](https://github.com/k3s-io/kine) on the CNPG cluster, which per its
README implements the etcd API subset the apiserver needs over SQLite, MySQL, Postgres, NATS or T4,
or its own etcd. A nodeless apiserver with the Agentplane CRDs already runs in tests
(`testing/kubernetes_apiserver.py`). Accepting the primary's tokens on it is webhook token
authentication to the primary, not tried, and its RBAC is a separate set of Roles. It is one more
control plane to run: certificates, HA, upgrades, backups, monitoring.

### Which kinds could move in D3

A kind stays on the primary when it owns primary objects, is owned by them, or protects them.

- **The Sandbox family** (`Sandbox`, its `PolicyBinding`, its Leases) moves together: the binding and
  Leases are owned only by the Sandbox, and the Policy Controller already keys native RoleBindings by
  binding UID in an annotation. The family grows with Sandbox count and holds the hot writes; its
  cost is its ownership of the workload children.
- **The catalog** (`PolicySet`, `EgressCredential`, `SandboxTemplate`, the static `PolicyBinding`s)
  owns nothing and is owned by nothing, so it moves cheaply, and gains nothing from moving except
  that a reader then watches one plane.
- **Never:** Pods, VMs, PVCs, Services, EndpointSlices, ServiceAccounts, Secrets, RBAC and admission.

The kinds with a reason to move are tied down by ownership; the kinds that move freely have no reason
to.

### GitOps glue for D3 and D4

Only the Flux-written kinds that move need glue. Presets need none: they are app settings
(`cluster/cdk8s/agentplane/app_settings.py`), and a Sandbox carries only a `preset` label. The shared
permission sets are `PolicySet`s: 39 in staging and 11 in testing, plus one in each delivery
manifest. Today Flux applies them with the rest of `agentplane-staging`, and the cluster-scoped CRDs
sit in `agentplane-crds` with `prune: false`, shared by staging and testing.

1. **Flux applies to the second plane.** `spec.kubeConfig` makes a Kustomization apply, health check,
   prune and delete on the cluster its kubeconfig names; its `secretRef` must be in the
   Kustomization's namespace, and with `serviceAccountName` Flux impersonates that account on the
   target ([Flux Kustomization reference](https://fluxcd.io/flux/components/kustomize/kustomizations/)).
   The typed wrapper has the field (`cluster/cdk8s/providers/flux`), `flux_kustomization` has no
   keyword for it yet (`cluster/cdk8s/flux.py`), and no manifest sets it. The reference does not say
   how `dependsOn` or SOPS decryption behave across clusters. Pairs with D3 moving the catalog.
2. **A mirror reconciler.** Flux keeps applying the catalog to the primary, which stays the reviewed
   source; a controller copies labelled objects to the second plane with server-side apply and
   prunes by label. The second plane is a disposable replica, at the cost of one more controller and
   a staleness window.
3. **Readers watch both planes.** The catalog stays on the primary and the Sandbox family goes to the
   second; each reader holds two sources and a freshness gate per source. Pairs with D3 moving only
   the Sandbox family.
4. **A git-to-API importer, for D4.** tofu-controller (`tf/gitops/`: Authentik providers, Forgejo
   tokens) is the precedent, with its state in CNPG; a new target means a provider or an apply job
   with its own prune rule.

### Size today

Read on staging on 2026-10-18 with `kubectl` as `claude-ai` in a build Sandbox, as list JSON
including `managedFields`:

| Kind              | Objects | Bytes   |
| ----------------- | ------- | ------- |
| `Sandbox`         | 3       | 16.7 KB |
| `SandboxTemplate` | 3       | 46.2 KB |
| `PolicySet`       | 40      | 91.7 KB |
| `PolicyBinding`   | 7       | 20.2 KB |
| `Lease`           | 9       | 11.9 KB |

That is about 190 KB in staging and about 50 KB in `agentplane-testing`. `claude-ai` cannot list
`EgressCredential`s; the generator renders 22 for staging. etcd itself was not read. The Leases are
the records written continuously. At that reading the three staging runner Leases had last renewed
17 to 25 hours earlier, and the three Dispatcher Leases every 10 s. Since #11035 a Dispatcher Lease
renews only while its Sandbox has queued or delivering work, so a Sandbox writes 0.1 Lease update
per second while it has work, about 0.07 more while its runner is connected, and none once retired;
arithmetic, not a measurement at scale.

### Keeping finished Sandboxes as records

The design agreed from this is in [Sandbox tags and History's Sandbox records](sandbox_tags.md).

The operator's knot (09:50): "when you delete a Sandbox CR, that deletes also all its history; if
you want to 'archive', that's some different operation that keeps the CR alive", and the question it
raises: "is it a good idea to keep around potentially all sandboxes I've ever ran if I made like 100
a day and I run agentplane for a year". That is 36,500 Sandboxes. Their bytes fit, 365 MB at the
10 KB above against etcd's default 2 GiB quota, but three costs grow with every Sandbox kept rather
than with the live ones:

- the apiserver's watch cache holds every object of a kind in memory;
- every service watching Sandboxes caches and relists all of them; a label selector keeps retired
  ones out of a watcher's cache, not out of the apiserver's;
- each keeps its node-local state PVC (`piraeus-hdd-local` in the checked-in templates).

Agentplane's own apiserver (D3) takes the first and the quota off the cluster's control plane. The
watchers and the PVCs cost the same, and kine is slower than etcd at large lists (not measured
here).

Kubernetes keeps live objects and bounds what has finished: terminated Pods are collected past
`--terminated-pod-gc-threshold` (12,500 by default), Events expire after `--event-ttl` (1 h), and
Jobs take `ttlSecondsAfterFinished`. Projects whose CRs record finished work keep them in a database
instead: [Tekton Results](https://tekton.dev/docs/results/) stores PipelineRuns and TaskRuns so the
cluster can delete them, and the
[Argo workflow archive](https://argo-workflows.readthedocs.io/en/latest/workflow-archive/) keeps
completed Workflows in Postgres or MySQL.

The operator's "archive" keeps the CR, which is D1's retiring. The precedent suggests a second kind
of archive, a move out of Kubernetes: the CR and state PVC go, History keeps the transcript and its
record of the Sandbox, and that record keeps what read grants need, the Sandbox's history reader
tags. Inbox tags go with the CR, since they do "not make sense to archive" (2026-10-20 00:21).
The history tags stay editable, since freezing them "would be a bit inconvenient" (10:24): once the
CR is gone History's copy is the only one. Editing them can wait, but a mistaken tag must stay
fixable, from the app (10:43), so no importer is needed. Until History has that write path, two
things keep it possible: the tags live once per archived Sandbox, never copied into each Session's
rows, and grants match them at read time, not at archive time. Retired Sandboxes, which can resume,
stay CRs and stay few. It is D2 with an explicit archive step. It needs no restore: the operator
would not ask for "given just history records and no Sandbox or PVC anymore, make a new Sandbox and
make it restore and continue that history" (09:53).

What each operation leaves:

|                                                                             | Retire                             | Archive         | Delete |
| --------------------------------------------------------------------------- | ---------------------------------- | --------------- | ------ |
| Sandbox CR and children (ServiceAccount, control Service, bindings, Leases) | Kept, stopped                      | Gone            | Gone   |
| State and workspace PVCs                                                    | Kept; the workspace can be dropped | Gone            | Gone   |
| Dispatcher queue rows                                                       | Kept                               | Gone            | Gone   |
| History transcript and app Thread                                           | Kept                               | Kept, read-only | Gone   |
| History's record of the Sandbox (namespace, name, UID, history tags)        | Kept                               | Kept            | Purged |
| Can run again                                                               | Yes                                | No              | No     |

Deleting a Sandbox today is an archive without History's record of the Sandbox. The operator
(23:24): "history's record of a Sandbox would be an entity in History Service, and Sandbox CR could
stop existing without the History Service records disappearing", but "I'm not sure if sandbox
controller should be involved". It need not be: History already records each Session's writer
namespace and name on the first push ([History
README](../history_service/README.md#which-sandbox-wrote-a-session)) and lacks only the UID and
tags, and a purge can be a History call the app makes after removing the CR; the operator deferred
purging (23:33). The operator keeps two kinds of tag apart (2026-10-20 00:21): "recipient tags" and
"history reader tags" are "permission gates for different services", since renamed inbox tags and
history tags after the service each gates (04:04). Inbox tags stay on the CR,
since "history should not be required for the tags feature": sending is checked against them with no
History involved (23:27), and History never looks at them. History tags are History's own gate. They
are a second, separately named set on the CR, the operator's pick (2026-10-20 00:32), so git and the
app set both when they create a Sandbox. History watches that set and keeps the last values it saw
once the CR goes, with no finalizer. The only gap is a history-tag edit made while History is down
just before a delete, which an app retag fixes. The operator agreed History may read Sandbox tags
(23:29), which relaxes "never routes to, reads or manages Sandboxes" ([service
boundaries](../docs/service_boundaries.md)) to reading them; the rule changes with the change that
adds the watch. Setting history tags only through History would have kept the rule and given live and
archived Sandboxes one write path, but git would have no way to set them.

The operator also raised (23:48) making "a sandbox's historical record" "a separate k8s object with
its own finalizer that might do purging". It costs far less than keeping Sandboxes: no PVCs, about
1 KB each (estimated, about 37 MB a year at 100 a day), watched only by History and the app. Git
could set history tags on it even for a Sandbox the app created, as it could on a membership CR, and
the finalizer is the Kubernetes form of the deferred purge. Against it, records grow in etcd again,
which reopens where they are stored (D3), and History takes on a CRD and a reconcile loop. The
operator leans away from it (2026-10-20 00:12): "since history manages all the historical bulk in
the database, it should probably not split its storage", so History keeps "its own records and a
reconciler if needed between those and live sandboxes", which the history-tag watch above is.

## Right under every direction

Candidates; each still needs the operator's go.

- **Record the writing Sandbox's UID in History and check it on every push.** Landed (#11835), with
  the global Session ID kept as the key. D1 deletes by it, D2 keys grants by it, and D3 and D4 still
  identify a Sandbox by it. History's record of the Sandbox and the history-tag watch remain
  (`HISTORY_SANDBOX_RECORD`).
- **Inbox tags on the Sandbox.** The operator (09:58): a collection as "something like a tag
  on one or more Sandboxes that's just a string, without a UID identity or a k8s object backing it,
  no Membership objects"; inbox tags and history tags are separate (2026-10-20 00:21). An
  inbox tag lives and dies with its Sandbox, and a rule matches it the same way in every
  direction, as it does direct and `allSandboxes` targets; membership then picks no direction, and a
  recreated Sandbox has only the tags its manifest or preset gives it. History tags, also on the CR
  (2026-10-20 00:32), follow the same rules below. Against the membership CRs, git cannot cleanly tag a Sandbox the app
  created: Flux would own the whole object, delete it when the entry leaves git, and try to recreate
  it from a partial manifest after an archive (not tried). On a Sandbox git creates, git and the app
  can each own different tags, since server-side apply tracks each entry of a set-typed list separately and Flux
  takes back only fields edited with `kubectl` (kustomize-controller's field-manager cleanup); a tag
  git declares is reverted if changed live. Setting a tag is a grant, and RBAC on Sandbox writes is
  its only gate, with no admission rule (2026-10-20 01:29): whoever can patch a Sandbox can already
  set its `spec.policySets`, which grants at least as much. Flux and the app are the writers
  (01:17), as the
  [agent communication plan](multiagent_communication.md#target-collections-and-launch-enrollment)
  now records.
  Both tag kinds are spec lists declared `x-kubernetes-list-type: set`, like `spec.policySets`
  (2026-10-20 00:36): the CRD validates them and requesters keep writing spec, and a set keeps the
  split ownership that a plain list, owned whole, would lose. Labels would let `kubectl` and watchers
  select on a tag, which nothing needs while History watches every Sandbox. This takes the
  membership CR out of #10987 ([design](sandbox_tags.md)).
- **Drop a Sandbox's workspace without deleting the Sandbox.** Deferred by the operator, "not needed
  for this plan" (23:32). The Sandbox owns `<sandbox>-workspace`, and a missing one blocks the next
  start with `VolumeLayoutMismatch` (`sandbox_controller/workspace.go`), so deleting the Sandbox is
  the only way to free its workspace, and that takes the state PVC with the runner's journals and
  native resume files. A request in the Sandbox's spec, acted on while it is suspended, would delete
  the claim and clear `status.volumes.workspace`; the next start creates a fresh claim, so an agent
  such as Haku can re-clone and resume. The operator: dropping the workspace "should still in
  principle let a harness start and continue" (09:50). The catch: a Session's working directory is
  under the workspace (`/state/workspaces/{session_id}/...` in the presets), and its one-time setup
  script, which creates it, does not rerun under the same Session ID ([app
  README](../app/README.md)). D1 and D3 finish a Sandbox this way; D2 and D4 can keep one without
  its storage. ## Configurable within sane options The operator: "one way to make things no-regret
  might also be to make the 'what lives where' configurable, or configurable within sane options"
  (02:19).
- **Which apiserver holds the records.** The outer cluster and an Agentplane-owned apiserver speak the
  same protocol, so each service could take a records connection apart from its cluster connection,
  both pointing at the cluster by default. Two things must first stop assuming one apiserver: the
  Sandbox Controller deletes a Sandbox's children itself instead of leaving them to garbage
  collection, and the Gateway (`runner_gateway/gateway/sandboxes.go`) and the launcher relay policy
  (`cluster/cdk8s/agentplane/launcher_relay.py`) tie a Pod to its Sandbox by something other than an
  owner reference. D3 then becomes a deployment setting that testing can try first.
- **Whether history outlives its Sandbox.** The delete cascade can be a setting, per Sandbox or per
  environment. Membership and grants are the catch: Sandbox-owned CRs work only when history dies
  with the Sandbox, while rows keyed by Sandbox UID, or history tags that History keeps once a
  Sandbox is archived, work under both settings.
- **Not a setting: database-backed records (D4).** It is a second backend for every record; offering
  both means building both.

Each setting is another mode to test and keep working, so configurability stays at these two.

## Moves that pick a direction

Held until one is chosen:

- deleting history with its Sandbox (D1), or archive grants apart from live grants (D2);
- a retired state that watchers treat specially (D1, D3);
- the History re-key by Sandbox UID (D2);
- any second-plane or records-service work (D3, D4).

## Experiments that would narrow it

- **Agentplane's share of the cluster's control plane.** Read Agentplane's object counts and request
  rates from the staging apiserver's metrics, with a read-only RBAC PR first if `claude-ai` cannot.
  Separates D1 from D3 at scale; the [size reading](#size-today) did not read etcd.
- **A nodeless apiserver on Postgres.** k3s, already pinned for the system tests' stack (`system_tests/k3s`), can keep its datastore in
  Postgres through kine. Install the Agentplane CRDs and try what D3 leaves untried: token
  authentication through a primary, the watch cache turned off for custom resources, and a Flux
  `spec.kubeConfig` apply.
- **The price of a retired Sandbox.** On `agentplane-testing`, stop one with
  `spec.wakeOnSend: false`, count its records and watcher cache entries, weigh its state PVC, and
  confirm that neither of its Leases renews. Checks D1's arithmetic.
- **D4's git glue without a provider of our own.** Whether a generic REST Terraform provider can
  drive a records API. Not tried.

## The map today

### Kubernetes records

- **Sandbox.** A `Sandbox` CR. The app writes desired state, the Sandbox Controller writes status,
  and the Dispatcher patches the wake fields. Operator: deletes a Sandbox when done and wants its
  history kept (2026-10-18 23:29), floated suspending instead of deleting, offered deleting the
  history with the Sandbox (2026-10-19 02:04), and wants the workspace disposable on its own while
  the state PVC and metadata stay with the history (09:42).
- **Sandbox children** (Pod or VM, state and workspace PVCs, ServiceAccount, `<sandbox>-control`
  Service, the Sandbox's own `PolicyBinding`). Owned by the Sandbox and removed with it; both PVCs
  survive suspension.
- **Grants and catalog** (`PolicySet`, static `PolicyBinding`s, `SandboxTemplate`,
  `EgressCredential`). Authored in git and applied by Flux; the app writes runtime grants as bindings
  owned by the Sandbox. Operator: wants the Flux-managed shared permission sets kept gitops-driven,
  and floated a second control plane "if it came to it" (2026-10-18 22:54, 23:02).
- **Leases.** `dispatcher-<sandbox UID>`, renewed every 10 s (`LeaseRenewInterval`) while its
  Sandbox has queued or delivering work and left to lapse otherwise (#11035), and `<sandbox>-runner`
  from the Gateway while a runner is connected. The Dispatcher also keeps a Postgres
  `sandbox_ownership` generation fence per Sandbox, with no expiry
  ([size today](#size-today)).

### Databases and runner storage

- **Session history** (raw Events, observations, settlements, the Thread fold). The History database
  `sandbox_service`, which outlives the Sandbox; four fold tables replicate through Electric to the
  browser. Operator: agent traces and big objects should not go into the control-plane etcd
  (2026-10-18 22:39).
- **Which Sandbox wrote a Session.** History records the namespace, name, and controller-owned Sandbox UID
  from the Sandbox's own ServiceAccount, which its runner authenticates as; it checks that UID on every
  push and keeps the globally unique Session ID
  key ([History README](../history_service/README.md#which-sandbox-wrote-a-session)).
- **Thread** (the app's link and metadata for a Session). The app database, keyed by Session ID with
  no Sandbox column. The app carries the recorded History UID in Thread views, filters Sandbox pages by
  UID, and passes it to the Dispatcher for live actions.
- **Commands and Session openings.** The Dispatcher database (`dispatch`, `session_opening`,
  `wake_attempt`, `sandbox_ownership`), every row carrying the Sandbox UID and ended when the Sandbox
  is gone. Proposed: `DISPATCHER_PERSISTENCE_MODEL_DESIGN` compares the durable queue with direct
  admission. Operator: the Dispatcher keeps no Session-to-Sandbox registry (2026-10-17 03:53, 04:12).
- **Inbox entries, notices, subscriptions.** The Notification database; entries survive their
  inbox's retirement when the Sandbox is gone. Proposed: peer messages and sender receipts join this
  store, and the service split stays unscheduled. Operator: wants receipts that tell "was this acked"
  from "did a notice cover this" (2026-10-17 03:30), and may revisit the service with later
  "refactors/splits/reshuffles" (04:18).
- **Action requests, Decisions, connections** (Actions database), **egress admission decisions**
  (egress database), **launch presets** (app configuration, with a label and a compact annotation on
  the Sandbox). Nothing proposed.
- **Runner journal.** One SQLite file per Session on the Sandbox's state volume; History is the
  retained copy once the runner pushes it. Nothing proposed.

### Not stored yet

- **Sandbox tags** (which Sandboxes a grant's collection covers). Agreed, not implemented: inbox
  and history tags as set-typed lists on the Sandbox, with History keeping the history tags in its own
  record of each Sandbox, keyed by Sandbox UID ([design](sandbox_tags.md)). They replace #10987's
  `SandboxCollectionMembership` CRs and #10990's membership rows.
- **Session-access grants** (who may send to, or read, which Sandbox's Sessions). History's
  configured `reader_accounts` are the only gate on reads, and the Dispatcher trusts the app and
  Notification to authorize their users. Proposed: `session_inbox_access` rules for sends and a rule
  type of their own for reads, in `PolicySet`s, assigned by
  `PolicyBinding`s and enforced by the owning service
  ([plan](multiagent_communication.md#tentative-implementation-direction)). Operator: sending and
  reading transcripts are "really separate capabilities" (2026-10-18 22:46); archive permissions maybe
  "in a possibly separate universe" from live objects (22:31, tentative); read decisions held
  (2026-10-17 10:56).

## What the operator has said

| Date (UTC)       | Operator                                                                                                                      | Consequence                                                     |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 2026-10-17 03:30 | Sender receipts that tell "was this acked" from "did a notice cover this"                                                     | Receipts in v1                                                  |
| 2026-10-17 03:41 | OAuth callers get an SA through an Actions connection and send from an actions-sandbox                                        | No separate OAuth sender path                                   |
| 2026-10-17 04:12 | Messages come with the Sandbox; wake-on-send does not look it up from a Session                                               | No Session-to-Sandbox registry                                  |
| 2026-10-17 05:58 | `allSandboxes` in v1, "under some kind of scope of the controller"                                                            | Bounded to the PolicySet's namespace                            |
| 2026-10-17 10:56 | Keep read-permission decisions held "for now"                                                                                 | Read grants stay held                                           |
| 2026-10-18 22:39 | Agent traces and archives should not go into the control-plane etcd                                                           | Per-Session data stays out of Kubernetes records                |
| 2026-10-18 22:46 | Sending and reading transcripts are "really separate capabilities we probably don't need to bundle"                           | The send rule carries sending only                              |
| 2026-10-18 23:19 | No need to name a different namespace                                                                                         | Direct targets are same-namespace                               |
| 2026-10-18 23:33 | Collections keep covering Sandboxes no longer alive                                                                           | Rules out live-members-only collections for reads               |
| 2026-10-19 01:37 | Python and Go ports can be sequenced in any order relative to the other things                                                | Ports are not part of this map                                  |
| 2026-10-19 02:14 | No definite pick yet on where data and objects live                                                                           | No direction chosen                                             |
| 2026-10-19 09:42 | The workspace PVC "should be disposable without history disappearing"                                                         | A workspace drop is right under every direction                 |
| 2026-10-19 09:50 | Dropping the workspace "should still in principle let a harness start and continue"                                           | The drop keeps the Sandbox resumable                            |
| 2026-10-19 09:53 | No restoring a Sandbox from history records alone                                                                             | An archive need not be resumable                                |
| 2026-10-19 10:24 | Freezing an archived Sandbox's collections "would be a bit inconvenient"                                                      | Archived Sandboxes' tags stay editable                          |
| 2026-10-19 10:43 | Retagging an archived Sandbox can wait, but must stay possible, from the app                                                  | No importer; tags stored once, matched at read time             |
| 2026-10-19 23:27 | Tags must not need History: sending checks them without it                                                                    | Tags stay on the Sandbox CR                                     |
| 2026-10-19 23:29 | History may read Sandbox tags                                                                                                 | History mirrors history tags for read grants                    |
| 2026-10-19 23:32 | The workspace drop "can be deferred, not needed for this plan"                                                                | Deferred                                                        |
| 2026-10-19 23:33 | No history purge operation yet                                                                                                | Purge deferred; history outlives its Sandbox as documented      |
| 2026-10-19 23:48 | Tags could split into sendability and readability tags; a Sandbox's record could be its own k8s object whose finalizer purges | Split adopted at 00:21; record object declined at 00:12         |
| 2026-10-20 00:12 | History keeps its own records in its database, with a reconciler against live Sandboxes if needed                             | No Kubernetes record object; the tag watch is that reconciler   |
| 2026-10-20 00:21 | Recipient tags and history reader tags are separate gates for different services; recipient tags are not archived             | Two tag sets; only history tags outlive the CR                  |
| 2026-10-20 00:32 | History tags as a second set on the Sandbox CR that History watches                                                           | History's watch reads only history tags; git can declare them   |
| 2026-10-20 00:36 | Tags as set-typed spec lists rather than labels                                                                               | `spec.inboxTags` and `spec.historyTags`, like `spec.policySets` |
| 2026-10-20 01:17 | Only Flux and the app may set tags                                                                                            | Flux and the app are the tag writers                            |
| 2026-10-20 01:29 | No webhook for tag writes; Sandbox RBAC is enough                                                                             | No admission rule                                               |
| 2026-10-20 04:04 | Name each tag kind after its service: "inbox tags" and "history tags"                                                         | `spec.inboxTags` and `spec.historyTags`                         |
| 2026-10-20 04:05 | Consider "transcript tags", with History renamed to a transcript service                                                      | `HISTORY_SERVICE_RENAME` on the task DAG                        |
| 2026-10-21 03:36 | Accepted the 30-day payload expiry, the cap of 128 unacknowledged messages per sender and inbox, and the idempotency key      | The peer message contract is agreed                             |
| 2026-10-21 03:43 | Send rule `session_inbox_access` with `send_message`; reads take "a different kind of rule", like their tags                  | Reads get a rule type of their own                              |

Tentative words, not decisions: archive permissions "in a possibly separate universe" (2026-10-18
22:31), suspending instead of deleting Sandboxes as "one consistent way" (23:29), a second control
plane "if it came to it" (22:54), membership in a database with a reconciler (2026-10-19 01:10),
deleting a Sandbox's history with it as "one other consistent option" (02:04), the state PVC and
metadata as parts "I do not really need to dispose independently, I think" (09:42), "one valid way
to cut the knot might be" deleting history with the Sandbox CR and archiving as "some different
operation that keeps the CR alive" (09:50), and "I guess I could treat a 'collection' as just
something like a tag" (09:58).

## Assumed by a lane and never confirmed

| Assumption                                         | Where it lives         | What the operator said                                |
| -------------------------------------------------- | ---------------------- | ----------------------------------------------------- |
| Records stay in the outer cluster's Kubernetes API | #10990 "Take" (closed) | Treated as open (2026-10-19 01:58); D1 and D2 keep it |

## Parked

[API aggregation](api_aggregation_exploration.md) is held by the operator. Nothing waits on it.
