# Flux Kustomization policy

When a component gets its own Flux `Kustomization`, and what `dependsOn`, `wait`
and `healthChecks` may be used to express.

## What a Kustomization is

A unit of **ownership and blast radius**, not a unit of ordering. Everything it
lists is applied together, pruned together, suspended together, and orphaned
together.

Its `Ready` means **the manifests were applied**, not _the system works_.
Workload health is Prometheus's job — `KubePodNotReady`,
`KubeDeploymentReplicasMismatch`, `KubeContainerWaiting`, `KubeJobFailed` all
fire independently of Flux — and gatus's for user-facing endpoints. A
Kustomization that goes Ready before its pods do loses no signal.

## Dependency classes

`dependsOn` is a **hard gate**: the dependent is not applied at all until the
prerequisite is Ready, and the block propagates to everything downstream of it.
Two kinds of prerequisite get confused with each other.

**Class 1 — admission-time.** The apply is _rejected_ without the prerequisite:
the CRD is not established, or an admission webhook with `failurePolicy: Fail`
is not answering. This does not self-heal within a reconcile and must be a
`dependsOn`.

**Class 2 — runtime.** The object applies fine and converges once the
prerequisite arrives: a Secret it mounts, a StorageClass its PVC binds to, a
registry credential it pulls with, a database it connects to, a Gateway that
routes to it. Kubernetes retries all of these on its own. Writing them as
`dependsOn` trades a self-healing transient for a permanent wedge that
propagates, and turns one fault into one alert per downstream node.

### What an edge actually buys here

kustomize-controller compares a dependency's `status.lastAppliedRevision`
against the current source revision **only** when the dependency's `sourceRef`
has the same kind, name and namespace as the dependent's
([`checkDependencies`](https://github.com/fluxcd/kustomize-controller/blob/main/internal/controller/kustomization_controller.go)).
Since the ArtifactGenerator migration each component reconciles from its own
`ExternalArtifact`, so 712 of our 713 edges name a different source, skip that
check, and gate on `Ready` alone.

Artifacts are content-addressed and source-watcher skips a rebuild when the
copied subtree is unchanged (`ExternalArtifact/… is up to date`), so a commit
wakes only the components it touched. That is the point of the generators, and
it is what the rest of this follows from:

- **A commit touching one component.** Its dependencies are Ready and unchanged,
  so every edge passes immediately. Nothing is ordered because nothing else
  moved.
- **A commit touching two Kustomizations of the same component.** Both artifacts
  change. The dependent sees the prerequisite `Ready` — at its _old_ revision,
  which `Ready` does not distinguish — and applies. The two race. This is
  precisely the case the layering exists to order, and the case it does not
  order. Upstream has known since
  [flux2#293](https://github.com/fluxcd/flux2/discussions/293) and has not fixed
  the general case; rule 6 is how to buy the ordering for real when it matters.
- **Bootstrap and disaster recovery.** Nothing is Ready, so every edge gates and
  the graph serializes for real. This is the only state in which the depth is
  paid, and it is the state this cluster's Primary Directive is about.
- **Any state, whenever something is broken.** The edge propagates the failure
  and multiplies the alerts.

So an edge buys bootstrap ordering and costs failure propagation. In steady
state it is a no-op except when it is a block. Delete accordingly: the question
for any candidate edge is **"does `bazel run //cluster:bootstrap` still
converge without it"**, not "is it tidier with it".

## Rules

### 1. Depend on what makes the API accept the object

A Kustomization applying kind `K` must transitively depend on whatever makes the
API server accept `K`: the `CustomResourceDefinition`, and — only where one
exists — the `failurePolicy: Fail` admission webhook that gates it, which means
the operator Deployment behind it.

Which one it is, is a fact about the operator, not a default. Measured
2026-09-16: ESO (`externalsecrets`, `secretstores`), CNPG, cert-manager,
Kyverno, KubeVirt `virt-api` and CDI register `Fail` webhooks — their consumers
need the operator up. Prometheus-operator's two webhooks are
`failurePolicy: Ignore` and cover only `prometheusrules`/`alertmanagerconfigs`;
SeaweedFS, tofu-controller and the Valkey operator register none at all — their
CRs need nothing but the CRD.

Where no webhook gates the kind, the edge points at a CRDs-only Kustomization
(`<operator>-crds`: no `wait`, no `healthChecks`, Ready in milliseconds, has no
runtime to fail), never at the operator's HelmRelease. A `ServiceMonitor` needs
the `ServiceMonitor` CRD; it does not need Prometheus running, and must not be
blocked when Prometheus is down.

### 2. Every other `dependsOn` edge is registered with a reason

A class-2 edge is allowed only when it is listed in `_ORDERING_EXCEPTIONS`
(<../validation/dependencies.py>) with a one-line reason of one of these kinds:

- **bootstrap-never-converges** — without it `bazel run //cluster:bootstrap`
  does not finish, as opposed to finishing after some retries: the CNI, the SOPS
  age key, the Flux source itself.
- **destructive-if-out-of-order** — applying B before A is not merely late but
  corrupting: a schema migration that must precede the writer, a PVC or bucket
  ownership handoff. An edge alone does not deliver this — see rule 6.

"It needs the secret", "it needs the namespace", "it needs the database", "it
should come after the app" are not reasons. Those converge on their own.

### 3. One Kustomization per component

A component gets exactly one, unless one of these applies — each of which is a
lifecycle boundary, not an ordering one:

- **a. API establishment** — it ships CRDs others depend on (rule 1).
- **b. Persistent-state ownership** — it owns a CNPG `Cluster`, a PVC, a
  SeaweedFS `Bucket`/`S3Identity`, or a `Terraform` CR holding tfstate. A prune
  reaches these; keeping them in a narrow, rarely-renamed Kustomization with
  `prune: false` means a rename of the app cannot destroy them. See
  <../AGENTS.md> § Migrating stateful Flux Kustomizations.
- **c. Different source or decryption identity** — another `GitRepository`, or
  an age key the rest of the component's path must not be decryptable with.
- **d. Independent suspend** — an operator must be able to suspend this alone
  mid-incident while the rest keeps reconciling. A `Terraform` CR that mutates
  an external system usually qualifies; state the reason in the
  `description` annotation.
- **e. Different cadence**, with the reason stated — e.g. `1h` on a `Terraform`
  CR that spends external API quota.

Everything else in the component — the Namespace, the ExternalSecrets, the
HelmRelease, the ServiceMonitor, the HTTPRoute, the NetworkPolicies, the
RoleBindings — goes in the one Kustomization.

Within a single Kustomization this is safe by construction, not by luck:
[`ssa.ApplyAllStaged`](https://pkg.go.dev/github.com/fluxcd/pkg/ssa) "extracts
the cluster and class definitions, applies them with ApplyAll, waits for them to
become ready, then it applies all the other objects" — precisely "when the given
objects have a mix of custom resource definition and custom resources, or a mix
of namespace definitions with namespaced objects". `CustomResourceDefinition`,
`Namespace`, `ClusterRole`, `RuntimeClass` and `PriorityClass` go in that first
stage; webhook configurations go last. Everything between applies at once, and
pods wait for their Secrets.

Merging does coarsen the artifact: one component, one artifact, so editing its
ServiceMonitor now re-reconciles the whole component instead of one row of it.
That is affordable exactly because of rule 4 — a re-apply of unchanged manifests
is a few server-side applies and no health-check wait. Rules 3 and 4 are one
decision; taking rule 3 without rule 4 makes every small edit wait on the
component's slowest pod.

### 4. `wait` and `healthChecks` gate, or they are absent

Set `wait: true` or `healthChecks` only where another Kustomization depends on
this one through a rule-1 or registered rule-2 edge. Otherwise omit both.

An ungated `wait` buys a red light that `FluxKustomizationNotReady` and
`KubePodNotReady` already give, and charges for it: one of kustomize-controller's
eight worker slots, parked for the full `timeout` (5–20m here), on every
reconcile of a sick component.

### 5. Namespaces are owned centrally

Application Namespaces live in one `namespaces` Kustomization with `prune:
false`, not in the components that use them. Two reasons, in order: a Namespace
inside a component's Kustomization is a prune target, and pruning a Namespace
cascade-deletes everything in it including PVCs; and a Namespace that several of
a component's Kustomizations need has no natural owner among them.

Consumers do not `dependsOn` it. An apply into a missing namespace fails and
retries at `retryInterval`, which converges at bootstrap without an edge.

### 6. Real ordering needs a shared artifact, not an edge

`dependsOn` across two different `ExternalArtifact`s gates on `Ready` and
nothing else, so it cannot express "apply A's new manifests before B's new
manifests" (above). Where that ordering genuinely is the requirement — a schema
migration before the writer that reads the new columns — put **both paths in one
artifact** and point both Kustomizations' `sourceRef` at it. The `sourceRef`
then matches, `checkDependencies` engages the revision comparison, and the
dependent waits for the prerequisite to have applied _that_ revision.

Nine artifacts already copy more than one path, and
`check_cross_namespace_references` already validates that a consumer's
`spec.path` lies inside what its artifact carries, so the shape needs no new
machinery.

This is the only construction here that delivers ordered updates. A
`destructive-if-out-of-order` entry in rule 2 that is not built this way is
documenting an intent the cluster does not implement.

## Measurements

Taken 2026-09-16 against the live cluster, 294 Kustomizations in Git and 278
applied.

- `dependsOn` edges: 713, of which **78** are class-1.
- Longest chain: **14**. With class-1 edges alone, **3**.
- Kustomizations managing two objects or fewer: 130 of 278 (mean inventory 6.6).
- `wait: true` or `healthChecks`: 212, of which **66** unsuspended have no
  dependent.
- Not-ready, unsuspended: 30 — 17 own faults, **13 `DependencyNotReady`**.
- Edges whose dependency shares the dependent's `sourceRef`, and so are
  revision-checked rather than `Ready`-checked: **1** of 713.
- `--requeue-dependency=30s`, so each level costs up to 30s after its dependency
  goes Ready — around 6 minutes across today's 14-deep chain, before any of the
  work itself. Reported in the field as ~50s per level
  ([flux2#5403](https://github.com/fluxcd/flux2/discussions/5403)).

The depth-3 figure is computed with class-1 edges pointing at _operators_, so it
does not depend on rule 1's CRD split landing. It is a floor — registered rule-2
exceptions add a few edges back. It is not 14.

Two live examples of the pathology, both from that snapshot:

- One `Job` with an immutable-field error (`clickhouse-schema`) blocked
  `aiquota` → `public-coder-agent-proxy` → `public-coder-agent-app` and
  `-devbox`. Five `FluxKustomizationNotReady` alerts, one fault.
- `forgejo-images` — a registry pull credential — is depended on by 48
  Kustomizations, and itself waits on a `Terraform` health check behind
  `forgejo`, `tofu-controller` and `tofu-state-db`. A Forgejo outage is
  therefore a cluster-wide deploy freeze, to spare 48 workloads an
  `ImagePullBackOff` they would recover from unaided.

## Rejected alternatives

**Layer the component: CRDs → secrets → app, each its own Kustomization with
`dependsOn` on the last.** The repo's law until this document, and the layout
[Flux's own guidance](https://fluxcd.io/flux/guides/repository-structure/) and
its maintainers recommend — that recommendation is about infrastructure-vs-apps
at cluster granularity, and it is applied here per component, which is where it
stops paying. It buys a bootstrap ordering that Kubernetes mostly provides by
retrying and no update ordering at all (above), and charges a control object, a
generated artifact, a root-kustomization entry and a graph node per layer — then
propagates every layer's failure downstream. The measurements above are what it
costs.

**Keep the split, drop the `dependsOn`.** Halves the damage and keeps all the
bookkeeping: a component still costs four files in three places to extend, and
`flux get ks` still lists four rows per app.

**One Kustomization for everything, no splits at all.** Loses rule 3b: a single
prune mistake reaches every PVC and database in the cluster at once.

**Gate on the operator being Healthy rather than on its CRDs** (rule 1's
alternative). Correct for the handful of operators with `failurePolicy: Fail`
webhooks, wrong for the rest, and it is what makes chains deep — an app
depending on an operator inherits that operator's whole upstream chain.
