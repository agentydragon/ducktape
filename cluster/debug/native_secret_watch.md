# Native Crossplane required-resource watch proof

**Status: verified 2026-10-30.** Crossplane `v2.4.2` watched a namespaced
Secret required by a cluster-scoped `WatchProbe`, reran its Composition within
one second, and propagated the value through a new output-only Workspace
managed by the existing provider-opentofu. The Workspace spec and identity remained stable through updates,
Secret recreation, and an explicitly approved Crossplane core restart.

## Live results

The function response TTL was `1h` for every observation, so the results did
not depend on the default one-minute function TTL expiring.

| Probe                             | Workspace annotation updated | OpenTofu output updated | Identity/spec                      |
| --------------------------------- | ---------------------------: | ----------------------: | ---------------------------------- |
| Secret marker update 1            |                      0.443 s |                 9.214 s | Same UID; generation 2             |
| Secret marker update 2            |                      0.413 s |                 3.887 s | Same UID; generation 2             |
| Secret delete/recreate            |                      0.342 s |                 3.381 s | Same Workspace UID; new Secret UID |
| After approved Crossplane restart |                      0.409 s |                 3.431 s | Same UID; generation 2             |

The Workspace spec SHA-256 stayed
`1b0dd2ed2493d8ac550e3523e0fcb4e6573ed8ea5acaf392184c78be6b24d0ac` through
the runs. During the interval where the Secret was absent, the Composition
failed safely and the existing Workspace had no deletion timestamp. Recreating
the Secret under the same name updated its UID/resourceVersion annotations and
the OpenTofu output without replacing the Workspace. All 16 production
Workspace UIDs were unchanged. The live spike resources have been removed; the
dedicated output-only PostgreSQL schema `native_secret_watch_spike` was retained.

## Configuration that passed

The function package was pinned to
[`function-go-templating:v0.13.2`](https://github.com/crossplane-contrib/function-go-templating/releases/tag/v0.13.2).
Its inline input set `ttl: 1h`. The Composition used Crossplane's bootstrap
requirement for exactly `Secret/input` in `native-secret-watch-spike`:

```yaml
requirements:
  requiredResources:
    - requirementName: probe-secret
      apiVersion: v1
      kind: Secret
      name: input
      namespace: native-secret-watch-spike
```

The template read the required resource, failed if it was missing or ambiguous,
and copied only its UID and resourceVersion into Workspace annotations:

```gotemplate
{{- $items := (index .requiredResources "probe-secret").items -}}
{{- if ne (len $items) 1 -}}
{{- fail "required input Secret must be present exactly once" -}}
{{- end -}}
{{- $secret := (index $items 0).resource -}}
watch-spike.example.org/input-uid: {{ $secret.metadata.uid | quote }}
watch-spike.example.org/input-resource-version: {{ $secret.metadata.resourceVersion | quote }}
```

The composed Workspace used `source: Inline`, external name
`native-secret-watch-spike`, and the `native-secret-watch-spike` ProviderConfig.
Its static module (`observer.tf` in the transient fixture) read the Secret directly with the Kubernetes provider and exposed
`nonsensitive(data.kubernetes_secret.input.data["marker"])` as
`observed_marker`. It used `deletionPolicy: Delete`, management policies
`Observe`, `Create`, `Update`, and `Delete`, `initArgs: [-reconfigure]`, and
`PGPASSWORD` from the existing `flux-system/tofu-state-db-credentials` Secret.

The ProviderConfig used the PostgreSQL backend at
`tofu-state-db-ovh-rw.tofu-state.svc:5432/tfstate` with the dedicated
`native_secret_watch_spike` schema. The initial local/default-workspace setup
failed with `No state file was found`. The successful setup used PostgreSQL and
the explicitly named workspace; the individual contribution of those changes
was not isolated. The state DB password stayed in a Kubernetes Secret; no
credential value was placed in the spike manifests or this report. The
`marker` value was disposable non-sensitive test data.

## Reproduce the trigger

The live setup was removed after the proof, so these snippets document the
working shape rather than provide a complete apply-ready fixture. Recreate its isolated Namespace,
non-sensitive `Secret/input`, ProviderConfig, cluster-scoped XRD, Composition,
and XR using the configuration above and the source links below. Keep the
Function healthy before creating the XR, and wait for the XRD to become
Established before creating its Composition or XR.

Change only the test marker, then inspect the Workspace annotation and output:

```bash
kubectl -n native-secret-watch-spike patch secret input --type=merge \
  -p '{"stringData":{"marker":"probe-two"}}'
kubectl get workspace.opentofu.upbound.io native-secret-watch-spike -o json \
  | jq '{uid: .metadata.uid, generation: .metadata.generation, annotations: .metadata.annotations, spec: .spec, observed_marker: .status.atProvider.outputs.observed_marker}'
```

For the recreation case, delete `Secret/input` and wait for the XR to report a
Composition error. Confirm the Workspace remains present with the same UID and
spec, then recreate `Secret/input` with a different test marker and verify the
Workspace annotations and `status.atProvider.outputs.observed_marker` advance.
Do not return an empty desired resource list while the Secret is absent; that
would ask Crossplane to garbage-collect the Workspace. A Crossplane core restart
is optional and disruptive; repeat that part only with explicit approval.

Afterward, delete the XR and wait for the Delete-managed Workspace to disappear
before deleting the ProviderConfig, Composition, XRD, Function, Secret, and
Namespace. Keep the dedicated PostgreSQL schema; do not remove state storage as
part of fixture cleanup.

## Why this is event-driven

Crossplane `v2.4.2` declares bootstrap `requirements.requiredResources` in the
[Composition CRD](https://github.com/crossplane/crossplane/blob/v2.4.2/cluster/crds/apiextensions.crossplane.io_compositions.yaml#L163-L214).
The [function composer](https://github.com/crossplane/crossplane/blob/v2.4.2/internal/controller/apiextensions/composite/composition_functions.go#L405-L417)
fetches and tracks those references, and the
[XR reconciler](https://github.com/crossplane/crossplane/blob/v2.4.2/internal/controller/apiextensions/composite/reconciler.go#L809-L823)
starts dependency watches for their GVKs. The
[dependency tracker](https://github.com/crossplane/crossplane/blob/v2.4.2/internal/controller/apiextensions/composite/dependency/tracker.go#L246-L266)
routes a namespaced name match back to the dependent XR. The
[function runner](https://github.com/crossplane-contrib/function-go-templating/blob/v0.13.2/fn.go#L91-L95)
applies the explicit one-hour TTL. The watch is kind-wide; this proof does not
show namespace/name-scoped informer RBAC. No Kubernetes provider was installed;
the existing Crossplane core identity already had cluster-wide Secret
list/watch permission.

## Minimal trigger and inspection

The OpenTofu module used by the Workspace was:

```hcl
data "kubernetes_secret" "input" {
  metadata {
    name      = "input"
    namespace = "native-secret-watch-spike"
  }
}

output "observed_marker" {
  value = nonsensitive(data.kubernetes_secret.input.data["marker"])
}
```

## In-place adoption check

Before the production implementation, a second disposable Workspace was created
and made Ready without a composite owner. A WatchProbe then adopted it through
the same Composition. Its UID and spec hash stayed unchanged. A subsequent
Secret update reached its annotations in 0.452 seconds and OpenTofu output in
3.506 seconds, with a one-hour function TTL. This checked adoption independently
of the earlier create-from-scratch proof. The second canary was removed too.
