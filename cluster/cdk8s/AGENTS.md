# cdk8s generators: don't reach for raw ApiObject

Full design and worked examples: <../docs/cdk8s.md>.

**Never default to `cdk8s.ApiObject` + `JsonPatch` for a whole resource just because
the first typed builder you reach for doesn't cover it.** Before writing one:

1. **Core Kubernetes type** (`Namespace`, `Deployment`, `Service`, `ConfigMap`,
   `ServiceAccount`, `Role`, `Job`, ...): check whether `cdk8s_plus_33` already has a
   typed builder — it covers far more than the handful used so far. Don't assume one
   is missing; import it and try it, or grep its module for the class name, before
   falling back to anything else.
2. **CRD type** (anything with its own `apiVersion` group like `external-secrets.io`,
   `monitoring.coreos.com`, `gateway.networking.k8s.io`): set up real typed bindings
   via `cdk8s_import` (`devinfra/js/cdk8s_import.bzl`), the same way
   `//third_party/flux:kustomization`, `//third_party/prometheus_operator:servicemonitor`,
   `//third_party/gateway_api:httproute`, and `//third_party/external_secrets:externalsecret`
   already do: fetch the upstream CRD YAML verbatim via an `http_file` in
   `MODULE.bazel`, add a `third_party/<name>/BUILD.bazel` calling `cdk8s_import`,
   import the generated dataclasses. This is normal, expected effort for a new CRD,
   not a fallback path — it gets you real schema validation (synth-time errors on a
   bad field) instead of a raw dict that only fails at `kubectl apply` time, if it
   fails at all.

**The one legitimate use of the raw escape hatch** is patching a single field a typed
builder is missing on an object that's otherwise typed — never the whole resource.
Build the resource with its typed constructor, then reach into the specific
already-typed construct: `ApiObject.of(construct).add_json_patch(JsonPatch.add(path,
value))` (`cdk8s_plus_33` non-`ApiObject` constructs like `Deployment` manage one
internally) or `.add_json_patch(...)` directly (`cdk8s.ApiObject` subclasses, e.g.
CRD-generated classes). Example: `Deployment`'s `topologySpreadConstraints`
(`litellm_constructs.py`) — no typed builder exists for it (only an all-or-nothing
`spread: bool` auto-toggle), so it keeps the typed `Deployment` constructor for every
other field and patches only that one in.

A raw `ApiObject` replacing an entire resource is a shortcut that throws away real
validation for the whole object to avoid the CRD-import setup cost. Do the setup.

## Restructuring which Kustomization owns an object: land it in two steps

Converting a directory to cdk8s often merges or splits which Flux `Kustomization`
renders a given object (e.g. folding a directory's separate `namespace`/`credentials`
Kustomizations into one `app` Kustomization, matching the fleet-wide "fold X into app"
pattern). **Never do this in one step.** Deleting the old Kustomization and having the
new one claim the same objects in the same PR is a race, not a handoff: Flux's default
`deletionPolicy: MirrorPrune` means deleting a Kustomization CR (because it's no longer
in the rendered `cluster/k8s/kustomization.yaml` tree) prunes every object it manages,
and nothing guarantees the new Kustomization re-applies and re-claims those objects
(updating their `kustomize.toolkit.fluxcd.io/name` ownership label) before that prune
fires. **Confirmed, not theoretical**: exactly this race deleted `ha-mcp`'s entire
namespace (Deployment, Service, ConfigMap, RBAC, CiliumNetworkPolicy, ServiceMonitor —
zero PVCs involved) when `cluster/k8s/agents/ha-mcp`'s `namespace`/`credentials`
Kustomizations were folded into `app` (#7150). For a stateless object this is a
self-healing blip once the new Kustomization's `dependsOn` is satisfied again; for a
`PersistentVolumeClaim` the same race can be permanent — deleting a PVC can delete the
underlying volume depending on the StorageClass's `reclaimPolicy`, and a freshly
recreated PVC does not automatically rebind to an orphaned `PersistentVolume`.

The fix is a typed field already on `//third_party/flux:kustomization`'s
`KustomizationSpec`, unused anywhere in this repo before this note:
`deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN`. Land the restructuring as two
separate changes:

1. **First**, a small change that sets `deletionPolicy: Orphan` on the _old_
   Kustomization(s) being folded away — nothing else changes. Merge it and let it
   reconcile before proceeding; this is the step that makes the handoff safe, since
   Orphan means Flux leaves the managed objects alone when that CR is deleted, instead
   of racing to prune them.
2. **Only then**, land the actual restructuring: delete the old Kustomization(s), have
   the new one render and claim the same objects. Flux's SSA apply adopts them
   (updates the ownership label) with no race left to lose, because the old
   Kustomization's deletion no longer touches them at all.

This is a live-cluster ownership concern, not a manifest-content one — `kustomize
build` and `flux build --dry-run` render output correctly either way and cannot catch
it, since neither has any visibility into what a real cluster currently owns. Verifying
a restructuring landed safely means checking the live cluster (e.g. `kubectl get <kind>
-n <namespace> -o jsonpath='{.metadata.uid}'` unchanged across the change confirms an
object was adopted, not deleted and recreated), not just diffing rendered YAML.

The complementary, resource-level tool is the `kustomize.toolkit.fluxcd.io/prune:
"disabled"` annotation, for the different failure mode of a single object dropped from
a still-live Kustomization's rendered output (no CR deletion involved) — it exempts
that one resource from pruning regardless of ownership-label timing.
