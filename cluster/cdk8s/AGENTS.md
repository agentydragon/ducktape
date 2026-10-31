# cdk8s generators

Design and conversion mechanics: <../docs/cdk8s.md>. Which directories are generated and
how to regenerate: `cluster/AGENTS.md` § Generated manifests.

## Boundaries

- **Generation code is not an application library.** Deployment config builders and
  generated CRD bindings are visible only to their cdk8s consumers. Shared model facts and
  cross-consumer policy live in `//model_catalog` (imported by cdk8s and Nix); cluster
  endpoints, credentials and environment selections stay here. External tools and
  acceptance tests read generated artifacts or deployed APIs, not generator internals.
  Existing narrow visibility grants for cluster validation are test seams, not precedent
  for runtime imports; public data targets and the synthesis CLI are separate from Python
  implementation visibility.
- **The vocabulary is Kubernetes, cdk8s, Flux and Kustomize objects, plus plain Python
  values.** No marker annotation, "provides" declaration, registry, or convention layered
  on top of the objects' own fields — nor a rule/check that only works with one. If a
  change seems to need one, stop and ask; the operator approves the design first.
  Allowed: an identity-only reference (frozen dataclass of names, keys, ports, labels plus
  derived addresses, declared once by the producer, projected into each dialect by
  methods) — not a record describing what to build; a map that _checks_ hand-written
  wiring (`cluster/validation/crd_layering.py`'s `OPERATOR_CRDS`) — not one that
  _chooses_ wiring.
- **Construction runs forward** (§ The Flux graph): inputs are values or constructs built
  earlier; every fact a node depends on is in its signature.
- **Stateful data is never destroyed by a change here.** Databases, PVs and anything a
  person authored survive every conversion; caches may be dropped. A Kustomization owning
  PVCs carries `deletionPolicy: Orphan`; ownership changes follow § Restructuring.
  Agentplane's "deployed state is disposable" (<../../agentplane/AGENTS.md>) covers schemas
  and wire formats, not volumes.
- **Escape hatches stay.** Every Flux/Kustomize field stays expressible, so an incident
  `suspend`, the two-step ownership move, a VolSync restore or a hand-written sibling file
  is a plain edit. Make a shape unrepresentable only where the operator asked; by default
  the safe procedure is documented, not enforced.

## Shape of a generator

- **Layout**: shared helpers and single-module components at the package root; a
  multi-module component gets a named subpackage (`agentplane/`, `haku/`, `clickhouse/`,
  `litellm/`). No directory for one file, no `agents/` layer, no `_constructs` suffix
  (`agentplane/actions.py`, `litellm/proxy.py`).
- **One frozen `Environment` per Agentplane stack** (`agentplane/environment.py`;
  instances in `agentplane/staging.py` and the k3s fixture). Constructs take the
  whole environment and read what they need (`env.namespace`, `env.replicas.count`), so
  shared values are written once. Data only — never a callable "also build this" field.
- **One chart function per environment** (`staging.py` `chart(app)`), shared
  by `generate_manifests.py` and test fixtures. It calls `agentplane/chart.py`'s
  `environment_chart(app, env)` and adds environment-only objects to the returned chart —
  never `if env.namespace == ...` in a construct, never a callback hook. Objects added
  afterwards are still checked (fleet rules run at App synth). `haku/charts.py`'s
  `*_chart(app)` is the same shape. The entry point holds no environment data.
- **References, not names**: `Service(selector=deployment)`,
  `Volume.from_config_map(config_map)`, `Role.from_role_name(...)`; network rules target a
  workload via the owning module's exported constant
  (`cilium.endpoint_labels(namespace, egress.NAME)`). Helpers return the object they build.
- **A name carries only what its namespace does not**: in namespace `atuin`, `server`,
  `quota`, `data`. The namespace's main workload and its Service/HTTPRoute/monitors may use
  the namespace name or `app`. Accepted stutter we don't author: Helm/operator-derived
  names (CNPG `<cluster>-rw`, Seaweed/ClickHouse/Alertmanager suffixes), vendored
  manifests, and names that are identities outside the namespace (SeaweedFS bucket/IAM,
  Postgres database/role, Secrets mirrored under one name, cross-namespace RBAC named for
  its consumer, Roles named in Agentplane's grant catalog or resolved by name from
  existing managed Sandboxes' snapshotted grants). Stateful objects (PVC, CNPG Cluster,
  Valkey, VolSync source) keep their name until moved for another reason — renaming
  migrates data (<../AGENTS.md> § Migrating stateful Flux Kustomizations).
- **The service's `Settings` is its deployment contract.** Render flags, env vars and
  settings files through the consumer's model (`util/settings_contract.py`:
  `cli_args`, `env_name`, `validate_settings_fragment`, `checked_value`) so a renamed
  field fails at synth. For a complete settings file, construct the full model with
  typed keyword arguments and pass it to `yaml_config` before `SettingsFile`.
  `yaml_config` and `json_config` serialize models recursively, including models nested in
  source mappings and lists, emitting only explicitly set model fields. Keep models typed
  until this text boundary; use `model_dump` only for an explicit field projection or
  different serialization policy.
  For a file fragment whose other values come from runtime sources, check the present
  keys and values with `yaml_config(validate_settings_fragment(Settings, content))`;
  the helper returns the original mapping unchanged. Fragment checks do not establish
  completeness or cross-field consistency; runtime settings validation checks the
  effective file-plus-environment configuration. `SettingsFile` only creates the
  ConfigMap and wires its mount and path env var. The code package owns `Settings` and
  its `config_file_env`; `cluster/cdk8s` owns where it runs.
  Nothing under `x/` or `haku/` imports `cluster/`. A project's `deploy/`
  may hold a props-driven construct (tested with synthetic props); the cluster's
  instantiation lives here. Generally, config is typed by its consumer's schema; where
  that schema is another artifact, mirror it in a model tied by a test — e.g. a
  tofu-controller module's inputs are a frozen pydantic model mirroring `variables.tf`
  (`gitops_terraform(variables=...)`, `test_terraform_vars.py`), since tofu only warns on
  undeclared variables.
  Put authoring explanations in Python comments, not prose prepended to serialized
  ConfigMap values. Keep syntax required by the consumer, such as parser directives.
- **What the generator knows, it renders** as a Python constant. Flux `postBuild`
  substitution is only for values only the cluster has: it costs a Kustomization plus a
  `dependsOn` edge per consumer, and the committed manifests see only
  `${VAR}`.
- **Cluster-wide behavior is configured once where it runs; objects carry only
  exceptions.** Reloader runs `autoReloadAll`, ignores Jobs/CronJobs (`reloader.py`), and
  reads only workload metadata (not the pod template): a workload that must not restart
  says `"false"` there; others say nothing. Alloy discovers every
  ServiceMonitor/PodMonitor/PrometheusRule without a selector — no label for it.
- **One shape per job** (build a kind, a field variant, amend a pod spec, declare a Flux
  node). Find how the tree already does it before writing a helper; a second shape for the
  same job is a review finding even if both work.
- **One helper per repeated shape**: cluster facts in `cilium.py` (`egress_via_gateway`,
  `dns_egress`, `fqdn_fence`, ...), generic shapes in `providers/cilium/network_policy.py`
  (`EgressRule`/`IngressRule`); also `gateway.https_route`, `probes.http_probe`,
  `node_scheduling.py`, `pod_policy.py`,
  `api_resource.custom_resource`. Parameterize only variation call sites actually have.
- **A value feeding two artifacts lives once** (web-push hosts: one tuple in
  `agentplane/staging.py` feeds both the Action Service allowlist and its egress rule).
  Derive both; never a test reading both.
- **New Kustomization directories default to cdk8s** when they hold more than a
  `HelmRelease` plus values.

## The Flux graph

Every Flux `Kustomization` is one function in one shared chart; its dependencies are its
parameters. `generate_manifests.py` is the hand-written topological order.

- **A node is `name(chart, directory: RenderedDirectory, *predecessors) -> Kustomization`**,
  writes no file, and returns `flux.flux_kustomization(chart, name, directory, ...)`,
  which derives `sourceRef`, `path`, `decryption` from the directory and applies our
  defaults (listed in its docstring); the node passes only differing `KustomizationSpec`
  fields as same-named keywords. The entry point builds the artifact
  (`artifact_generators.artifact(name, directory, *shared_bases)`) just before the call
  and passes `generation.write_directory(root, artifact, *chart_builders, siblings=[...])`
  as the node's argument, which synthesizes the directory's one generated file
  (<../docs/cdk8s.md> § Shapes of a directory) plus a `kustomization.yaml` listing it and
  hand-written siblings. Every artifact goes to `write_artifact_generators` last; parked
  nodes' artifacts are left out. A directory with a hand-written `kustomization.yaml` is
  written by its module's `write_manifests` at the top of `generate_manifests()`, and its
  node takes the artifact instead of a directory (as do nodes not yet on
  `write_directory`). A node sourcing a `GitRepository` directly passes that `sourceRef`
  and a `path`. `dependsOn` is `flux_kustomization_depends_on_many(predecessor, ...)`,
  reading name/namespace off the constructs (so the entry carries an explicit
  `namespace`). Predecessors are already-built values — never strings, module-level
  lookups, or late resolution.
- **The entry point calls nodes in dependency order**, passing results as locals; a wrong
  order is a `NameError`, a cycle unwritable. Never add a registry, sorter, lazy
  reference, node-describing class/record, or a module that builds nodes on import.
- **A resource chart joins in order: chart → Kustomization → dependents.** Build the chart
  (`staging.chart(app)`, `haku.charts.console_chart(app)`); the node takes _values_
  derived from it at the join in `generate_manifests.py`
  (`health_checks=flux.health_checks(chart, kinds)`) plus predecessors — never the `Chart`
  itself, so every crossing fact is a parameter and the node is testable without
  importing workload modules. A second crossing fact is a second parameter (the review
  signal). A Kustomization never builds the chart it describes.
- **Build each object from what it reads at runtime, never from what reads it**:
  Kustomization from artifact, path, predecessors; artifact from directory;
  `ArtifactGenerator` from all artifacts, last. Never derive Kustomizations from the
  artifact inventory or vice versa.
- **A `flux_kustomization` default is policy, not the common value**: only where nearly
  every node agrees as a stance; per-app choices (`timeout`, `decryption`,
  `health_checks`) stay on the node. `None` leaves the field unset (Flux's default, which
  differs from ours). The same _fact_ twice becomes one local; the same _value_
  everywhere (SOPS `decryption`) may become a module constant.
- **A node lives with its directory's generator once fully generated**
  (`aiquota.aiquota`, `litellm.keys.litellm_keys_tf`); until then in
  `<area>/flux_kustomizations.py`, moving as part of the conversion. No interim flattening
  of those packages.
- **Output routing is by `spec.path`**; Kustomizations whose `path` isn't their own
  directory are listed explicitly in the writer — keep them explicit.
- **A directory's root is written once**: its module's `OUTPUT_DIR` (or e.g. `BASE_DIR`)
  is `f"{GENERATED_ROOT}/..."`, `f"{HAND_WRITTEN_ROOT}/..."` or `f"{PARKED_ROOT}/..."`
  (`manifest_roots.py`), and `generate_manifests.py` passes that constant. Moving roots is
  that edit plus the committed files; `GENERATED_ROOT` iff the generator writes every
  file in the directory.

Worked edge, `monitoring-crds -> ntfy`:

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


# ntfy.py, beside the chart it deploys
def ntfy(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, monitoring_crds, kyverno),
    )


# generate_manifests.py
monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
...
ntfy_artifact = artifact("ntfy", ntfy.OUTPUT_DIR)
ntfy_kustomization = ntfy.ntfy(
    flux_chart,
    # The SOPS sibling turns on Flux decryption.
    write_directory(root, ntfy_artifact, ntfy.chart, siblings=["credentials.sops.yaml"]),
    cnpg_kustomization,
    external_secrets_operator_kustomization,
    monitoring_crds_kustomization,
    kyverno_kustomization,
)
...
write_artifact_generators(root, ducktape=[..., ntfy_artifact, ...], flux_system=[...])
```

The only string edge is `artifact-generators -> flux-system` (`artifact_generators.py`):
`gotk-sync.yaml` owns the bootstrap Kustomization, which isn't a graph node.

Stop and ask before changing the design if a typed field can't represent a directory's
YAML, an in-graph dependency can't be a parameter, or finishing seems to need another
helper, class or indirection.

## Testing a generator

- **The snapshot is the only pin.** `//cluster/cdk8s:test_generate_manifests` regenerates
  in memory, asserts every written file (including
  `cluster/k8s/flux/kustomizations.k8s.yaml`) equals the committed one, and that
  `cluster/generated` holds nothing else. No keep-lists: data deps carry both trees.
- **Invariants live beside the generator**: tests over the in-memory synth
  (`agentplane/conftest.py`'s `agentplane_manifests`) or fleet rules (`fleet_rules.py`,
  attached to every App `generation.write_app` writes, seeing all its charts). Every object
  of a kind → fleet rule; one construct's shape → test in its package; a relation holding
  by construction (selector is the Deployment, ConfigMap rendered from `Settings`) → no
  test.
- **A value in one place gets no test** (memory limit, replica count, hostname); never
  copy a generator literal into an assertion.
- **Code is not its deployment**: STYLE.md § Testing; here, tests of code under `x/` or
  `haku/` never read `cluster/` or synthesize a cluster chart.
- **Construct tests use synthetic props** (`Chart(scratch_app, ...)`, pattern:
  `test_fleet_rules.py`); invariants use real environments.
- **Test apps and charts are the `scratch_app`/`scratch_chart` fixtures** (`testing/scratch.py`,
  exposed by `conftest.py`), outdir under `tmp_path`; one test's charts share its `scratch_app`
  (distinct ids). Module/session-scoped fixtures call `scratch_app_factory()`.
  `Testing.app()`/`Testing.chart()` are banned (ruff `TID251`): each call leaks a
  `/tmp/cdk8s.outdir.*`. Helpers take the `App`/`Chart` as a parameter; outside pytest, use
  `App()`. The conftest imports cdk8s, so every test under this tree needs jsii's Node runtime
  (`data` + `JSII_NODE`).

## Adding a fleet rule

A plain function `objects -> list[str]` over rendered dicts in `fleet_rules.py`, listed in
`FleetRules.validate`, unit-tested on synthetic objects. Encode what the fleet already
holds; a rule failing on `devel` is a policy change and lands in its own PR with the
violations fixed. Exceptions are explicit `write_app` parameters (`unhardened_pods`,
`unpinned_https_egress`), never name matching in the rule; synth fails on an exception
whose object is gone or now passes.

## Gotchas

- `Chart.api_objects` is direct children only: walk `chart.node.find_all()` filtered by
  `ApiObject.is_api_object`.
- `cdk8s.Testing.synth(chart)` is `chart.to_json()`, which validates the whole app, so
  `add_validation` rules run in tests. In a `test_*.py`, `from cdk8s import Testing` gets
  collected by pytest: import `Testing as Cdk8sTesting` with a comment saying why.
- `ApiObject.to_json()` inside a validation is a safe pure render; mutating the tree there
  is not.
- `EphemeralStorageResources` accepts only whole GiB, and a `JsonPatch` into
  `containers/N/resources` is silently discarded (resources re-render from props after
  patches).
- `Deployment.scheduling.spread()` is pod anti-affinity, not `topologySpreadConstraints`
  (LiteLLM patches typed `k8s` structs). Pod-level seccomp needs a patch
  (`PodSecurityContextProps` has no field): `JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", k8s.SeccompProfile(type="RuntimeDefault"))`;
  container-level is typed (`ContainerSecurityContextProps.seccomp_profile=SeccompProfile(type=SeccompProfileType.RUNTIME_DEFAULT)`),
  never a patch.
- `cdk8s_plus_34` defaults: `automount_token=False` (ServiceAccount) and
  `automount_service_account_token=False` (workload) — set both for a TokenReview caller;
  `readOnlyRootFilesystem`/`runAsNonRoot` hardened (a container with unaudited writes
  states `read_only_root_filesystem=False`); `allowPrivilegeEscalation: false` and
  `privileged: false` always emitted, capabilities never (`pod_policy.harden` drops ALL
  where unset). The Deployment selector is `cdk8s.io/metadata.addr`, not
  `app.kubernetes.io/name`; `select=False` plus
  `deployment.select(LabelSelector.of(labels=...))` keeps a hand-written selector
  (immutable on the live Deployment), and `Service(selector=deployment)` still selects the
  address label, which pods carry either way. `scheduling.attract(Node.labeled(...))` is
  required node affinity, not `nodeSelector`.
- `add_container(env_from=[EnvFrom(config_map=...)])` takes the wrapper, not the
  ConfigMap.
- `Chart(namespace=...)` also injects the namespace into cluster-scoped objects
  (ClusterRole, Bundle) with no opt-out; usable only once those get their own chart.
- `cdk8s import` names a multi-version CRD's _first listed_ version plainly and suffixes
  the rest, regardless of storage version: tofu-controller's `Terraform` is v1alpha1; the
  cluster's CRs are `TerraformV1Alpha2`
  (`//cluster/cdk8s/providers/tofu_controller:test_terraform_import`).

## Typed constructs over raw dicts

Never build a resource or field value as a raw dict when a typed alternative exists; a raw
dict fails only at apply, if at all. Tiers, in order:

1. **`cdk8s_plus_34` fluent layer** — richest API; prefer when the kind has one.
2. **`cdk8s_plus_34.k8s`** — generated bindings for _every_ core kind and field (the same
   generator `cdk8s_import` runs). Not a fallback: kinds missing from tier 1 have
   `k8s.Kube<Kind>` + `<Kind>Spec`; fields missing from a tier-1 builder have a
   `k8s.<Struct>` (e.g. `k8s.TopologySpreadConstraint`) to build the patch value from.
3. **CRDs** (own `apiVersion` group): `cdk8s_import` bindings under
   `providers/<name>/` (declarations and pins: <../docs/cdk8s.md> § CRD bindings), beside
   that CRD's generic wrappers. `providers/<name>/` holds only what the CRD schema defines
   — never a ducktape namespace, secret name, hostname, topology fact, or one caller's use
   of a field the schema leaves untyped (a freeform `metadata: map[string]string`); those
   stay in the ducktape module, passed as parameters (`haku_ci/runner.py` owns its KEDA
   `forgejo-runner` scaler `metadata`, agentydragon/ducktape#7952). Wrapper design: the
   `cdk8s_builders` skill.

Check `dir(...)` on the tier before going raw; if inconclusive, grep a clone of
`https://github.com/cdk8s-team/cdk8s-plus` — `src/*.ts` (tier 1) or `src/imports/k8s.ts`
(tier 2) — for the `export class`/`export interface`. Record non-obvious finds in the
list below.

A field a tier-1 builder lacks: `ApiObject.of(construct).add_json_patch(JsonPatch.add(path, value))`
with `value` a tier-2 `k8s.<Struct>` (serializes correctly); a literal dict only when no
struct exists. CRD-generated `ApiObject` subclasses call `.add_json_patch(...)` directly.

### Typed affordances

- Tier 1 covers Namespace, ServiceAccount, Secret (+ `BasicAuthSecret`, `SshAuthSecret`,
  `TlsSecret`, `DockerConfigSecret`), ConfigMap, PVC/PV, Role/ClusterRole,
  RoleBinding/ClusterRoleBinding, all workload kinds, Service, NetworkPolicy, Ingress,
  HorizontalPodAutoscaler, `Probe`/`Handler`, `Volume` (`.from_*`). Existing objects by
  name: `.from_<kind>_name(...)` (e.g. `ServiceAccount.from_service_account_name(scope, id, name, namespace_name=...)`).
- Binding subjects (`.add_subjects`): `User.from_name`, `Group.from_name` (any string,
  incl. `oidc-ksbx-groups:haku`), `ServiceAccount.from_service_account_name`, or a
  `Role`/`ClusterRole`.
- Tier 2 examples: `k8s.KubeResourceQuota(spec=k8s.ResourceQuotaSpec(hard={...}))` with
  `k8s.Quantity.from_string(...)`/`.from_number(...)`;
  `k8s.KubeLimitRange(spec=k8s.LimitRangeSpec(limits=[k8s.LimitRangeItem(...)]))`;
  `k8s.KubePodDisruptionBudget(spec=k8s.PodDisruptionBudgetSpec(...))`.

#### RBAC rules go through the typed constructor, not a `/rules` patch

`Role`/`ClusterRole` `rules=` takes `RolePolicyRule`/`ClusterRolePolicyRule` whose
`resources` are `IApiResource`s:

- Unscoped: `ApiResource.<CONSTANT>`, or `api_resource.custom_resource(api_group, resource_type)`
  (the one `ApiResource.custom()` cast) for others incl. subresources (`pods/exec`).
- One named object of a typed kind: that kind's `from_*_name` reference.
- Named, no typed kind (e.g. `serviceaccounts/token` — `ApiResource.custom()` never sets
  `resource_name`): `api_resource.named_resource(...)` (a `@jsii.implements` class; such a
  class needs `@pypi//jsii` as an explicit dep).
- Synthesis emits one rule per `IApiResource` (`role.ts` `synthesizeRules()`): a
  hand-written file's rule grouping won't survive conversion, only its permissions. Expect
  that diff; it is not a reason to go raw.

### Ecosystem (checked 2026-09-18)

No maintained jsii library covers any CRD here (`@opencdk8s/*`, `cdk8s-flux`,
`@cdk8s-kit/crds`, `cdk8s-grafana` are dead, Flux v1, grafana-operator v4, or bundled
`cdk8s import` output). Use `cdk8s_import`, not a package search. `cdk8s-plus-34` is
current until a 35 line exists (it ships three minors). Datree is defunct; kubeconform
already checks committed output.

## `image-pins/kustomization.yaml`: the `:tag` Setters marker, not the bare form

A hand-written `image-pins/` Component pins each image via placeholder `newTag:` plus a
Setters marker:

```yaml
images:
  - name: git.allegedly.works/ducktape-ci/<image>
    newTag: <tag> # {"$imagepolicy": "flux-system:<name>:tag"}
```

The trailing `:tag` is load-bearing: without it Setters writes the full `repository:tag`
into the field, which once produced `repository:repository:tag` and took `litellm` down
(`InvalidImageName`). Point here rather than re-explaining per directory.

A bare-tag field (an `*_IMAGE_TAG` env value) also takes the placeholder; the Component
copies the pinned tag in with a block-style `replacements` rule splitting the container
`image` on `:` (`images:` runs first) — see `cluster/k8s/agents/airlock/image-pins`.
`images:` matches by name across every resource in the Kustomization's output, so one
entry patches e.g. both `app/`'s runner `SandboxTemplate` and its Deployment.

## Restructuring which Kustomization owns an object: land it in two steps

Never move objects between Flux Kustomizations in one step: the old CR's default
`MirrorPrune` can delete them before the new one adopts them. This deleted `ha-mcp`'s
whole namespace (#7150); a PVC can permanently lose its volume and won't rebind to an
orphaned PV.

1. Set `deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN`
   (`//cluster/cdk8s/providers/flux:kustomization`) on the old Kustomization(s), nothing
   else; merge and let it reconcile.
2. Then delete the old Kustomization(s); the new one's SSA apply adopts the objects.

`kustomize build`/`flux build --dry-run` can't catch this; verify on the live cluster that
`.metadata.uid` is unchanged (adopted, not recreated). Adoption can also change
operator-generated Service selectors: follow
[the ownership-label traffic checks](../AGENTS.md#migrating-stateful-flux-kustomizations)
and verify service continuity separately. For one object dropped from a still-live
Kustomization, annotate it `kustomize.toolkit.fluxcd.io/prune: "disabled"`.
