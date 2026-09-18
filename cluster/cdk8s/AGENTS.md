# cdk8s generators

Design and conversion mechanics: <../docs/cdk8s.md>. Which directories are generated and
how to regenerate: `cluster/AGENTS.md` § Generated manifests.

## Shape of a generator

Model with constructs, deploy with one props object per environment.

- **One `Environment` per deployed namespace** (`agentplane/environment.py`;
  `agentplane/staging.py` and `agentplane/testing.py` each hold one frozen instance).
  Every construct takes the whole environment and reads what it needs
  (`env.namespace`, `env.replicas.count`, `env.egress.ca_secret_name`), so a value two
  constructs share is written once. Environment-only objects are a callable on the
  props (`extra`), never `if env.namespace == ...` inside a construct.
- **One chart function composes the environment** (`agentplane/chart.py`'s
  `environment_chart(app, env)`), shared by `generate_manifests.py` (synth and write)
  and the test fixtures (`Testing.synth` in memory). The entry point holds no
  environment data.
- **References, not names.** `Service(selector=deployment)`,
  `Volume.from_config_map(config_map)`, `Role.from_role_name(...)`; a network rule
  targets a workload through the constant the owning module exports
  (`cilium_helpers.endpoint_labels(namespace, egress_constructs.NAME)`), never the
  string spelled again.
- **The service's `Settings` is its deployment contract.** Flags, env vars and settings
  files are rendered through the binary's pydantic-settings model
  (`x/agentplane/settings_contract.py`: `cli_args`, `env_name`, `settings_file`,
  `checked_value`), so a renamed field fails at synth. The code package owns `Settings`
  and its `CONFIG_FILE_ENV`; `cluster/cdk8s` owns where and how it runs. Nothing under
  `x/` or `haku/` imports `cluster/`. A project's own `deploy/` may hold a props-driven
  construct (tested with synthetic props); the cluster's instantiation of it lives here.
- **One helper per repeated shape.** When the same dozen generated-struct lines appear
  twice, name the shape once: `agentplane/cilium_helpers.py` (`ingress_from_gateway`,
  `egress_to`, `egress_to_fqdns`, `egress_via_gateway`, `dns_egress`,
  `deny_all_egress`, ...), `gateway.https_route`, `probes.http_probe`,
  `agentplane/migrate_container.py`, `agentplane/node_scheduling.py`,
  `pod_spec_patches.py`, `api_resource.custom_resource`. Parameterize the variation the
  call sites have (SNI list, listener, timeout), not variation nobody uses.
- **A value that feeds two artifacts lives once.** The web-push hosts feed both the
  Action Service allowlist and its egress rule from one tuple in `staging.py`;
  `Environment.provided_secrets` ties each externally provided Secret to the Flux
  dependency that creates it. When two artifacts must agree, derive both from one value;
  never write a test that reads both.
- **New Kustomization directories default to cdk8s** when they hold more than a
  `HelmRelease` plus values.

## Testing a generator

- **The snapshot is the only pin.** `//cluster/cdk8s:test_generate_manifests`
  regenerates every converted directory in memory and asserts equality with the
  committed files; a change to generated output is a diff in the PR that makes it. No
  other test reads a committed `.k8s.yaml` or `flux-kustomization.yaml`.
- **Invariants live beside the generator**: tests over the in-memory synth
  (`agentplane/conftest.py`'s `agentplane_manifests`;
  `cluster/validation/agentplane_fixtures.py` where a test needs that package), or
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
  `topologySpreadConstraints` (`pod_spec_patches.py` patches those). Pod-level seccomp
  is a patch (`PodSecurityContextProps` has no field); container-level is typed.
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
   `//third_party/{flux,prometheus_operator,gateway_api,external_secrets,cilium}` are
   the examples) — this is the same generator tier 2 already ran for you on the core
   API, just pointed at the CRD's own schema instead.

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
- **Scoped to a name with no typed kind covering it** (e.g. `serviceaccounts/token`): `ApiResource.custom()` never sets `resource_name`. Implement `IApiResource` directly — `@jsii.implements(cdk8s_plus_34.IApiResource)` on a small class with `api_group`/`resource_type`/`resource_name` properties, same as `Secret.from_secret_name` does internally. Needs `@pypi//jsii` as an explicit `BUILD.bazel` dep. Example: `agentplane/namespace_rbac_constructs.py`'s `_NamedApiResource`; the plain
  `ApiResource.custom()` cast lives once in `api_resource.custom_resource`.
- **Caveat, not an excuse to go raw**: synthesis emits **one output rule per `IApiResource` entry**, always — `RolePolicyRule(resources=[a, b], ...)` becomes two rules, never one rule listing two resource types (`role.ts`'s `synthesizeRules()`; no typed way around it). RBAC-equivalent (Kubernetes unions all rules), so a hand-written file's rule _grouping_ won't survive conversion unchanged — only its permissions. Expect that diff.

## `image-pins/kustomization.yaml`: the `:tag` Setters marker, not the bare form

Every converted directory's hand-written (never generated) `image-pins/` Component pins
the real tag via a placeholder `newTag:` plus a Flux image-automation Setters marker
comment, one entry per image:

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

Fix: `//third_party/flux:kustomization`'s `KustomizationSpec` has
`deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN`. Land in two changes:

1. Set `deletionPolicy: Orphan` on the _old_ Kustomization(s) being folded away, nothing
   else. Merge and let it reconcile — this is what makes the handoff safe.
2. Only then: delete the old Kustomization(s), let the new one render and claim the
   same objects. Flux's SSA apply adopts them (updates the ownership label); no race
   left, since the old CR's deletion no longer touches them.

Live-cluster ownership concern, not manifest content — `kustomize build`/`flux build
--dry-run` render correctly either way and can't catch it. Verify via the live cluster
(`kubectl get <kind> -n <namespace> -o jsonpath='{.metadata.uid}'` unchanged = adopted,
not recreated), not by diffing rendered YAML.

Complementary, resource-level tool: `kustomize.toolkit.fluxcd.io/prune: "disabled"`
annotation, for a single object dropped from a still-live Kustomization's output (no CR
deletion involved) — exempts it from pruning regardless of ownership-label timing.
