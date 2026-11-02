@README.md

Talos docs: start from `https://docs.siderolabs.com/llms.txt` (WebFetch).

## Invariants

Getting these wrong is destructive or silently corrupting.

- **Persistent auth survives bootstrap.** "Bootstrap/tear down/recreate the cluster" means
  `bazel run //cluster:bootstrap`. Persistent-auth resources (keypairs, CSI tokens, signing
  keys) carry `prevent_destroy`. Never remove a `prevent_destroy` rule unless the user says
  "including persistent auth" or "from scratch".
- **Commit and push before reconciling Flux** — it reads the git remote; reconciling
  uncommitted work applies the old state and looks like a failed change.
- **Wiping a backing DB orphans tofu state.** `tf/gitops/sso-providers/` (Authentik OAuth2
  providers) and `props/deploy/forgejo/tofu/` (Forgejo registry user) record IDs of objects
  in other stateful systems; wiping that DB without clearing tofu state (CNPG
  `tofu-state-db-ovh`, one schema per `Terraform` CR) yields
  `Unable to read … not found with id N`. Recovery: <docs/troubleshooting.md>
  § "Resource ID Desync After Wiping a Backing Datastore".
- **DNS and website must survive OVH-only** (README § "OVH-Only Resilience Invariants").

## Bootstrap

- Done = `bazel run //cluster:bootstrap` from committed state → working cluster. Debug
  freely, but every fix lands as committed config, never an imperative patch. SSO is
  required for all in-scope apps.
- Bootstrap is the only supported method: preflight, targeted applies against
  `terraform/main/` (persistent-auth → infrastructure → full), SOPS age key deployment. Run
  with `dangerouslyDisableSandbox: true`, `timeout: 600000`; takes ~15–20 min.
- New Terraform modules get BUILD targets for format, lint, validate.
- Changing bootstrap prerequisites or recovery of its infra/access material: update
  <docs/bootstrap_dependencies.md> (app credentials, TF runners, service deps are
  documented with their owning component).

## Access

- SSH `root@atlas` (key auth); from wyrm2 before Nebula DNS is up, `root@10.2.0.2`.
- Run `talosctl` from `cluster/` (direnv supplies tools + config).
- Proxmox API is reachable only from the VLAN: pods need
  `nodeSelector: topology.kubernetes.io/region: proxmox`.

## Key files (`terraform/main/`)

`ovh-nodes.tf` (OVH bare metal), `home-nodes.tf` (home Talos workers), `proxmox-nodes.tf`
(Proxmox VMs), `talos-machine-secrets.tf` (ephemeral), `cilium.tf`, `infrastructure.tf`
(firewall, Talos bootstrap, registry mirrors), `persistent-auth.tf` (`prevent_destroy`),
`nebula.tf` (per-node config + endpoint drift check).

## Secrets

Procedures: <docs/secrets.md>. SSO: <docs/sso.md>.

**SOPS files here set no `mac_only_encrypted`**, so the MAC covers metadata: a raw text
edit (e.g. adding an annotation) fails with `MAC mismatch`. Edit via `sops <file>` with the
cluster age key; without it, re-author the whole Secret as plaintext and `sops -e -i`,
minting fresh values for fields you can't recover (i.e. rotate).

## Manifest conventions

- Add `metadata.annotations.description` where name + namespace doesn't make the purpose
  obvious.
- YAML consumed by `configMapGenerator` stays `.yaml` (never disguised as `.txt`): add
  `cluster-manifest-ignored=true` in root `.gitattributes`, add the path to the kubeconform
  exclusion in `.pre-commit-config.yaml`, and mark the file as a `configMapGenerator` input.
- A `HelmRepository`/`GitRepository` consumed by exactly one `HelmRelease` in the same
  component directory: put it first in `helmrelease.yaml` and list only that file in
  `kustomization.yaml`. Keep separate if reused, independently owned, or a consumer needs
  a single-document file.
- Agent read access to a new namespace: a new `agent-rbac/` directory, never RoleBindings in
  `agent-rbac-base` or `shared-rbac` (<docs/agent_rbac.md>).
- CiliumNetworkPolicy for Gateway API backends: <docs/cilium_network_policy.md>.
  Postmortems: <docs/lessons_learned/>.

## Container Images

Guide: <docs/container-images.md>.

- **No YAML flow mappings (`{a: 1}`) in a manifest
  carrying an `$imagepolicy` marker** — `ImageUpdateAutomation` re-serializes the document
  differently from prettier and pushes to `devel` as `chore: update images [skip ci]`
  without CI, turning `devel` red on every image update. Use block style.
- One `ImageRepository` + one `ImagePolicy` not reused elsewhere: one `<name>-image.yaml`,
  repository first. `ImageUpdateAutomation` gets its own file. A Receiver reference alone
  doesn't force a split.

## Storage Selection

App PVCs default to `seaweedfs-ovh` (not node-pinned). Use `local-path-*` only for:

- self-replicating stores needing a raw local disk — CNPG Postgres
  (<docs/cnpg_conventions.md>; never a DB on SeaweedFS);
- workloads a recorded benchmark shows SeaweedFS can't serve (<docs/seaweedfs_csi_bench.md>).

Before operating on SeaweedFS `Bucket`/`S3Identity`/`S3Credentials` objects, read
<skills/seaweed_operator/SKILL.md>.
For retirement, follow <docs/runbooks/seaweedfs_s3_retirement.md>; delete the
identity only after credential and other IAM writers have finalized.

## Flux Kustomizations

- All Flux `Kustomization`s are generated into `k8s/flux/kustomizations.k8s.yaml`, applied by
  the bootstrap `flux-system` Kustomization (`k8s/flux/flux-system/gotk-sync.yaml`). Root
  `k8s/kustomization.yaml` is applied by nothing; it is the runfiles anchor validation tests
  use. A directory's `kustomization.yaml` lists only what Flux applies at `spec.path`, never
  its own Flux Kustomization.
- **Custom resources must transitively depend on the Kustomization installing their
  CRDs/operator**; an operator HelmRelease and its instances go in separate Kustomizations.
  Enforced by `validate_operator_dependencies` in
  `//cluster/validation:test_cluster_integration` (regression: `:test_crd_layering`).

### Migrating stateful Flux Kustomizations

- Never rename/remove a Kustomization owning a CNPG Cluster, PVC, Bucket, or other persistent
  resource in the same unrehearsed reconciliation that adds its replacement (`prune: true`
  deletes the old inventory). First suspend the old owner and patch it live to
  `spec.prune: false`, `spec.deletionPolicy: Orphan`; verify, reconcile the replacement,
  confirm identical resource/PVC identities, then clean up. `suspend` alone is not safe.
- **Operators may copy `kustomize.toolkit.fluxcd.io/*` labels into Service selectors**, so
  an ownership change can drop endpoints before Pods roll (seen: Opstree Redis operator,
  Langfuse Valkey). Inspect generated Services' selectors and Pod labels first; if they
  participate, hand off an explicit label-transition procedure. Verify ready EndpointSlice
  backends and a connection from the consumer — identical manifests and Flux readiness
  don't prove continuity. See
  [the incident](docs/lessons_learned/2026_09_20_redis_operator_flux_labels.md).

### Parked applications

- A decommissioned or indefinitely suspended app whose source ducktape does **not** own
  (third-party image/chart/tool) moves whole to `cluster/parked/<name>/`, layout unchanged.
  Its Flux Kustomization stays in the central chart with `spec.suspend: true` and annotation
  `ducktape.org/parked: "true"` (required for `cluster/parked/` paths by
  `test_flux_kustomizations_under_parked_path_are_annotated` in
  `cdk8s/test_generate_manifests.py`).
- Ducktape-owned code (`<project>/deploy/`, e.g. `props/deploy/`) keeps its manifests beside
  its source, active or suspended.
- `cluster/parked` is outside the manifest roots: `cluster/validation` skips it, image
  automation doesn't update its `$imagepolicy` pins.
- Full unwire only when explicitly asked: remove its Kustomization from the central chart,
  keep the manifests. Revive by re-adding it without the annotation/suspension and moving
  the directory under a manifest root (`cdk8s/manifest_roots.py`).
- Inventory and reasons: <docs/decisions.md> § "Parked application manifests".

## Generated manifests

Everything under `cluster/generated`, every `*.k8s.yaml` under `cluster/k8s` or
`cluster/parked`, and files `.gitattributes` marks `linguist-generated=true` there are
`bb run //cluster/cdk8s:generate_manifests` output: change the generator and regenerate
(`//cluster/cdk8s:test_generate_manifests` fails on drift). A directory is under
`cluster/generated` exactly when the generator writes all of it. Layout rules above bind
generated directories only where the generator has a knob. Invariants over generated
objects are fleet rules or tests beside the generator, never new `cluster/validation/` tests
reading committed output (that package keeps whole-graph checks and hand-written-directory
tests). Conventions: <cdk8s/AGENTS.md>; design and what stays hand-written: <docs/cdk8s.md>.
