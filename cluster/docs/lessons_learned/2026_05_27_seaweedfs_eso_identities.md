# SeaweedFS s3-config restructure to per-tenant SOPS + ESO assembly

**Date**: 2026-05-27
**Status**: Migrated. Phase 1 (existing tenants) + Phase 2 (augur-assets) both live.

## What changed

`cluster/k8s/seaweedfs/secrets/s3-config.sops.yaml` was a single bulk
SOPS-encrypted Secret containing all SeaweedFS s3 IAM identities. Adding
or rotating any tenant required decrypting every tenant's creds. PRs
were opaque (the JSON-in-string blob couldn't be reviewed).

New layout in `cluster/k8s/seaweedfs/secrets/`:

- `identities/<tenant>.sops.yaml` — one file per tenant; `name` and
  `actions` plaintext, only `accessKey` and `secretKey` encrypted. Each
  file is a two-document YAML: the raw Secret with split stringData,
  plus a per-tenant ExternalSecret that templates the assembled
  identity JSON into a single-key intermediate Secret keyed
  `identity`.
- `secretstore.yaml` — `SecretStore` (kubernetes provider, in-namespace)
  using a dedicated `eso-reader` ServiceAccount.
- `eso-reader-rbac.yaml` — SA + Role + RoleBinding (`secrets: get/list/watch`).
- `externalsecret-s3-config.yaml` — global `ExternalSecret` with
  `dataFrom.find` matching `s3-identity-*-json` Secrets, concatenating
  their `identity` values into the assembled
  `seaweedfs_s3_config.json` blob the Seaweed CR consumes.

The `Seaweed` CR (`cluster/k8s/seaweedfs/cluster/seaweed.yaml`)
gains `s3.annotations: { reloader.stakater.com/auto: "true" }` so the
s3 deployment rolls when the assembled Secret changes.

`.sops.yaml` has a narrow rule for `identities/*.sops.yaml`:
`encrypted_regex: ^(accessKey|secretKey)$`.

## Traps and lessons

### 1. ESO `dataFrom.find` returns each Secret's data as a JSON-encoded **string**, not a map

Naive template fails at runtime with `range can't iterate over
{"alice":"{...}"}`. Correct template parses each value via `fromJson`
before iterating:

```gotemplate
{{- range $sName, $sJson := . -}}
{{- range $k, $v := ($sJson | fromJson) -}}
{{ $v }}
{{- end -}}
{{- end -}}
```

Verified against ESO v1 with the kubernetes provider in a
claude-sandbox scratch spike before the real migration. The naive
single-`range` version doesn't error in the API server's CR validation
— it fails only when ESO tries to render at reconcile time, so a spike
in a scratch namespace is the cheapest way to catch this.

### 2. `creationPolicy: Merge` won't CREATE a missing target

The original cutover plan was to use `creationPolicy: Merge` so ESO
could take over the existing `seaweedfs-s3-config` Secret without
owner-reference conflict. But the cutover commit also deleted
`s3-config.sops.yaml`, so Flux pruned the existing Secret before ESO
could merge. ESO with `Merge` policy refuses to create when the target
is absent (event: `secret will not be created due to
CreationPolicy=Merge`), leaving new s3 pods stuck in
`ContainerCreating` with no config to mount.

Fix: `creationPolicy: Owner`. ESO creates the Secret if missing and
sets an owner reference. Don't try to coordinate "Flux deletes
existing, ESO recreates" — let ESO own it from the start.

### 3. Stakater Reloader annotation via operator's `s3.annotations` lands on the **pod template**, not the Deployment metadata

The Seaweed CR has a `s3.annotations` field, and after applying
`reloader.stakater.com/auto: "true"` there, the annotation ends up on
`spec.template.metadata.annotations` of the operator-generated
Deployment — NOT on the Deployment's own `metadata.annotations`.
Reloader detects either, so this works. But if a future Reloader
version tightens to "only Deployment.metadata.annotations", this will
silently break. Worth a kustomize-patch on the operator-generated
Deployment if that ever happens.

### 4. SeaweedFS auto-creates buckets when an identity's `actions` reference them

When `augur-assets` was added to s3-config with `actions:
["Read:augur-assets", "Write:augur-assets", ...]`, SeaweedFS's s3
gateway saw the bucket-scoped action and auto-created the
`/buckets/augur-assets/` directory in the filer **before** any
`Bucket` CR existed. When the augur-assets Bucket CR landed later, it
went to `phase: Failed / reason: BucketAlreadyExists` because the
operator refuses to adopt buckets it didn't create.

This affects every existing bucket (`attic`, `loki`, `tempo`,
`mimir-blocks`, `mimir-ruler`, `augur-assets`) — none are
operator-managed. Open followup in TODO list ("adopt all buckets into
gitops"). Functionally the buckets work fine; the CR's `status: Failed`
is cosmetic at runtime, but it breaks Flux `Kustomization.spec.wait:
true` which gates on resource health.

### 5. The attic identity isn't in s3-config at all

`attic`'s accessKey lives only in SeaweedFS's dynamic filer metadata —
likely created via the s3 IAM API at some point in the past. Not in
any file-managed config. If the filer's persistent metadata is ever
wiped, attic auth breaks until manually re-created. Tracked alongside
the bucket-adoption work.

## Adding a new tenant going forward

One file:

```yaml
# cluster/k8s/seaweedfs/secrets/identities/<new-tenant>.sops.yaml
apiVersion: v1
kind: Secret
metadata:
  name: s3-identity-<new-tenant>
  namespace: seaweedfs
  labels: { seaweedfs.io/role: s3-identity }
type: Opaque
stringData:
  name: <new-tenant>
  actions: "Read:<bucket>,Write:<bucket>,List:<bucket>,Tagging:<bucket>"
  accessKey: <openssl rand -hex 10>
  secretKey: <openssl rand -hex 20>
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: s3-identity-<new-tenant>-json
  namespace: seaweedfs
  labels: { seaweedfs.io/role: s3-identity-json }
spec:
  refreshInterval: 1m
  secretStoreRef: { kind: SecretStore, name: seaweedfs-identities }
  target:
    name: s3-identity-<new-tenant>-json
    template:
      type: Opaque
      data:
        identity: |
          {"name":"{{ .name }}",
           "credentials":[{"accessKey":"{{ .accessKey }}","secretKey":"{{ .secretKey }}"}],
           "actions":[{{- $first := true -}}
             {{- range splitList "," .actions -}}
             {{- if not $first }},{{ end -}}"{{ . }}"{{- $first = false -}}
             {{- end }}]}
  dataFrom:
    - extract: { key: s3-identity-<new-tenant> }
```

Then `sops -e -i` the file (encrypts only the cred fields per the
`.sops.yaml` rule). Add to `secrets/kustomization.yaml`. Commit + push.
ESO assembles, Reloader rolls the s3 deployment.

No other tenants' creds get touched. PR reviewer sees the plaintext
name + actions and only opaque `ENC[...]` for the cred values.

## References

- ESO ExternalSecret templating pattern: this codebase's
  `cluster/k8s/agents/homeassistant-proxy/proxy-tokens-eso.yaml`
- ESO `dataFrom.find` semantics docs: <https://external-secrets.io/latest/api/spec/#external-secrets.io/v1.ExternalSecretDataFromRemoteRef>
- Stakater Reloader: in cluster as `kube-system/reloader-reloader`
- SeaweedFS s3 IAM docs: <https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API#identity-and-access-management>
