# cdk8s generators

Design and conversion mechanics: <../docs/cdk8s.md>. Which directories are generated and
how to regenerate: `cluster/AGENTS.md` § Generated manifests.

## Boundaries

- **The vocabulary is Kubernetes, cdk8s, Flux and Kustomize objects, plus plain Python
  values.** Nothing here introduces a concept those do not have: no marker annotation,
  no "provides" declaration, no record type standing in for an object, no registry, no
  convention a reader must learn on top of the objects' own fields. When a change seems
  to need one, stop and ask; the operator approves the design before it is built. The
  same applies to a rule or check that would only work with such a marker.
- **Construction runs forward** (§ The Flux graph): inputs are values or constructs
  built earlier, and every fact a node depends on is in its signature.
- **Stateful data is never destroyed by a change here.** Databases, PersistentVolumes
  and anything a person authored survive every conversion and restructuring; caches may
  be dropped. A Kustomization that owns PVCs carries `deletionPolicy: Orphan`, and an
  ownership change goes through § Restructuring. The repository-level rule that deployed
  state is disposable covers schemas and wire formats, not volumes.
- **Escape hatches stay.** Every Flux and Kustomize field remains expressible, so an
  incident `suspend`, the two-step ownership move, a VolSync restore, or a one-off
  hand-written sibling file is a plain edit and not a fight with the generator. A shape
  is made unrepresentable only where the operator asked for that; by default the safe
  procedure is documented and possible, not enforced.

## Shape of a generator

Model with constructs, deploy with one props object per environment.

- **Keep shared helpers and single-module components at the package root.** A component
  with several related modules gets a named subpackage (`agentplane/`, `haku/`,
  `clickhouse/`, `litellm/`); don't add a directory for one file or an `agents/` layer.
  Use descriptive module and Bazel target names (`agentplane/actions.py`,
  `litellm/proxy.py`) without a repeated `_constructs` suffix.

- **One `Environment` per deployed namespace** (`agentplane/environment.py`;
  `agentplane/staging.py` and `agentplane/testing.py` each hold one frozen instance).
  Every construct takes the whole environment and reads what it needs
  (`env.namespace`, `env.replicas.count`, `env.egress.ca_secret_name`), so a value two
  constructs share is written once. The props object holds data only — never a callable
  field for "and also build this".
- **One chart function per environment** (`agentplane/staging.py`'s and `testing.py`'s
  `chart(app)`), shared by `generate_manifests.py` (synth and write) and the test
  fixtures (`Testing.synth` in memory). It calls the shared `agentplane/chart.py`'s
  `environment_chart(app, env)` and adds its environment-only objects to the chart that
  returns — never `if env.namespace == ...` inside a construct, and never a hook the
  shared function calls back into. Objects added after it returns are still checked:
  `add_fleet_rules` registers a synth-time validation rather than reading the chart when
  called. `haku/charts.py`'s per-directory `*_chart(app)` is the same shape. The entry
  point holds no environment data.
- **References, not names.** `Service(selector=deployment)`,
  `Volume.from_config_map(config_map)`, `Role.from_role_name(...)`; a network rule
  targets a workload through the constant the owning module exports
  (`cilium.endpoint_labels(namespace, egress.NAME)`), never the
  string spelled again.
- **The service's `Settings` is its deployment contract.** Flags, env vars and settings
  files are rendered through the binary's pydantic-settings model
  (`util/settings_contract.py`: `cli_args`, `env_name`, `settings_file`,
  `checked_value`), so a renamed field fails at synth. The code package owns `Settings`
  and its `CONFIG_FILE_ENV`; `cluster/cdk8s` owns where and how it runs. Nothing under
  `x/` or `haku/` imports `cluster/`. A project's own `deploy/` may hold a props-driven
  construct (tested with synthetic props); the cluster's instantiation of it lives here.
- **One helper per repeated shape.** When the same dozen generated-struct lines appear
  twice, name the shape once: `cilium.py` (`ingress_from_gateway`, `egress_to`,
  `egress_to_fqdns`, `egress_via_gateway`, `dns_egress`, `fqdn_fence`,
  `deny_all_egress`, ...), `gateway.https_route`, `probes.http_probe`,
  `agentplane/migrate_container.py`, `agentplane/node_scheduling.py`,
  `pod_spec_patches.py`, `api_resource.custom_resource`. Parameterize the variation the
  call sites have (SNI list, listener, timeout), not variation nobody uses.
- **A value that feeds two artifacts lives once.** The web-push hosts feed both the
  Action Service allowlist and its egress rule from one tuple in `staging.py`. When
  two artifacts must agree, derive both from one value; never write a test that reads
  both.
- **New Kustomization directories default to cdk8s** when they hold more than a
  `HelmRelease` plus values.

## The Flux graph

Every Flux `Kustomization` is one function in one shared chart, and its dependencies are
its parameters. `generate_manifests.py` is the topological order, written out by hand.

- **A node is `name(chart, artifact, *predecessors: Kustomization) -> Kustomization`.** It
  returns `flux.flux_kustomization(chart, name, artifact, ...)`, which derives `sourceRef`
  and `path` from the artifact and applies our defaults (listed once, in its docstring);
  the node passes only the `KustomizationSpec` fields that differ, as keywords of the
  same names and types. The entry point builds the artifact
  (`artifact_generators.artifact(name, directory, *shared_bases)`) just before the call,
  and passes every artifact to `write_artifact_generators` last; a parked node's
  artifact is left out, since nothing packages a suspended directory. A node sourcing a
  `GitRepository` directly takes no artifact and passes that `sourceRef` and a `path`
  instead. `dependsOn` is
  `flux_kustomization_depends_on_many(predecessor, ...)`, which reads name and namespace
  off the constructs it is handed; the entry carries an explicit `namespace` for that
  reason. The predecessor is a value the caller already built, never a string, a
  module-level lookup, or something resolved later.
- **The entry point calls the nodes in dependency order** and passes each result as a
  local to the nodes that need it. A wrong order is a `NameError` at synth; a cycle
  cannot be written. Never add a registry, a sorter, a lazy reference, a class or record
  that "describes" a node, or a module that builds nodes on import.
- **A resource chart joins the graph in three steps, in this order**: build the resource
  chart (`staging.chart(app)`, `haku.charts.console_chart(app)`); then its Kustomization
  node takes the _values_ derived from that chart, computed at the join in
  `generate_manifests.py` (`health_checks=flux.health_checks(chart, kinds)`), plus its
  predecessor Kustomizations; then dependents take the returned Kustomization. The node
  never takes the `Chart` itself: a Kustomization reads the rendered directory at
  runtime, not a construct, and a node whose inputs are plain values shows every fact
  that crosses in its signature and can be tested with hand-supplied values, without
  importing the workload modules. A second fact that needs to cross is a second
  parameter, and that is the review signal. Chart before Kustomization before
  dependents; a Kustomization never builds the chart it describes.
- **Each object is built from what it reads at runtime, never from what reads it.** A
  Kustomization is built from its artifact, its path and its predecessors; an artifact
  from its directory; the `ArtifactGenerator` from all artifacts, last. Building a
  Kustomization from the artifact inventory, or the inventory from the Kustomizations'
  `sourceRef` names, is the same mistake facing opposite ways.
- **A default is policy, not the common value.** `flux_kustomization` defaults a field
  only where nearly every node agrees and the value is a stance we take for all of them;
  a per-app choice (`timeout`, `decryption`, `health_checks`) stays on the node. `None`
  leaves a field unset, so Flux's own default applies; Flux's defaults differ from ours.
  A literal that is the same _fact_ in two places becomes one local; a block that is the
  same _value_ everywhere (the SOPS `decryption` entry) may become one module constant.
- **A node lives with its directory's generator once that directory is fully
  generated** (`aiquota.aiquota`, `litellm.keys.litellm_keys_tf`); until then it stays in
  `<area>/flux_kustomizations.py`, one package per area, and moves as part of the
  conversion. No interim flattening of those packages.
- **Output routing is by `spec.path`**, with the handful of Kustomizations whose `path`
  is not their own directory listed explicitly in the writer. Keep those explicit.
- **A directory's root is written once**: its module's `OUTPUT_DIR` (or a named constant
  like `BASE_DIR`) is `f"{GENERATED_ROOT}/..."` or `f"{HAND_WRITTEN_ROOT}/..."`
  (`manifest_roots.py`), and the artifact in `generate_manifests.py` takes that constant,
  never the path spelled again. Moving a directory between roots is that one edit, plus
  the committed files; `GENERATED_ROOT` is right exactly when the generator writes every
  file the directory holds.

The worked edge, `monitoring-crds -> cilium-monitoring`:

```python
# monitoring/flux_kustomizations.py
def monitoring_crds(chart: Chart) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-crds",
        KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, ...),
        path="./example/prometheus-operator-crd-full",
        interval="1h",
        prune=False,  # Don't delete CRDs on uninstall (safety)
        timeout="5m",
    )


# monitoring/cilium_monitoring.py, beside the chart it deploys
def cilium_monitoring(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cilium-monitoring",
        artifact,
        timeout="2m",
        # The ServiceMonitor CRD.
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )


# generate_manifests.py
monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
monitoring_cilium_artifact = artifact("monitoring-cilium", cilium_monitoring.OUTPUT_DIR)
cilium_monitoring.cilium_monitoring(flux_chart, monitoring_cilium_artifact, monitoring_crds_kustomization)
...
write_artifact_generators(root, ducktape=[..., monitoring_cilium_artifact, ...], flux_system=[...])
```

The one edge still written as a string is `artifact-generators -> flux-system`
(`artifact_generators.py`): `gotk-sync.yaml` owns the bootstrap Kustomization, which
isn't a node in the generated Flux chart.

If a typed field cannot represent a directory's YAML, an in-graph dependency cannot be
passed as a parameter, or finishing a change appears to need another helper, class or
indirection, stop and ask before changing the design.

## Testing a generator

- **The snapshot is the only pin.** `//cluster/cdk8s:test_generate_manifests`
  regenerates in memory and asserts every written file equals the committed one at its
  path, including the single `cluster/k8s/flux/kustomizations.k8s.yaml` chart, and that
  `cluster/generated` holds nothing else; a change to generated output is a diff in the
  PR that makes it. No list of files to keep: the data deps carry both whole trees.
- **Invariants live beside the generator**: tests over the in-memory synth
  (`agentplane/conftest.py`'s `agentplane_manifests`), or
  **fleet rules** (`fleet_rules.py`, run by every synth through
  `chart.node.add_validation`). A rule for every object of a kind is a fleet rule; a
  rule about one construct's shape is a test in its package; a relation that holds by
  construction (the Service selector is the Deployment, the ConfigMap is rendered from
  the Settings model) gets no test.
- **A value in one place gets no test.** A memory limit, a replica count, a hostname:
  editing it is the review. Never copy a generator's literal into an assertion.
- **Code is not its deployment.** A test of code under `x/` or `haku/` never reads
  `cluster/` or synthesizes a cluster chart. A test under `cluster/` may parse deployed
  config with the code's model (moot once the generator renders through it) but never
  exercises code behavior on it. STYLE.md § Testing has the rule.
- **Synthetic props for construct tests, real environments for invariants.** A
  construct test builds a `Chart(Testing.app(), ...)` with a small props value and
  asserts the shape; `test_fleet_rules.py` is the pattern.
- **Render identity across a conversion**: `render_diff.py` reconciles the whole Flux
  graph at two revisions (sources, ArtifactGenerator copies, `kustomize build`, before
  `postBuild`) and diffs every Kustomization's objects, exiting 1 on any difference.
  From the devshell: `python3 cluster/cdk8s/render_diff.py origin/devel HEAD`; renders
  cache under `~/.cache/render-diff` (`--cache-dir` moves it). Its docstring lists the
  Flux semantics it reproduces.

## Adding a fleet rule

A rule is a plain function `objects -> list[str]` over rendered dicts in
`fleet_rules.py`, listed in `FleetRules.validate`, with a unit test on synthetic
objects. Encode what the fleet holds; a rule that fails on `devel` is a policy change
and lands in its own PR with the violations fixed. Exceptions are explicit parameters
(`unpinned_https_egress`), never name matching inside the rule.

## Gotchas

- `Chart.api_objects` is direct children only: walk `chart.node.find_all()` and filter
  with `ApiObject.is_api_object`.
- `cdk8s.Testing.synth(chart)` is `chart.to_json()`, which validates the whole app
  first, so `add_validation` rules run in tests. `from cdk8s import Testing` in a
  `test_*.py` gets collected by pytest (a class named `Test*`): import it as
  `Testing as Cdk8sTesting` with that comment.
- `ApiObject.to_json()` inside a validation is a pure render and safe; mutating the tree
  there is not.
- `EphemeralStorageResources` only accepts whole gibibytes (cdk8s-plus `container.ts`:
  `toGibibytes().toString() + 'Gi'`), and a `JsonPatch` into `containers/N/resources` is
  silently discarded: container resources are re-rendered from the construct's props
  after patches apply.
- `Deployment.scheduling.spread()` is pod anti-affinity, not
  `topologySpreadConstraints`; custom topology spread in LiteLLM uses typed `k8s` structs
  in a patch. Pod-level seccomp is a patch (`PodSecurityContextProps` has no field);
  container-level is typed.
- `cdk8s_plus_34` defaults: `automount_token=False` on a ServiceAccount and
  `automount_service_account_token=False` on a workload, both to set for a TokenReview
  caller; `readOnlyRootFilesystem`/`runAsNonRoot` hardened
  (`agentplane/container_security.py` opts out where unaudited);
  `allowPrivilegeEscalation: false` and `privileged: false` always emitted; the
  Deployment selector is `cdk8s.io/metadata.addr`, not `app.kubernetes.io/name`
  (`select=False` plus `deployment.select(LabelSelector.of(labels=...))` keeps a
  hand-written selector, which is immutable on the live Deployment;
  `Service(selector=deployment)` still selects the address label, which the pods
  carry either way); `scheduling.attract(Node.labeled(...))` renders as required node
  affinity, not `nodeSelector`.
- `add_container(env_from=[EnvFrom(config_map=...)])` takes the wrapper, not the
  ConfigMap.
- `Chart(namespace=...)` would drop the `metadata(name, namespace)` call from every
  object, but it injects the namespace into cluster-scoped objects too (ClusterRole,
  Bundle) with no opt-out; usable only once cluster-scoped objects get their own chart.
- Synth imports each service's `main` for its `Settings`, pulling the runtime in; synth
  tests are `size = "medium"` until a light `settings.py` per service exists
  (`TODO.md`).
- `cdk8s import` names a multi-version CRD's _first listed_ version plainly and
  suffixes the others, regardless of which is the storage version: tofu-controller's
  `Terraform` is v1alpha1, the cluster's CRs are `TerraformV1Alpha2`
  (`//cluster/cdk8s/crd_bindings/tofu_controller:test_terraform_import` pins it).

## Ecosystem (checked 2026-09-18)

No maintained jsii library covers any CRD this repo uses. What exists (`@opencdk8s/*`,
`cdk8s-flux`, `@cdk8s-kit/crds`, `cdk8s-grafana`) is dead, Flux v1, grafana-operator
v4, or bundled `cdk8s import` output, which `cdk8s_import` already produces hermetically
from the deployed CRD version. `cdk8s-plus` ships three Kubernetes minor lines at a time
(32/33/34 today), so `cdk8s-plus-34` is current until a 35 line exists. Datree, the one
validation plugin the cdk8s docs list, is defunct; kubeconform already runs on the
committed output. Reach for `cdk8s_import`, not a package search.

## Typed constructs over raw dicts

**Rule**: never build a resource, or a field's value, as a raw untyped dict when a
typed alternative exists. Three tiers, in order:

1. **`cdk8s_plus_34`'s hand-written fluent layer** — `Namespace`, `Role`, `Deployment`,
   etc. (table below). Richest API (`.add_subjects(...)`, `.from_*_name(...)`, ...),
   so prefer it when the resource kind has one.
2. **`cdk8s_plus_34.k8s`** — the schema-generated raw bindings layer, covering _every_
   core Kubernetes kind and every field, pre-generated inside the same package
   (`from cdk8s_plus_34 import k8s`). This is not a fallback or an escape hatch: it's
   the exact same generator `cdk8s_import` runs for CRDs (below), just already run for
   you against the core API. A resource kind missing from tier 1 — `ResourceQuota`,
   `LimitRange`, `PodDisruptionBudget` — almost certainly has a `k8s.Kube<Kind>` class
   with a typed `<Kind>Spec` here; check `dir(cdk8s_plus_34.k8s)` before assuming
   nothing typed exists. A field missing from an otherwise-typed tier-1 builder (e.g.
   `Deployment` has no `topologySpreadConstraints`) usually has a typed `k8s.<Struct>`
   too (`k8s.TopologySpreadConstraint`) — build the raw-patch _value_ (below) from that
   struct instead of a dict literal.
3. **CRD type** (own `apiVersion` group, e.g. `external-secrets.io`,
   `monitoring.coreos.com`): generate real bindings via `cdk8s_import`
   (`devinfra/js/cdk8s_import.bzl`;
   `//cluster/cdk8s/crd_bindings/{flux,prometheus_operator,gateway_api,cilium}` and
   `//cluster/cdk8s/providers/{external_secrets,keda}` are the examples) — this is the same
   generator tier 2 already ran for you on the core API, just pointed at the CRD's own
   schema instead. `providers/<name>/` is the target layout for every provider
   (`cluster/cdk8s/PLAN.md` item A): the `cdk8s_import` declarations colocated with that
   CRD's generic, cluster-topology-free wrapper functions, once it has one.

   Put each `cdk8s_import` declaration and any import smoke test in
   `cluster/cdk8s/crd_bindings/<provider>/BUILD.bazel`. Keep pinned upstream CRD
   schemas in `MODULE.bazel`; generated Python bindings are Bazel outputs and are
   never checked in.

All three give synth-time validation; a raw dict fails only at `kubectl apply`, if at
all. Check `dir(...)` on the relevant tier — built by cloning `cdk8s-team/cdk8s-plus`
and reading `src/*.ts`, not guessing — before writing a raw dict anywhere.

**A field still needs `ApiObject.of(construct).add_json_patch(JsonPatch.add(path,
value))`** when a tier-1 builder is otherwise right for the resource but doesn't expose
one field — there's no other way to attach an extra field to an already-typed fluent
construct. But build `value` from the matching tier-2 `k8s.<Struct>` (it serializes
correctly as a `JsonPatch` value — verified) instead of a raw dict; only fall back to a
literal dict for a field with no typed struct anywhere. `.add_json_patch(...)` directly
(no `ApiObject.of()`) is for `cdk8s.ApiObject` subclasses, e.g. CRD-generated classes,
patching in something even their own generated schema doesn't cover.

### Typed affordances (`cdk8s_plus_34`) — use these

| Kind                                                 | Builder                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Reference existing by name                                                                                                                                                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                                            | `Namespace`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | —                                                                                                                                                                                                                     |
| ServiceAccount                                       | `ServiceAccount`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | `.from_service_account_name(scope, id, name, namespace_name=...)`                                                                                                                                                     |
| Secret                                               | `Secret`, `BasicAuthSecret`, `SshAuthSecret`, `TlsSecret`, `DockerConfigSecret`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `Secret.from_secret_name(scope, id, name)`                                                                                                                                                                            |
| ConfigMap                                            | `ConfigMap`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | `.from_config_map_name(scope, id, name)`                                                                                                                                                                              |
| PVC / PV                                             | `PersistentVolumeClaim`, `PersistentVolume` (+ AWS/Azure/GCE disk subclasses)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `.from_claim_name`, `.from_persistent_volume_name`                                                                                                                                                                    |
| Role / ClusterRole                                   | `Role`, `ClusterRole` — pass real `rules=` to the constructor, see below                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | `.from_role_name`, `.from_cluster_role_name`                                                                                                                                                                          |
| RoleBinding / ClusterRoleBinding                     | `RoleBinding`, `ClusterRoleBinding` + `.add_subjects(...)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | subjects: `User.from_name`, `Group.from_name` (any string, incl. `oidc-ksbx-groups:haku`), `ServiceAccount.from_service_account_name`, or a `Role`/`ClusterRole` instance                                             |
| Deployment / StatefulSet / DaemonSet / Job / CronJob | matching `Workload` subclass                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | —                                                                                                                                                                                                                     |
| Service                                              | `Service`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | —                                                                                                                                                                                                                     |
| NetworkPolicy (stock k8s)                            | `NetworkPolicy`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | —                                                                                                                                                                                                                     |
| Ingress                                              | `Ingress`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | —                                                                                                                                                                                                                     |
| HorizontalPodAutoscaler                              | `HorizontalPodAutoscaler`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | —                                                                                                                                                                                                                     |
| Probes / handlers                                    | `Probe`, `Handler`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `.from_http_get`, `.from_command`, `.from_tcp_socket`, `.from_grpc`                                                                                                                                                   |
| Volumes                                              | `Volume`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | `.from_config_map`, `.from_secret`, `.from_empty_dir`, `.from_persistent_volume_claim`, `.from_host_path`, `.from_nfs`, `.from_csi`, `.from_aws_elastic_block_store`, `.from_azure_disk`, `.from_gce_persistent_disk` |
| Any CRD (own `apiVersion` group)                     | `cdk8s_import`-generated bindings                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | —                                                                                                                                                                                                                     |
| ResourceQuota                                        | tier 2: `k8s.KubeResourceQuota(metadata=k8s.ObjectMeta(...), spec=k8s.ResourceQuotaSpec(hard={...}))` — values are `k8s.Quantity.from_string(...)`/`.from_number(...)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | —                                                                                                                                                                                                                     |
| LimitRange                                           | tier 2: `k8s.KubeLimitRange(spec=k8s.LimitRangeSpec(limits=[k8s.LimitRangeItem(type=..., max=..., min=..., default=..., default_request=...), ...]))`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | —                                                                                                                                                                                                                     |
| PodDisruptionBudget                                  | tier 2: `k8s.KubePodDisruptionBudget(spec=k8s.PodDisruptionBudgetSpec(min_available=..., selector=...))`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | —                                                                                                                                                                                                                     |
| Pod-level `securityContext.seccompProfile`           | no tier-1/tier-2 builder on `Deployment`'s `security_context=` — `PodSecurityContextProps` has no `seccomp_profile` field (verified against the installed package: only `ensure_non_root`/`fs_group`/`fs_group_change_policy`/`group`/`sysctls`/`user`). Raw `JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", k8s.SeccompProfile(type="RuntimeDefault"))` is the only path. **Container-level** `securityContext.seccompProfile` is different: `ContainerSecurityContextProps.seccomp_profile` is a real, typed tier-1 field — pass `SeccompProfile(type=SeccompProfileType.RUNTIME_DEFAULT)` there directly, never a patch. | —                                                                                                                                                                                                                     |

Anything not in the table: check `dir(cdk8s_plus_34.<Thing>)` (tier 1), then
`dir(cdk8s_plus_34.k8s)` (tier 2 — `Kube<Kind>` for a resource, or the bare struct name
for a field value, e.g. `k8s.<Struct>`); if both are inconclusive, clone
`https://github.com/cdk8s-team/cdk8s-plus` and grep `src/*.ts` (tier 1) or
`src/imports/k8s.ts` (tier 2) for the kind's `export class`/`export interface` before
reaching for a raw dict. Add the result to the table.

#### RBAC rules go through the typed constructor, not a `/rules` patch

`Role`/`ClusterRole`'s `rules=` takes `RolePolicyRule`/`ClusterRolePolicyRule`, each
`resources`/`endpoints` a list of real `IApiResource`/`IApiEndpoint` — not dicts. Fully
typed, including `resourceNames`:

- **Resource type, no name scoping**: `ApiResource.<CONSTANT>` (60+ constants — `dir(cdk8s_plus_34.ApiResource)`) or `ApiResource.custom(api_group=..., resource_type=...)` for anything else, subresources included (`"pods/exec"`, `"serviceaccounts/token"`).
- **Scoped to one named object**: pass that kind's own `from_*_name` reference (`Secret.from_secret_name(...)`, `Role.from_role_name(...)`, ...) as the `IApiResource` — its `resource_name` is already wired.
- **Scoped to a name with no typed kind covering it** (e.g. `serviceaccounts/token`): `ApiResource.custom()` never sets `resource_name`. Implement `IApiResource` directly — `@jsii.implements(cdk8s_plus_34.IApiResource)` on a small class with `api_group`/`resource_type`/`resource_name` properties, same as `Secret.from_secret_name` does internally. Needs `@pypi//jsii` as an explicit `BUILD.bazel` dep. Example: `agentplane/rbac.py`'s `_NamedApiResource`; the plain
  `ApiResource.custom()` cast lives once in `api_resource.custom_resource`.
- **Caveat, not an excuse to go raw**: synthesis emits **one output rule per `IApiResource` entry**, always — `RolePolicyRule(resources=[a, b], ...)` becomes two rules, never one rule listing two resource types (`role.ts`'s `synthesizeRules()`; no typed way around it). RBAC-equivalent (Kubernetes unions all rules), so a hand-written file's rule _grouping_ won't survive conversion unchanged — only its permissions. Expect that diff.

## `image-pins/kustomization.yaml`: the `:tag` Setters marker, not the bare form

Where a converted directory uses a hand-written (never generated) `image-pins/`
Component, it pins the real tag via a placeholder `newTag:` plus a Flux image-automation
Setters marker comment, one entry per image:

```yaml
images:
  - name: git.allegedly.works/ducktape-ci/<image>
    newTag: <tag> # {"$imagepolicy": "flux-system:<name>:tag"}
```

The marker's trailing `:tag` is load-bearing: it tells Setters to write back only the
bare tag value into the marked `newTag:` field. Omit it and Setters instead rewrites the
_whole_ marked field to the full `repository:tag` reference — which corrupted a
`newTag:`-only field into `repository:repository:tag` on its very first write-back and
took `litellm` down (`InvalidImageName`), a real incident, not a theoretical one. See
<https://fluxcd.io/flux/components/image/imageupdateautomations/> § "Field-specific
update markers". Don't repeat this explanation per directory; point back here instead.

A bare-tag field (an `*_IMAGE_TAG` env value) takes the placeholder too, and the Component
copies the pinned tag into it with a block-style `replacements` rule that splits the
container `image` on `:` (`images:` runs first); see `agents/airlock/image-pins`.

Agentplane testing keeps its image pins inline in the hand-maintained root
`kustomization.yaml`, since the Kustomization itself is part of the flat resource
directory. Flux updates those `newTag:` markers in place; cdk8s generates only the
separate `agentplane.k8s.yaml` resource file.

The `images:` transformer matches by image `name:` across **every resource in the
Kustomization's rendered output**, not just one Deployment — relevant when a directory's
chart shares one image across multiple resources (e.g. `app/`'s runner `SandboxTemplate`
alongside its Deployment): one `image-pins` entry patches all of them.

## Restructuring which Kustomization owns an object: land it in two steps

Converting a directory to cdk8s often changes which Flux `Kustomization` renders a
given object (e.g. folding `namespace`/`credentials` into `app`). **Never do this in
one step.** Deleting the old Kustomization and having the new one claim the same
objects in the same PR is a race: Flux's default `deletionPolicy: MirrorPrune` prunes
everything the old CR managed once it's gone, and nothing guarantees the new
Kustomization re-claims those objects (updates `kustomize.toolkit.fluxcd.io/name`)
first. **Confirmed, not theoretical**: this race deleted `ha-mcp`'s entire namespace
(Deployment, Service, ConfigMap, RBAC, CiliumNetworkPolicy, ServiceMonitor) when its
`namespace`/`credentials` Kustomizations folded into `app` (#7150). Stateless objects
self-heal; a `PersistentVolumeClaim` can permanently lose its volume (depends on
`reclaimPolicy`) and won't auto-rebind to an orphaned `PersistentVolume`.

Fix: `//cluster/cdk8s/crd_bindings/flux:kustomization`'s `KustomizationSpec` has
`deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN`. Land in two changes:

1. Set `deletionPolicy: Orphan` on the _old_ Kustomization(s) being folded away, nothing
   else. Merge and let it reconcile to protect against deletion during the handoff.
2. Only then: delete the old Kustomization(s), let the new one render and claim the
   same objects. Flux's SSA apply adopts them (updates the ownership label); the old
   owner's deletion can no longer delete the transferred objects.

Live-cluster ownership concern, not manifest content — `kustomize build`/`flux build
--dry-run` render correctly either way and can't catch it. Verify via the live cluster
(`kubectl get <kind> -n <namespace> -o jsonpath='{.metadata.uid}'` unchanged = adopted,
not recreated), not by diffing rendered YAML.

Adoption can also change operator-generated Service selectors. Follow
[the ownership-label traffic checks](../AGENTS.md#migrating-stateful-flux-kustomizations)
and verify service continuity separately from object survival.

Complementary, resource-level tool: `kustomize.toolkit.fluxcd.io/prune: "disabled"`
annotation, for a single object dropped from a still-live Kustomization's output (no CR
deletion involved) — exempts it from pruning regardless of ownership-label timing.
