# Cluster Bootstrap Dependencies

Complete dependency graph for bootstrapping the cluster from scratch or
recovering from partial state loss. Organized as layers — each layer depends
on the ones above it.

## Dependency Graph

```text
L0  External Credentials ─────────────────────────────────────────────────
    (Hetzner token, Proxmox token, admin age key)
        │
L1  SOPS Secrets in Git ─────────────────────────────────────────────────
    (nebula-ca, flux deploy key, cluster age key, app credentials)
        │  encrypted with: admin age key (L0)
        │
L2  Persistent Auth (tofu state) ────────────────────────────────────────
    (Proxmox users/tokens, nebula node certs, k8s SOPS secret)
        │  reads: L1 SOPS files
        │  writes to: Proxmox API, local disk (nebula-certs/)
        │
L3  Infrastructure ──────────────────────────────────────────────────────
    (Talos machine secrets, Hetzner VPS, Proxmox VM, kubeconfig)
        │  reads: L0 tokens, L2 nebula certs
        │
L4  Cluster Networking ──────────────────────────────────────────────────
    (Gateway API CRDs, Cilium CNI, node readiness)
        │
L5  Flux GitOps ─────────────────────────────────────────────────────────
    (flux-system namespace, Flux controllers, git sync)
        │  reads: L1 flux deploy key (via L2 tofu), L1 cluster age key
        │  decrypts: all k8s/**/*.sops.yaml
        │
L6  Cluster Services ────────────────────────────────────────────────────
    (cert-manager, Vault, ESO, Authentik, PowerDNS, apps...)
        │  reads: L1 SOPS app credentials
        │
L7  NixOS Worker Integration ────────────────────────────────────────────
    (wyrm2, rugged join cluster via Nebula + kubelet bootstrap)
        │  reads: L1 nebula CA (certs generated manually, stored in SOPS)
```

## L0: External Credentials

Not stored in git or tofu state. Must exist before any `tofu apply`.

| Credential                | Source                                          | Storage               | Consumed By                    |
| ------------------------- | ----------------------------------------------- | --------------------- | ------------------------------ |
| Admin age private key     | Derived from `~/.ssh/id_ed25519` via ssh-to-age | User SSH key          | Decrypt all SOPS files locally |
| GitHub account SSH access | GitHub settings → SSH keys                      | `~/.ssh/id_ed25519`   | Flux deploy key registration   |
| Domain DNS delegation     | Domain registrar                                | NS records → PowerDNS | External DNS resolution        |

**If lost**: Regenerate from the source (GitHub settings, domain registrar).
No downstream regeneration needed — these are read-only inputs.

## L1: SOPS Secrets in Git

Encrypted with admin age key (L0). These are the source of truth for
secrets that Flux and tofu consume.

| File                                      | Contents                           | Depends On                      | Depended On By                                                   |
| ----------------------------------------- | ---------------------------------- | ------------------------------- | ---------------------------------------------------------------- |
| `secrets/nebula/ca.crt`                   | Nebula CA public cert (plaintext)  | None                            | L2: tofu node cert signing; L7: NixOS workers, ansible           |
| `secrets/nebula/ca.sops.key`              | Nebula CA private key (SOPS bin)   | Admin age key                   | L2: tofu node cert signing                                       |
| `secrets/nebula/*.sops.key`               | Nebula host private keys (binary)  | Admin age key + host age key    | L7: NixOS worker nebula mesh, ansible                            |
| `secrets/nebula/*.crt`                    | Nebula host public certs (plain)   | None                            | L7: NixOS worker nebula mesh, ansible                            |
| `secrets/shared/flux-deploy-key.yaml`     | ED25519 SSH private key            | Admin age key                   | L5: Flux git sync; GitHub deploy key                             |
| `secrets/flux-deploy-key.pub`             | ED25519 SSH public key (plain)     | None                            | GitHub deploy key registration                                   |
| `secrets/shared/cluster-secrets-age.yaml` | Age keypair (private + public)     | Admin age key                   | L5: Flux SOPS decryption (`sops-age-cluster-secrets` k8s secret) |
| `secrets/shared/cluster-tokens.yaml`      | Hetzner + Proxmox API tokens       | Admin age key + user age keys   | `.envrc` → `TF_VAR_hcloud_token`, `PROXMOX_VE_API_TOKEN`         |
| `secrets/k8s-ca.crt`                      | K8s cluster CA cert (plaintext)    | None                            | L7: kubelet TLS on NixOS workers                                 |
| `secrets/k8s-worker.yaml`                 | k8s bootstrap token                | Admin age key                   | L7: kubelet TLS bootstrap on NixOS workers                       |
| `cluster/k8s/**/*.sops.yaml` (26 files)   | App credentials (API keys, tokens) | Admin age key + cluster age key | L6: individual services                                          |

**If nebula CA is lost**: Generate new CA with `nebula-cert ca`, write cert
to `secrets/nebula/ca.crt`, encrypt key to `secrets/nebula/ca.sops.key`.
Then regenerate all node certs (L2) and update all NixOS worker nebula
files (L7).

**If `secrets/shared/cluster-secrets-age.yaml` is lost**: Generate new age key with
`age-keygen`, SOPS-encrypt, commit. Update `.sops.yaml` with new public key.
Re-encrypt all `k8s/**/*.sops.yaml` files with `sops updatekeys`. Redeploy
the k8s secret via `tofu apply`.

**If `secrets/shared/flux-deploy-key.yaml` is lost**: Generate new ED25519 key with
`ssh-keygen -t ed25519`, SOPS-encrypt, commit. Register public key in
GitHub → repo settings �� deploy keys. Redeploy via `tofu apply`.

**If `secrets/shared/cluster-tokens.yaml` is lost**: Re-enter the Hetzner token
(Hetzner console) and Proxmox token (Proxmox UI → API Tokens), SOPS-encrypt
to `secrets/shared/cluster-tokens.yaml`, commit. `.envrc` picks them up automatically.

**If a `k8s/**/\*.sops.yaml` app credential is lost\*\*: Re-enter the credential
from the external service (see [App Credentials](#app-credentials) below),
SOPS-encrypt, commit, push. Flux picks it up automatically.

## L2: Persistent Auth (Tofu State)

Created by `tofu apply` Phase 1 (persistent-auth targets). Stored in PG
backend. Resources have `lifecycle { prevent_destroy = true }`.

| Resource                                                          | Reads                                         | Creates                                           | Depended On By                          |
| ----------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------- | --------------------------------------- |
| `proxmox_virtual_environment_role.persistent`                     | L0: Proxmox token                             | Proxmox roles (CSI, TerraformAdmin)               | L2: Proxmox users                       |
| `proxmox_virtual_environment_user.persistent`                     | L2: roles                                     | Proxmox users (kubernetes-csi@pve, terraform@pve) | L2: tokens                              |
| `proxmox_virtual_environment_user_token.persistent`               | L2: users                                     | API tokens for CSI + terraform                    | L3: Proxmox VM creation; L6: CSI driver |
| `local_file.nebula_ca_crt` / `local_sensitive_file.nebula_ca_key` | L1: `secrets/nebula/ca.{crt,sops.key}`        | CA cert/key on disk                               | L2: node cert signing                   |
| `null_resource.nebula_node_cert` (5 Talos nodes)                  | L2: CA on disk                                | Per-node cert+key at `nebula-certs/`              | L3: Talos machine config (embedded)     |
| `talos_machine_secrets.cluster`                                   | `var.talos_version`                           | CA keypairs, bootstrap token, etcd certs          | L3: all Talos machine configs           |
| `kubernetes_namespace.flux_system`                                | L3: kubeconfig                                | `flux-system` namespace                           | L2: SOPS age secret; L5: Flux           |
| `kubernetes_secret.sops_age_cluster_secrets`                      | L1: `secrets/shared/cluster-secrets-age.yaml` | k8s secret in flux-system                         | L5: Flux SOPS decryption                |

**If tofu state is lost**: All L2 resources must be recreated. Proxmox
roles/users/tokens that still exist on Proxmox must be deleted first (tofu
can't create over existing). Nebula node certs on disk must be deleted
(nebula-cert refuses to overwrite). Then run bootstrap Phase 1.

**If only Proxmox tokens are lost** (but state intact): `tofu apply` with
persistent-auth targets regenerates them. Update the SOPS CSI secret.

## L3: Infrastructure

Created by `tofu apply` Phase 2.

| Resource                                           | Key Inputs                                        | What It Produces                    |
| -------------------------------------------------- | ------------------------------------------------- | ----------------------------------- |
| `hcloud_server.vps` (4x)                           | L0: hcloud token, Talos image                     | 2 CP + 2 worker VPS nodes           |
| `proxmox_virtual_environment_vm.talos`             | L0: Proxmox token, Talos disk                     | 1 CP Proxmox VM                     |
| `talos_machine_configuration_apply.*`              | L2: machine secrets, nebula certs, config patches | Talos config pushed to nodes        |
| `talos_machine_bootstrap.cluster`                  | Machine config applied                            | etcd initialized, k8s API available |
| `local_file.kubeconfig` / `local_file.talosconfig` | Bootstrap output                                  | Cluster access files                |

**If VPS nodes are lost**: `tofu apply` recreates them. Machine config is
re-applied, nodes rejoin etcd (if quorum exists) or bootstrap fresh.

**If kubeconfig is lost**: `tofu apply` regenerates it from Talos state.

## L4: Cluster Networking

Created by `tofu apply` Phase 2 (continued).

| Resource                             | What It Does                            |
| ------------------------------------ | --------------------------------------- |
| `null_resource.gateway_api_crds`     | Installs Gateway API CRDs before Cilium |
| `null_resource.cilium_bootstrap`     | Helm install Cilium CNI                 |
| `null_resource.wait_for_nodes_ready` | Polls until all nodes Ready             |

**If Cilium is broken**: Delete the Cilium helm release and re-run bootstrap
Phase 2 (`--start-from=infrastructure`).

## L5: Flux GitOps

Created by `tofu apply` Phase 3. The `flux_bootstrap_git` resource deploys
Flux controllers and pushes sync manifests to the git repo.

| Dependency                              | Why                                    |
| --------------------------------------- | -------------------------------------- |
| L1: flux deploy key (via L2 tofu)       | Flux authenticates to GitHub           |
| L1: cluster age key (via L2 k8s secret) | Flux decrypts `*.sops.yaml` in-cluster |
| L4: nodes Ready, networking functional  | Flux pods must schedule                |

**If Flux is broken but cluster is healthy**: `tofu apply` with
`-target=flux_bootstrap_git.cluster` reinstalls Flux. Or delete the
flux-system namespace and re-run Phase 3.

## L6: Cluster Services

Deployed by Flux from `cluster/k8s/`. Depend on SOPS secrets (L1) being
decryptable (L5) and on dependency chains between kustomizations (cert-manager
→ gateway → apps, CNPG → databases → apps, etc.).

Services with external credentials read them from `*.sops.yaml` files
decrypted by Flux. See [App Credentials](#app-credentials) for the full list.

### flux-webhook-token

The `flux-webhook-token` tofu-controller module creates the `github-webhook-token` k8s
secret (consumed by the Flux `github` Receiver) and the GitHub repository webhook on
`ducktape`. It reads `github-secrets-sync-pat` (provided by `github-secrets-sync-secrets`).
The `flux-webhook` Kustomization depends on `flux-webhook-token`.

**If `flux-webhook` receiver stops working after bootstrap**: Check `flux-webhook-token`
reconciliation. The token and webhook URL use `ignore_changes` and are stable across
re-applies — only resource recreation changes them.

## L7: NixOS Worker Integration

wyrm2 and rugged join the cluster via kubelet TLS bootstrap over Nebula mesh.

| What They Need           | Source                                       | Delivery                                                                        |
| ------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------- |
| Nebula host cert         | Generated via `nebula-cert sign` (see below) | `secrets/nebula/{host}.crt` (plaintext) → `nixos-rebuild switch`                |
| Nebula host key          | Generated via `nebula-cert sign` (see below) | `secrets/nebula/{host}.sops.key` (SOPS binary) → `nixos-rebuild switch`         |
| Nebula CA cert           | L1: `secrets/nebula/ca.crt`                  | Plaintext PEM, deployed via `environment.etc`                                   |
| k8s bootstrap kubeconfig | L2: machine secrets (bootstrap token)        | `secrets/k8s-worker.yaml` (SOPS, auto-updated by bootstrap) → sops-nix          |
| k8s CA cert              | L2: machine secrets (k8s CA cert)            | `secrets/k8s-ca.crt` (plaintext, auto-updated by bootstrap) → `environment.etc` |

**After fresh bootstrap** (same persisted machine secrets):

Since machine secrets are persistent (L2), `k8s-ca.crt` and `k8s-worker.yaml` are
auto-updated by the bootstrap script. Workers rejoin without manual intervention:

1. Commit updated `secrets/k8s-ca.crt` and `secrets/k8s-worker.yaml` (if changed)
2. `nixos-rebuild switch` on each NixOS worker (picks up any cert/token changes)
3. Verify: `kubectl get nodes` should show the worker as `Ready` after ~30s

**After bootstrap with NEW machine secrets** (e.g., lost tofu state, fresh seed):

If the CA changed, existing kubelet TLS state is invalid:

1. `nixos-rebuild switch` on each NixOS worker
2. Delete stale kubelet TLS state and restart:

   ```bash
   sudo rm /var/lib/kubelet/kubelet.conf /var/lib/kubelet/pki/*
   sudo systemctl restart kubelet
   ```

3. Verify: `kubectl get nodes` should show the worker as `Ready` after ~30s

**If only nebula certs rotate** (same cluster CA): steps 1-3 only, skip step 4.

## App Credentials

External service credentials stored in `cluster/k8s/**/*.sops.yaml`. If lost,
re-enter from the external service and SOPS-encrypt.

| Category        | File(s)                                                                                                                                                                | External Source                     |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| **AWS**         | `dns-automation/aws-credentials.sops.yaml`                                                                                                                             | AWS IAM console (Route 53 access)   |
| **GitHub**      | `agents/shared-secrets/github-token.sops.yaml`, `arc/secrets/github-app.sops.yaml`, `github-secrets-sync/secrets/github-secrets-sync-pat.sops.yaml`                    | GitHub settings → PAT / App         |
| **BuildBuddy**  | `buildbuddy-executor/api-key.sops.yaml`, `agents/shared-secrets/buildbuddy-api-key.sops.yaml`                                                                          | BuildBuddy org settings             |
| **OpenAI**      | `props/secrets/openai-api-key.sops.yaml`, `agents/openclaw/gateway-secrets/openai-api-key.sops.yaml`                                                                   | OpenAI API keys                     |
| **Anthropic**   | `agents/openclaw/gateway-secrets/anthropic-api-key.sops.yaml`                                                                                                          | Anthropic console                   |
| **Google**      | `agents/airlock/google-client-credentials.sops.yaml`, `agents/openclaw/gateway-secrets/gemini-api-key.sops.yaml`                                                       | Google Cloud console                |
| **Financial**   | `agents/openclaw/sandbox-secrets/coinbase-api-credentials.sops.yaml`, `agents/openclaw/sandbox-secrets/ibkr-flex-query-credentials.sops.yaml`                          | Coinbase / IBKR portals             |
| **Messaging**   | `agents/openclaw/gateway-secrets/telegram-bot-token.sops.yaml`, `flux-webhook/ntfy-webhook.sops.yaml`                                                                  | Telegram @BotFather / ntfy.sh       |
| **Home infra**  | `agents/homeassistant-proxy/ha-token.sops.yaml`, `scanner/samba-credentials.sops.yaml`                                                                                 | HA UI / Samba config                |
| **OAuth**       | `agents/airlock/oura-client-credentials.sops.yaml`                                                                                                                     | Oura developer portal               |
| **Agent infra** | `agents/shared-secrets/attic-push-token.sops.yaml`, `agents/claude-sandbox-secrets/claude-web-age-key.sops.yaml`, `agents/openclaw/mitmproxy/mitmproxy-ca-*.sops.yaml` | Generated / internal                |
| **Proxmox CSI** | `proxmox-csi/secrets/proxmox-csi.sops.yaml`                                                                                                                            | L2: Proxmox CSI token (tofu output) |
| **Nix cache**   | `nix-cache/app/signing-key.sops.yaml`, `nix-cache/app/jwt-token.sops.yaml`                                                                                             | Generated at bootstrap              |
| **CNPG**        | `tofu-state/db/credentials.sops.yaml`                                                                                                                                  | Generated (random password)         |

## Recovery Scenarios

### Full bootstrap from zero

1. Ensure L0 credentials exist (SSH key, GitHub access)
2. Ensure L1 SOPS secrets exist in git (nebula-ca, flux deploy key, cluster age key)
3. Start temp PG: `podman run -d --name tofu-pg -e POSTGRES_PASSWORD=tofu -e POSTGRES_DB=tfstate -p 15432:5432 docker.io/postgres:16-alpine`
4. `tofu init -reconfigure` with `PG_CONN_STR` pointing to temp PG
5. `bazel run //cluster:bootstrap -- --exclude=module.wyrm2`
6. Post-bootstrap: copy nebula certs to NixOS worker SOPS files, `nixos-rebuild switch`
7. Migrate state to in-cluster PG: `tofu init -migrate-state` — **verify with `tofu state list` before deleting temp PG**

### Lost tofu state (but cluster running)

1. Import existing resources: `tofu import` for Proxmox users/roles/tokens
2. Delete stale nebula cert files on disk (tofu regenerates)
3. `tofu apply` to reconcile state with reality
4. If Flux still running: no action needed for L5+

### Lost Nebula CA

1. Generate new CA: `nebula-cert ca -name "allegedly.works"`
2. Write cert to `secrets/nebula/ca.crt`, encrypt key to `secrets/nebula/ca.sops.key`
3. `tofu apply` (persistent-auth targets) — regenerates all Talos node certs
4. Regenerate non-Talos node certs (see <secrets.md> "Generating a new cert")
5. `nixos-rebuild switch` on NixOS workers; `ansible-playbook atlas.yaml --tags nebula`
6. Talos nodes get new certs via machine config apply (automatic in tofu)

### Lost cluster age key

1. Generate: `age-keygen -o /dev/stdout`
2. SOPS-encrypt private key to `secrets/shared/cluster-secrets-age.yaml`
3. Update `.sops.yaml` with new public key
4. Re-encrypt all `k8s/**/*.sops.yaml`: `for f in $(find cluster/k8s -name '*.sops.yaml'); do sops updatekeys "$f"; done`
5. `tofu apply` to deploy new k8s secret
6. Commit + push; Flux picks up re-encrypted secrets

### Lost single app credential

1. Get new credential from external service
2. Edit the `*.sops.yaml` file: `sops cluster/k8s/<path>.sops.yaml`
3. Commit + push; Flux deploys automatically
