# cdk8s generators: don't reach for raw ApiObject

Full design and worked examples: <../docs/cdk8s.md>.

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

## Typed affordances (`cdk8s_plus_34`) — use these

| Kind                                                 | Builder                                                                                                                                                                | Reference existing by name                                                                                                                                                                                            |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                                            | `Namespace`                                                                                                                                                            | —                                                                                                                                                                                                                     |
| ServiceAccount                                       | `ServiceAccount`                                                                                                                                                       | `.from_service_account_name(scope, id, name, namespace_name=...)`                                                                                                                                                     |
| Secret                                               | `Secret`, `BasicAuthSecret`, `SshAuthSecret`, `TlsSecret`, `DockerConfigSecret`                                                                                        | `Secret.from_secret_name(scope, id, name)`                                                                                                                                                                            |
| ConfigMap                                            | `ConfigMap`                                                                                                                                                            | `.from_config_map_name(scope, id, name)`                                                                                                                                                                              |
| PVC / PV                                             | `PersistentVolumeClaim`, `PersistentVolume` (+ AWS/Azure/GCE disk subclasses)                                                                                          | `.from_claim_name`, `.from_persistent_volume_name`                                                                                                                                                                    |
| Role / ClusterRole                                   | `Role`, `ClusterRole` — pass real `rules=` to the constructor, see below                                                                                               | `.from_role_name`, `.from_cluster_role_name`                                                                                                                                                                          |
| RoleBinding / ClusterRoleBinding                     | `RoleBinding`, `ClusterRoleBinding` + `.add_subjects(...)`                                                                                                             | subjects: `User.from_name`, `Group.from_name` (any string, incl. `oidc-ksbx-groups:haku`), `ServiceAccount.from_service_account_name`, or a `Role`/`ClusterRole` instance                                             |
| Deployment / StatefulSet / DaemonSet / Job / CronJob | matching `Workload` subclass                                                                                                                                           | —                                                                                                                                                                                                                     |
| Service                                              | `Service`                                                                                                                                                              | —                                                                                                                                                                                                                     |
| NetworkPolicy (stock k8s)                            | `NetworkPolicy`                                                                                                                                                        | —                                                                                                                                                                                                                     |
| Ingress                                              | `Ingress`                                                                                                                                                              | —                                                                                                                                                                                                                     |
| HorizontalPodAutoscaler                              | `HorizontalPodAutoscaler`                                                                                                                                              | —                                                                                                                                                                                                                     |
| Probes / handlers                                    | `Probe`, `Handler`                                                                                                                                                     | `.from_http_get`, `.from_command`, `.from_tcp_socket`, `.from_grpc`                                                                                                                                                   |
| Volumes                                              | `Volume`                                                                                                                                                               | `.from_config_map`, `.from_secret`, `.from_empty_dir`, `.from_persistent_volume_claim`, `.from_host_path`, `.from_nfs`, `.from_csi`, `.from_aws_elastic_block_store`, `.from_azure_disk`, `.from_gce_persistent_disk` |
| Any CRD (own `apiVersion` group)                     | `cdk8s_import`-generated bindings                                                                                                                                      | —                                                                                                                                                                                                                     |
| ResourceQuota                                        | tier 2: `k8s.KubeResourceQuota(metadata=k8s.ObjectMeta(...), spec=k8s.ResourceQuotaSpec(hard={...}))` — values are `k8s.Quantity.from_string(...)`/`.from_number(...)` | —                                                                                                                                                                                                                     |
| LimitRange                                           | tier 2: `k8s.KubeLimitRange(spec=k8s.LimitRangeSpec(limits=[k8s.LimitRangeItem(type=..., max=..., min=..., default=..., default_request=...), ...]))`                  | —                                                                                                                                                                                                                     |
| PodDisruptionBudget                                  | tier 2: `k8s.KubePodDisruptionBudget(spec=k8s.PodDisruptionBudgetSpec(min_available=..., selector=...))`                                                               | —                                                                                                                                                                                                                     |

Anything not in the table: check `dir(cdk8s_plus_34.<Thing>)` (tier 1), then
`dir(cdk8s_plus_34.k8s)` (tier 2 — `Kube<Kind>` for a resource, or the bare struct name
for a field value, e.g. `k8s.<Struct>`); if both are inconclusive, clone
`https://github.com/cdk8s-team/cdk8s-plus` and grep `src/*.ts` (tier 1) or
`src/imports/k8s.ts` (tier 2) for the kind's `export class`/`export interface` before
reaching for a raw dict. Add the result to the table.

### RBAC rules go through the typed constructor, not a `/rules` patch

`Role`/`ClusterRole`'s `rules=` takes `RolePolicyRule`/`ClusterRolePolicyRule`, each
`resources`/`endpoints` a list of real `IApiResource`/`IApiEndpoint` — not dicts. Fully
typed, including `resourceNames`:

- **Resource type, no name scoping**: `ApiResource.<CONSTANT>` (60+ constants — `dir(cdk8s_plus_34.ApiResource)`) or `ApiResource.custom(api_group=..., resource_type=...)` for anything else, subresources included (`"pods/exec"`, `"serviceaccounts/token"`).
- **Scoped to one named object**: pass that kind's own `from_*_name` reference (`Secret.from_secret_name(...)`, `Role.from_role_name(...)`, ...) as the `IApiResource` — its `resource_name` is already wired.
- **Scoped to a name with no typed kind covering it** (e.g. `serviceaccounts/token`): `ApiResource.custom()` never sets `resource_name`. Implement `IApiResource` directly — `@jsii.implements(cdk8s_plus_34.IApiResource)` on a small class with `api_group`/`resource_type`/`resource_name` properties, same as `Secret.from_secret_name` does internally. Needs `@pypi//jsii` as an explicit `BUILD.bazel` dep. Example: `agentplane_constructs.py`'s `_NamedApiResource`.
- **Caveat, not an excuse to go raw**: synthesis emits **one output rule per `IApiResource` entry**, always — `RolePolicyRule(resources=[a, b], ...)` becomes two rules, never one rule listing two resource types (`role.ts`'s `synthesizeRules()`; no typed way around it). RBAC-equivalent (Kubernetes unions all rules), so a hand-written file's rule _grouping_ won't survive conversion unchanged — only its permissions. Expect that diff.

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
