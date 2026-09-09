# Cluster Bootstrap Dependencies

Prerequisites for `bazel run //cluster:bootstrap` and recovery of its infrastructure
and access material. Application deployment and credential inventories live with
their owning components. The layers below follow the bootstrap handoff to Flux
and subsequent worker integration.

## Dependency Graph

```text
L0  External Credentials ─────────────────────────────────────────────────
    (operator decryption identity, infrastructure API access)
        │
L1  SOPS Secrets in Git ─────────────────────────────────────────────────
    (Nebula identities, cluster age key, infrastructure credentials)
        │  encrypted with: admin age key (L0)
        │
L2  Persistent Auth (tofu state) ────────────────────────────────────────
    (Proxmox users/tokens, Talos machine secrets)
        │  reads: L1 SOPS files
        │  writes to: Proxmox API, Terraform state
        │
L3  Infrastructure ──────────────────────────────────────────────────────
    (OVH Kimsufi nodes, Proxmox VM, kubeconfig)
        │  reads: L0 tokens, L1 Nebula identities, L2 persistent auth
        │
L4  Cluster Networking ──────────────────────────────────────────────────
    (Gateway API CRDs, Cilium CNI, node readiness, Flux decryption key)
        │
L5  Flux GitOps ─────────────────────────────────────────────────────────
    (flux-system namespace, Flux controllers, git sync)
        │  reads: L1 cluster age key
        │  decrypts: all k8s/**/*.sops.yaml
        │
L6  Cluster Services ────────────────────────────────────────────────────
    (Flux-managed operators and applications)
        │  reads: component-owned SOPS credentials
        │
L7  NixOS Worker Integration ────────────────────────────────────────────
    (wyrm2, rugged join cluster via Nebula + kubelet bootstrap)
        │  reads: L1 nebula CA (certs generated manually, stored in SOPS)
```

## L0: External Credentials

The operator needs a decryption identity and network access to the infrastructure
APIs before applying. API credentials are SOPS-backed inputs in L1.

| Credential            | Source                                          | Storage      | Consumed By                    |
| --------------------- | ----------------------------------------------- | ------------ | ------------------------------ |
| Admin age private key | Derived from `~/.ssh/id_ed25519` via ssh-to-age | User SSH key | Decrypt authorized SOPS inputs |

## L1: SOPS Secrets in Git

These infrastructure inputs must be decryptable by the bootstrap operator.
Cluster-side SOPS files also need the Flux decryption identity as a recipient.

| File                                      | Contents                              | Depends On                                               | Depended On By                                                                             |
| ----------------------------------------- | ------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `secrets/nebula/ca.crt`                   | Nebula CA public cert (plaintext)     | None                                                     | L3: Talos machine configuration; L7: NixOS workers                                         |
| `secrets/nebula/ca.sops.key`              | Nebula CA private key (SOPS bin)      | Admin age key                                            | Explicit certificate rotation only                                                         |
| `secrets/nebula/*.sops.key`               | Nebula host private keys (binary)     | Operator recipients for Talos; host recipients for NixOS | L3: Talos machine configuration; L7: NixOS worker mesh                                     |
| `secrets/nebula/*.crt`                    | Nebula host public certs (plain)      | None                                                     | L3: Talos machine configuration; L7: NixOS worker mesh                                     |
| `secrets/shared/cluster-secrets-age.yaml` | Age keypair (private + public)        | Admin age key                                            | L5: Flux SOPS decryption (`sops-age-cluster-secrets` k8s secret)                           |
| `secrets/shared/cluster-tokens.yaml`      | Proxmox API token                     | Admin age key + user age keys                            | `.envrc` -> `PROXMOX_VE_API_TOKEN`                                                         |
| `secrets/ovh-credentials.sops.yaml`       | OVH API credentials (AK/AS/CK)        | Admin age key + user age keys + cluster-secrets          | `terraform.tf` OVH provider → `ovh-nodes.tf` Kimsufi provisioning                          |
| `secrets/ovh-rescue-ssh.sops.yaml`        | OVH rescue-mode SSH keypair (ED25519) | Admin age key + user age keys + cluster-secrets          | `ovh_dedicated_server.kimsufi` `rescue_ssh_key`; rescue `remote-exec` during Talos install |
| `secrets/k8s-ca.crt`                      | K8s cluster CA cert (plaintext)       | None                                                     | L7: kubelet TLS on NixOS workers                                                           |
| `secrets/k8s-worker.yaml`                 | k8s bootstrap token                   | Admin age key                                            | L7: kubelet TLS bootstrap on NixOS workers                                                 |

**If nebula CA is lost**: Generate new CA with `nebula-cert ca`, write cert
to `secrets/nebula/ca.crt`, encrypt key to `secrets/nebula/ca.sops.key`.
Then explicitly regenerate and persist every node certificate/key under
`secrets/nebula/`, apply the Talos machine configuration, and update all NixOS
worker Nebula files (L7).

**If `secrets/shared/cluster-secrets-age.yaml` is lost**: regenerate and redeploy per
<secrets.md> § "Rotating the Cluster Age Key".

**If `secrets/shared/cluster-tokens.yaml` is lost**: Re-enter the Proxmox token
(Proxmox UI -> API Tokens), SOPS-encrypt to `secrets/shared/cluster-tokens.yaml`,
commit. `.envrc` picks it up automatically.

**If `secrets/ovh-credentials.sops.yaml` is lost**: Create new API credentials at
`https://api.us.ovhcloud.com/createToken/` (GET/PUT/POST/DELETE on `/dedicated/server/*`),
SOPS-encrypt to `secrets/ovh-credentials.sops.yaml`, commit.

## L2: Persistent Auth (Tofu State)

Created by `tofu apply` Phase 1 (persistent-auth targets). Stored in PG
backend. Resources have `lifecycle { prevent_destroy = true }`.

| Resource                                            | Reads               | Creates                                  | Depended On By                |
| --------------------------------------------------- | ------------------- | ---------------------------------------- | ----------------------------- |
| `proxmox_virtual_environment_role.persistent`       | L1: Proxmox token   | Proxmox TerraformAdmin role              | L2: Proxmox users             |
| `proxmox_virtual_environment_user.persistent`       | L2: roles           | Proxmox users (terraform@pve)            | L2: tokens                    |
| `proxmox_virtual_environment_user_token.persistent` | L2: users           | Terraform API token                      | L3: Proxmox VM creation       |
| `talos_machine_secrets.cluster`                     | `var.talos_version` | CA keypairs, bootstrap token, etcd certs | L3: all Talos machine configs |

**If tofu state is lost**: restore the state or import surviving resources before
applying. Recover Talos machine secrets from `secrets/talos-machine-secrets.sops.yaml`
using the import procedure in `cluster/terraform/main/talos-machine-secrets.tf`.
Preserve the Nebula identities in `secrets/nebula/`.

## L3: Infrastructure

Created by `tofu apply` Phase 2.

| Resource                                           | Key Inputs                                        | What It Produces                    |
| -------------------------------------------------- | ------------------------------------------------- | ----------------------------------- |
| `ovh_dedicated_server.*`                           | L1: OVH credentials, Talos image                  | OVH Kimsufi Talos nodes             |
| `proxmox_virtual_environment_vm.talos`             | L1: Proxmox token, Talos disk                     | Proxmox Talos VMs, if configured    |
| `data.talos_machine_configuration.home_worker`     | L2: machine secrets, Nebula cert, config patches  | Home bare-metal worker config       |
| `talos_machine_configuration_apply.*`              | L2: machine secrets, nebula certs, config patches | Talos config pushed to nodes        |
| `talos_machine_bootstrap.cluster`                  | Machine config applied                            | etcd initialized, k8s API available |
| `local_file.kubeconfig` / `local_file.talosconfig` | Bootstrap output                                  | Cluster access files                |

**If OVH Talos nodes are lost**: restore or replace them through OVH, update
`ovh-nodes.tf`/`nebula-mesh.json` as needed, and run bootstrap so machine config
is re-applied. Nodes rejoin etcd if quorum exists, or bootstrap fresh.

**If kubeconfig is lost**: `tofu apply` regenerates it from Talos state.

## L4: Cluster Networking

Created by `tofu apply` Phase 2 (continued).

| Resource                             | What It Does                            |
| ------------------------------------ | --------------------------------------- |
| `null_resource.gateway_api_crds`     | Installs Gateway API CRDs before Cilium |
| `null_resource.cilium_bootstrap`     | Helm install Cilium CNI                 |
| `null_resource.wait_for_nodes_ready` | Polls until all nodes Ready             |

Phase 2 also creates `kubernetes_namespace.flux_system` and deploys
`kubernetes_secret.sops_age_cluster_secrets` from the L1 age key. Flux needs
that Secret before it can decrypt any GitOps-managed credentials.

## L5: Flux GitOps

Created by `tofu apply` Phase 3. OpenTofu applies the committed bootstrap
manifests from `cluster/k8s/flux-system/` and waits for the root Flux
`GitRepository` and `Kustomization` to become Ready.

| Dependency                                | Why                                    |
| ----------------------------------------- | -------------------------------------- |
| L1: cluster age key (deployed in Phase 2) | Flux decrypts `*.sops.yaml` in-cluster |
| L4: nodes Ready, networking functional    | Flux pods must schedule                |

**If Flux is broken but cluster is healthy**: inspect and update the committed
manifests under `cluster/k8s/flux-system/`, then run `tofu apply` to re-apply
the bootstrap manifests.

### Git source authentication boundary

The bootstrap `flux-system/flux-system` GitRepository reads the public Ducktape
repository anonymously. It must not depend on the GitHub App Secret that Flux
itself decrypts after bootstrap. Private repositories and image-automation writes
use runtime credentials; their rotation is outside this bootstrap graph.

## L6: Cluster Services

Deployed by Flux from `cluster/k8s/`. Depend on SOPS secrets (L1) being
decryptable (L5) and on dependency chains between kustomizations (cert-manager
→ gateway → apps, CNPG → databases → apps, etc.).

Service-specific prerequisites belong in each component's manifests and docs.
Credential handling is documented in <secrets.md>.

## L7: NixOS Worker Integration

wyrm2 and rugged join the cluster via kubelet TLS bootstrap over Nebula mesh.

| What They Need           | Source                                | Delivery                                                                        |
| ------------------------ | ------------------------------------- | ------------------------------------------------------------------------------- |
| Nebula host cert         | Persisted Nebula identity (L1)        | `secrets/nebula/{host}.crt` (plaintext) → `nixos-rebuild switch`                |
| Nebula host key          | Persisted Nebula identity (L1)        | `secrets/nebula/{host}.sops.key` (SOPS binary) → `nixos-rebuild switch`         |
| Nebula CA cert           | L1: `secrets/nebula/ca.crt`           | Plaintext PEM, deployed via `environment.etc`                                   |
| k8s bootstrap kubeconfig | L2: machine secrets (bootstrap token) | `secrets/k8s-worker.yaml` (SOPS, auto-updated by bootstrap) → sops-nix          |
| k8s CA cert              | L2: machine secrets (k8s CA cert)     | `secrets/k8s-ca.crt` (plaintext, auto-updated by bootstrap) → `environment.etc` |

**After fresh bootstrap** (same persisted machine secrets):

Since machine secrets are persistent (L2), `k8s-ca.crt` and `k8s-worker.yaml` are
auto-updated by the bootstrap script. Deploy those outputs to workers:

1. Commit updated `secrets/k8s-ca.crt` and `secrets/k8s-worker.yaml` (if changed)
2. `nixos-rebuild switch` on each NixOS worker (picks up any cert/token changes)
3. Verify: `kubectl get nodes` must show the worker as `Ready`

**After an intentional replacement of Talos machine secrets**:

If the CA changed, existing kubelet TLS state is invalid:

1. `nixos-rebuild switch` on each NixOS worker
2. Delete stale kubelet TLS state and restart:

   ```bash
   sudo rm /var/lib/kubelet/kubelet.conf /var/lib/kubelet/pki/*
   sudo systemctl restart kubelet
   ```

3. Verify: `kubectl get nodes` must show the worker as `Ready`

**If only Nebula certificates rotate**: deploy the updated identities; retain kubelet TLS state when the Kubernetes CA is unchanged.

## Recovery Scenarios

### Full bootstrap from zero

1. Ensure the operator can decrypt SOPS inputs and reach the infrastructure APIs
2. Ensure L1 SOPS secrets exist in git (Nebula identities, cluster age key, infrastructure credentials)
3. Start temp PG: `podman run -d --name tofu-pg -e POSTGRES_PASSWORD=tofu -e POSTGRES_DB=tfstate -p 15432:5432 docker.io/postgres:16-alpine`
4. `tofu init -reconfigure` with `PG_CONN_STR` pointing to temp PG
5. `bazel run //cluster:bootstrap` (on wyrm2, add `-- --exclude=proxmox_virtual_environment_vm.wyrm2`)
6. Deploy the persisted Nebula identities and exported kubelet bootstrap material to NixOS workers
7. Migrate state to in-cluster PG: `tofu init -migrate-state` — **verify with `tofu state list` before deleting temp PG**

### Lost tofu state (but cluster running)

1. Import existing resources: `tofu import` for Proxmox users/roles/tokens
2. Preserve persisted Nebula identities and import the SOPS-backed Talos machine secrets
3. `tofu apply` to reconcile state with reality
4. If Flux still running: no action needed for L5+

### Lost Nebula CA

1. Generate new CA: `nebula-cert ca -name "allegedly.works"`
2. Write cert to `secrets/nebula/ca.crt`, encrypt key to `secrets/nebula/ca.sops.key`
3. Regenerate and persist every node identity (see <secrets.md> "Generating a new cert")
4. `tofu apply` to embed the new Talos node certificates and keys
5. `nixos-rebuild switch` on NixOS workers; `ansible-playbook atlas.yaml --tags nebula`

### Lost cluster age key

Regenerate and redeploy per <secrets.md> § "Rotating the Cluster Age Key". The
key is also SOPS-backed in `secrets/shared/cluster-secrets-age.yaml`, so it
survives tofu-state loss.
