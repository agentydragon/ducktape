@README.md

## Talos Linux Documentation

Use `https://docs.siderolabs.com/llms.txt` as the entrypoint for Talos Linux
documentation. Fetch it with WebFetch to discover available doc pages.

# Agent Instructions

## CRITICAL: Bootstrap Terminology

"Bootstrap/tear down/recreate the cluster" means:

- **Default scope**: `bazel run //cluster:bootstrap` (single TF root at `terraform/main/`, uses targeted applies)
- **Persistent-auth resources** (keypairs, CSI tokens, signing keys) have `lifecycle { prevent_destroy = true }` in the merged root and are preserved across bootstrap cycles
- Only destroy persistent-auth resources when user explicitly says "including persistent auth" or "from scratch" (requires removing `prevent_destroy` lifecycle rules first)

## CRITICAL: Persistent Auth Protection

**NEVER remove `prevent_destroy` lifecycle rules on persistent-auth resources without explicit user authorization.**

## CRITICAL: Commit Before Reconcile

**NEVER reconcile Flux resources until changes are committed AND pushed.** Flux reads from
the git remote, not your local filesystem.

## CRITICAL: Wiping a backing DB orphans tofu state

`tf/gitops/sso-providers/` (Authentik OAuth2 providers) and `tf/gitops/forgejo-props/`
(Forgejo registry user) both manage objects inside another stateful system whose IDs
they record in tfstate. Wiping the backing DB without also clearing the tofu state
triggers `Unable to read … not found with id N` failures on the next plan.

State now lives in the `tofu-state-db` CNPG cluster (one schema per `Terraform` CR),
not in the old `tfstate-default-*` k8s secrets (those were retired with the
kubernetes-backend migration). Recovery procedure for both this and the historical
secret-based variant: <docs/troubleshooting.md> § "Resource ID Desync After Wiping a
Backing Datastore". Original incident write-up:
<docs/lessons_learned/2026_02_18_authentik_tf_state_lifecycle_coupling.md>.

## CRITICAL: OVH-Only Resilience

DNS and website MUST work with OVH only (without Proxmox). No `proxmox-csi-retain` storage
or Proxmox-pinned nodes. See <docs/plan.md> "OVH-Only Resilience Invariants".

## Primary Directive: Declarative Turnkey Bootstrap

**Goal**: `bazel run //cluster:bootstrap` from committed repo state produces a working cluster.

1. NO imperative patches -- all fixes must be committed configuration
2. Dev loop: `bazel run //cluster:bootstrap` -> verify (single TF root with targeted applies)
3. Debug freely, but solutions MUST be declarative
4. Done = bootstrap->verify passes
5. SSO required for all in-scope applications

### Debugging Broken Bootstrap

Investigate root cause (events, describe, flux kustomization status) and fix declarative config.
Common patterns: missing `dependsOn`, CRD not installed before instance, secret not deployed
before consumer.

## Bootstrap Script

**Only supported method**: `bazel run //cluster:bootstrap`

Handles preflight validation, targeted applies against `terraform/main/` (persistent-auth ->
infrastructure -> full apply), SOPS age key deployment. Requires `dangerouslyDisableSandbox: true`
and `timeout: 600000` (10 min). Takes ~15-20 min.

## Testing

Includes validation scripts, Helm lint, Terraform format/lint/validate. When adding new
Terraform modules, create BUILD.bazel targets for format, lint, and validate.

## Task Delegation

Delegate complex diagnostics and independent workstreams to subagents via the Task tool.

## Operational Context

- **SSH**: `root@atlas` (Proxmox host, key auth). Fallback from wyrm2: `root@10.2.0.2` if nebula DNS isn't up yet.
- **Talos CLI**: Run from cluster directory (direnv provides tools + config)
- **Proxmox API**: Only reachable from VLAN. Use `nodeSelector: topology.kubernetes.io/region: proxmox`.

## Cilium Gateway Status

The public `cluster-gateway` intentionally uses Cilium Gateway API in
`gatewayAPI.hostNetwork.enabled` mode. Envoy binds ports 80/443 directly on the
OVH Kubernetes nodes, and Route 53 wildcard/apex records point at those node IPs.
There is no provider-managed `LoadBalancer`/VIP object for Cilium to report as a
Gateway address.

Because of that exposure model, `gateway-system/cluster-gateway` can report
`Programmed=False` with `AddressNotAssigned` / `Address not ready yet` even while
HTTPRoutes are accepted, Envoy listeners are serving traffic, and public probes
succeed. Do not treat that condition alone as an outage or try to "fix" it by
adding static `Gateway.spec.addresses`; that would not create provider-level
failover. Check HTTPRoute `Accepted`/`ResolvedRefs`, Cilium/Envoy programming,
and blackbox probes against the public node IPs instead. See <docs/plan.md>
"Cilium Gateway API `Programmed=False`" for the full rationale and migration
options.

## Key Files

In `terraform/main/`:

| File                       | Purpose                                        |
| -------------------------- | ---------------------------------------------- |
| `ovh-nodes.tf`             | OVH Kimsufi bare-metal definitions             |
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

Adding annotations/labels to a `*.sops.yaml` Secret needs **no manual re-MAC** —
encrypting recomputes the MAC. But these files set no `mac_only_encrypted`, so
the document MAC covers metadata too: a **raw text edit** of the ciphertext
(without re-encrypting) fails decryption with a `MAC mismatch` (verified —
adding one annotation to a real `*.sops.yaml` Secret breaks `sops -d`). Go
through `sops`: with the cluster age key, `sops <file>` edits and re-MACs in one
step. An **agent** that lacks the key can still _encrypt_ to a rule's recipients
(encryption only needs their public keys), so it authors the whole Secret as
plaintext and `sops -e -i`s it — minting a fresh value for any opaque field it
can't recover (i.e. rotate). Example: `k8s/wayback-cache/token.sops.yaml` carries
the emberstack reflector annotations inline and was (re)authored that way.

### Description Annotations

Add `metadata.annotations.description` to any resource where name + namespace doesn't
make the purpose obvious. Skip for obvious cases.

## Container Images

See <docs/container-images.md> for build/push/tag guide and Flux image automation.

## Agent RBAC Architecture

When adding agent read access to a new service namespace, create a new `agent-rbac/`
directory — never add RoleBindings to `agent-rbac-base` or `shared-rbac`. The full
three-layer split, permission scopes, and the sandbox quota live once in the agent RBAC base
README, transcluded here:

@k8s/agents/agent-rbac-base/README.md

## Storage Selection

**Prefer replicated/distributed storage (`seaweedfs-ovh`) over node-local storage
(`local-path-*`) for app PVCs.** A SeaweedFS volume is served from the SeaweedFS
cluster, not the consuming pod's node, so the pod can reschedule across nodes (drain,
node loss, rebalance) and keep its data. `local-path-*` pins the pod to the one node
that owns the directory — a node failure strands the volume. New OVH-hosted apps default
to `seaweedfs-ovh` for document/media/state volumes.

Use `local-path-*` only when:

- The workload does its **own** replication and must own a raw local disk — **CNPG
  Postgres** (follow <docs/cnpg_conventions.md>; never put a DB on SeaweedFS) and similar
  self-replicating stores.
- A benchmark shows SeaweedFS latency/throughput is inadequate for the workload
  (see <docs/seaweedfs_csi_bench.md>) — record the finding before falling back.

Storage-class table and region notes live in <README.md> § Storage.

## Flux Kustomization Layering

**Never mix HelmReleases with CRD instances in the same Kustomization.**

Layer 1 (CRD operators) → Layer 2 (secrets with ESO) → Layer 3 (app with HelmRelease).
Each layer's `flux-kustomization.yaml` has `dependsOn` on previous.
Violations are caught by the Bazel test `//cluster/validation:test_crd_layering`
(part of the `//cluster/validation:test_*` suite that validates the cluster in CI).

- Flat example: `k8s/scanner/` — single flux-kustomization, all manifests at root
- Grouped example: `k8s/langfuse/{namespace,secrets,db,app}/` — multi-layer with dependsOn

## Reference Documentation

Read these on demand when the task requires them:

- <docs/plan.md> — cluster roadmap, TODO list, suspended services, future directions
- <docs/secrets.md> — SOPS procedures, adding/rotating secrets, age key management
- <docs/bootstrap_dependencies.md> — full dependency graph for bootstrap recovery
- <docs/cnpg_conventions.md> — CloudNativePG rules (2 profiles, storage, region pinning)
- <docs/troubleshooting.md> — diagnosis recipes for Talos, Cilium, secrets, DNS, and
  log retrieval (use Loki for logs of pods that no longer exist — `kubectl logs` can't)
- <docs/lessons_learned/> — past incident postmortems (ESO desync, MTU, hostname loss, etc.)
