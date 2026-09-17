# cdk8s generators: don't reach for raw ApiObject

Full design and worked examples: <../docs/cdk8s.md>.

**Rule**: never build a resource with raw `cdk8s.ApiObject` + `JsonPatch` when a typed
`cdk8s_plus_33` builder exists, and never leave a CRD as a raw `ApiObject` instead of
generating real bindings via `cdk8s_import` (`devinfra/js/cdk8s_import.bzl`;
`//third_party/{flux,prometheus_operator,gateway_api,external_secrets,cilium}` are the
examples). Typed builders give synth-time validation; raw dicts fail only at
`kubectl apply`, if at all. Check the table below — built by cloning
`cdk8s-team/cdk8s-plus` and reading `src/*.ts`, not guessing from `dir()` — before
writing any raw `ApiObject`. Extend it the same way when a new construct comes up.

**The only legitimate raw usage** is patching one field a typed builder is missing, on
an object that's otherwise fully typed — never the whole resource:
`ApiObject.of(construct).add_json_patch(JsonPatch.add(path, value))` (typed non-`ApiObject`
constructs like `Deployment`) or `.add_json_patch(...)` directly (`cdk8s.ApiObject`
subclasses, e.g. CRD-generated classes).

## Typed affordances (`cdk8s_plus_33`) — use these

| Kind                                                 | Builder                                                                         | Reference existing by name                                                                                                                                                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                                            | `Namespace`                                                                     | —                                                                                                                                                                                                                     |
| ServiceAccount                                       | `ServiceAccount`                                                                | `.from_service_account_name(scope, id, name, namespace_name=...)`                                                                                                                                                     |
| Secret                                               | `Secret`, `BasicAuthSecret`, `SshAuthSecret`, `TlsSecret`, `DockerConfigSecret` | `Secret.from_secret_name(scope, id, name)`                                                                                                                                                                            |
| ConfigMap                                            | `ConfigMap`                                                                     | `.from_config_map_name(scope, id, name)`                                                                                                                                                                              |
| PVC / PV                                             | `PersistentVolumeClaim`, `PersistentVolume` (+ AWS/Azure/GCE disk subclasses)   | `.from_claim_name`, `.from_persistent_volume_name`                                                                                                                                                                    |
| Role / ClusterRole                                   | `Role`, `ClusterRole` — pass real `rules=` to the constructor, see below        | `.from_role_name`, `.from_cluster_role_name`                                                                                                                                                                          |
| RoleBinding / ClusterRoleBinding                     | `RoleBinding`, `ClusterRoleBinding` + `.add_subjects(...)`                      | subjects: `User.from_name`, `Group.from_name` (any string, incl. `oidc-ksbx-groups:haku`), `ServiceAccount.from_service_account_name`, or a `Role`/`ClusterRole` instance                                             |
| Deployment / StatefulSet / DaemonSet / Job / CronJob | matching `Workload` subclass                                                    | —                                                                                                                                                                                                                     |
| Service                                              | `Service`                                                                       | —                                                                                                                                                                                                                     |
| NetworkPolicy (stock k8s)                            | `NetworkPolicy`                                                                 | —                                                                                                                                                                                                                     |
| Ingress                                              | `Ingress`                                                                       | —                                                                                                                                                                                                                     |
| HorizontalPodAutoscaler                              | `HorizontalPodAutoscaler`                                                       | —                                                                                                                                                                                                                     |
| Probes / handlers                                    | `Probe`, `Handler`                                                              | `.from_http_get`, `.from_command`, `.from_tcp_socket`, `.from_grpc`                                                                                                                                                   |
| Volumes                                              | `Volume`                                                                        | `.from_config_map`, `.from_secret`, `.from_empty_dir`, `.from_persistent_volume_claim`, `.from_host_path`, `.from_nfs`, `.from_csi`, `.from_aws_elastic_block_store`, `.from_azure_disk`, `.from_gce_persistent_disk` |
| Any CRD (own `apiVersion` group)                     | `cdk8s_import`-generated bindings                                               | —                                                                                                                                                                                                                     |

### RBAC rules go through the typed constructor, not a `/rules` patch

`Role`/`ClusterRole`'s `rules=` takes `RolePolicyRule`/`ClusterRolePolicyRule`, each
`resources`/`endpoints` a list of real `IApiResource`/`IApiEndpoint` — not dicts. Fully
typed, including `resourceNames`:

- **Resource type, no name scoping**: `ApiResource.<CONSTANT>` (60+ constants — `dir(cdk8s_plus_33.ApiResource)`) or `ApiResource.custom(api_group=..., resource_type=...)` for anything else, subresources included (`"pods/exec"`, `"serviceaccounts/token"`).
- **Scoped to one named object**: pass that kind's own `from_*_name` reference (`Secret.from_secret_name(...)`, `Role.from_role_name(...)`, ...) as the `IApiResource` — its `resource_name` is already wired.
- **Scoped to a name with no typed kind covering it** (e.g. `serviceaccounts/token`): `ApiResource.custom()` never sets `resource_name`. Implement `IApiResource` directly — `@jsii.implements(cdk8s_plus_33.IApiResource)` on a small class with `api_group`/`resource_type`/`resource_name` properties, same as `Secret.from_secret_name` does internally. Needs `@pypi//jsii` as an explicit `BUILD.bazel` dep. Example: `agentplane_constructs.py`'s `_NamedApiResource`.
- **Caveat, not an excuse to go raw**: synthesis emits **one output rule per `IApiResource` entry**, always — `RolePolicyRule(resources=[a, b], ...)` becomes two rules, never one rule listing two resource types (`role.ts`'s `synthesizeRules()`; no typed way around it). RBAC-equivalent (Kubernetes unions all rules), so a hand-written file's rule _grouping_ won't survive conversion unchanged — only its permissions. Expect that diff.

Anything not in the table above: check `dir(cdk8s_plus_33.<Thing>)` first; if
inconclusive, clone `https://github.com/cdk8s-team/cdk8s-plus` and grep `src/*.ts` for
the kind's `export class` before reaching for `ApiObject`. Add the result to the table.

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
