@README.md

## Talos Linux Documentation

Use `https://docs.siderolabs.com/llms.txt` as the entrypoint for Talos Linux
documentation. Fetch it with WebFetch to discover available doc pages.

# Agent Instructions

## Invariants

These are destructive or silently corrupting if you get them wrong. Everything else in
this file is guidance you can apply judgment to.

### Persistent auth survives bootstrap

"Bootstrap/tear down/recreate the cluster" defaults to `bazel run //cluster:bootstrap`
(single TF root at `terraform/main/`, targeted applies). Persistent-auth resources
(keypairs, CSI tokens, signing keys) carry `lifecycle { prevent_destroy = true }` and are
preserved across bootstrap cycles.

**Never remove a `prevent_destroy` lifecycle rule without explicit user authorization.**
Destroying persistent auth requires the user to say "including persistent auth" or "from
scratch"; removing the lifecycle rules is part of that request, never a prerequisite you
satisfy on your own.

### Commit and push before reconciling Flux

Flux reads from the git remote, not your local filesystem — reconciling uncommitted work
applies the previous state and reads as a failed change.

### Wiping a backing DB orphans tofu state

`tf/gitops/sso-providers/` (Authentik OAuth2 providers) and `tf/gitops/forgejo-props/`
(Forgejo registry user) both manage objects inside another stateful system whose IDs
they record in tfstate. Wiping the backing DB without also clearing the tofu state
triggers `Unable to read … not found with id N` failures on the next plan. State lives
in the `tofu-state-db` CNPG cluster (one schema per `Terraform` CR). Recovery:
<docs/troubleshooting.md> § "Resource ID Desync After Wiping a Backing Datastore".

### DNS and website must survive OVH-only

They must work without Proxmox: no Proxmox-pinned storage (`lvm-proxmox-*`,
`local-path-proxmox`) and no Proxmox-pinned nodes. See <docs/plan.md> "OVH-Only Resilience
Invariants".

## Primary Directive: Declarative Turnkey Bootstrap

**Goal**: `bazel run //cluster:bootstrap` from committed repo state produces a working cluster.

1. NO imperative patches -- all fixes must be committed configuration
2. Dev loop: `bazel run //cluster:bootstrap` -> verify (single TF root with targeted applies)
3. Debug freely, but solutions MUST be declarative
4. Done = bootstrap->verify passes
5. SSO required for all in-scope applications

## Bootstrap Script

**Only supported method**: `bazel run //cluster:bootstrap`

Handles preflight validation, targeted applies against `terraform/main/` (persistent-auth ->
infrastructure -> full apply), SOPS age key deployment. Requires `dangerouslyDisableSandbox: true`
and `timeout: 600000` (10 min). Takes ~15-20 min.

New Terraform modules get BUILD.bazel targets for format, lint, and validate.

## Operational Context

- **SSH**: `root@atlas` (Proxmox host, key auth). Fallback from wyrm2: `root@10.2.0.2` if nebula DNS isn't up yet.
- **Talos CLI**: Run from cluster directory (direnv provides tools + config)
- **Proxmox API**: Only reachable from VLAN. Use `nodeSelector: topology.kubernetes.io/region: proxmox`.

## Cilium Gateway Status

The public `cluster-gateway` intentionally uses Cilium Gateway API in
`gatewayAPI.hostNetwork.enabled` mode: Envoy binds 80/443 directly on the OVH nodes and
Route 53 records point at those node IPs, so there is no provider `LoadBalancer`/VIP for
Cilium to report as a Gateway address. `gateway-system/cluster-gateway` can therefore
report `Programmed=False` (`AddressNotAssigned`) even while HTTPRoutes are accepted and
public probes succeed. Do not treat that condition alone as an outage or "fix" it by
adding static `Gateway.spec.addresses` (that would not create provider-level failover) —
check HTTPRoute `Accepted`/`ResolvedRefs`, Cilium/Envoy programming, and blackbox probes
against the public node IPs instead. Full rationale and migration options:
<docs/plan.md> "Cilium Gateway API `Programmed=False`".

## Key Files

In `terraform/main/`:

| File                       | Purpose                                        |
| -------------------------- | ---------------------------------------------- |
| `ovh-nodes.tf`             | OVH Kimsufi bare-metal definitions             |
| `home-nodes.tf`            | Home bare-metal Talos worker definitions       |
| `proxmox-nodes.tf`         | Proxmox VM definitions                         |
| `talos-machine-secrets.tf` | Machine secrets (ephemeral)                    |
| `cilium.tf`                | CNI configuration                              |
| `infrastructure.tf`        | Firewall, Talos bootstrap, registry mirrors    |
| `persistent-auth.tf`       | Keypairs, tokens (`prevent_destroy` lifecycle) |
| `nebula.tf`                | Per-node Nebula config + endpoint drift check  |

At repo root:

| File               | Purpose                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `nebula-mesh.json` | Mesh host roster (SSOT). Add/remove/re-IP: <docs/mesh_membership.md> |

## SSO

See <docs/sso.md> for secret flow, proxy NetworkPolicy template, blueprint tombstone rules.

## Secrets

See <docs/secrets.md> for SOPS procedures, adding/rotating secrets, age key management.

**Keep <docs/bootstrap_dependencies.md> up to date** when adding/removing/changing secrets,
SOPS files, tofu resources, or external credential requirements.

### Annotating a SOPS-encrypted Secret

These files set no `mac_only_encrypted`, so the document MAC covers metadata: a **raw
text edit** of the ciphertext (adding an annotation without re-encrypting) fails
decryption with `MAC mismatch`. Go through `sops`: with the cluster age key,
`sops <file>` edits and re-MACs in one step. An agent without the key can still
_encrypt_ to a rule's recipients, so it authors the whole Secret as plaintext and
`sops -e -i`s it — minting a fresh value for any opaque field it can't recover (i.e.
rotate).

### Description Annotations

Add `metadata.annotations.description` to any resource where name + namespace doesn't
make the purpose obvious. Skip for obvious cases.

## Container Images

See <docs/container-images.md> for build/push/tag guide and Flux image automation.

**Gotcha — no YAML flow mappings (`{a: 1, b: 2}`) in a manifest carrying an
`$imagepolicy` marker.** `ImageUpdateAutomation` re-serialises the whole document
when it rewrites a tag, and its emitter writes `{a: 1}` where prettier writes
`{ a: 1 }`. Since that rewrite lands on `devel` as `chore: update images
[skip ci]` — no PR, no CI — the branch goes red _after_ your green PR merged, and
re-reds on every image update. Use block style in those files.

## Agent RBAC Architecture

When adding agent read access to a new service namespace, create a new `agent-rbac/`
directory — never add RoleBindings to `agent-rbac-base` or `shared-rbac`. The full
three-layer split, permission scopes, and the sandbox quota:
<k8s/agents/agent-rbac-base/README.md>.

## Storage Selection

**Prefer replicated storage (`seaweedfs-ovh`) over node-local (`local-path-*`) for app
PVCs** — SeaweedFS volumes are not node-pinned, so pods reschedule across drain, node
loss, and rebalance. New OVH-hosted apps default to `seaweedfs-ovh` for
document/media/state volumes. Use `local-path-*` only when:

- The workload does its **own** replication and must own a raw local disk — **CNPG
  Postgres** (follow <docs/cnpg_conventions.md>; never put a DB on SeaweedFS) and similar
  self-replicating stores.
- A benchmark shows SeaweedFS latency/throughput is inadequate for the workload
  (see <docs/seaweedfs_csi_bench.md>) — record the finding before falling back.

## Flux Kustomization Wiring

Flux `Kustomization` resources (`flux-kustomization.yaml`) are applied from the **root**
`cluster/k8s/kustomization.yaml`. A directory's own `kustomization.yaml` lists only the
manifests Flux applies at `spec.path` — **never its `flux-kustomization.yaml`**, which
would apply it redundantly.

**Never mix HelmReleases with CRD instances in the same Kustomization.**
Layer 1 (CRD operators) → Layer 2 (secrets with ESO) → Layer 3 (app with HelmRelease),
each layer's `flux-kustomization.yaml` with `dependsOn` on the previous. Violations are
caught by `//cluster/validation:test_crd_layering`.

- Flat example: `k8s/scanner/` — single flux-kustomization, all manifests at root
- Grouped example: `k8s/langfuse/{namespace,secrets,db,app}/` — multi-layer with dependsOn

## Reference Documentation

Read on demand:

- <docs/cilium_network_policy.md> — CiliumNetworkPolicy patterns for Gateway API backends (`fromEntities: [ingress]`, not host/remote-node)
- <docs/lessons_learned/> — past incident postmortems (ESO desync, MTU, hostname loss, etc.)
