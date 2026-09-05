# Cluster Bootstrap Dependencies

Complete dependency graph for bootstrapping the cluster from scratch or
recovering from partial state loss. Organized as layers — each layer depends
on the ones above it.

## Dependency Graph

```text
L0  External Credentials ─────────────────────────────────────────────────
    (Proxmox token, OVH credentials, admin age key)
        │
L1  SOPS Secrets in Git ─────────────────────────────────────────────────
    (nebula-ca, cluster age key, app credentials)
        │  encrypted with: admin age key (L0)
        │
L2  Persistent Auth (tofu state) ────────────────────────────────────────
    (Proxmox users/tokens, k8s SOPS secret)
        │  reads: L1 SOPS files
        │  writes to: Proxmox API, Kubernetes API
        │
L3  Infrastructure ──────────────────────────────────────────────────────
    (Talos machine secrets, OVH Kimsufi nodes, Proxmox VM, kubeconfig)
        │  reads: L0 tokens, L1 Nebula identities, L2 persistent auth
        │
L4  Cluster Networking ──────────────────────────────────────────────────
    (Gateway API CRDs, Cilium CNI, node readiness)
        │
L5  Flux GitOps ─────────────────────────────────────────────────────────
    (flux-system namespace, Flux controllers, git sync)
        │  reads: L1 cluster age key
        │  decrypts: all k8s/**/*.sops.yaml
        │
L6  Cluster Services ────────────────────────────────────────────────────
    (cert-manager, ESO, Authentik, PowerDNS, apps...)
        │  reads: L1 SOPS app credentials
        │
L7  NixOS Worker Integration ────────────────────────────────────────────
    (wyrm2, rugged join cluster via Nebula + kubelet bootstrap)
        │  reads: L1 nebula CA (certs generated manually, stored in SOPS)
```

## L0: External Credentials

Not stored in git or tofu state. Must exist before any `tofu apply`.

| Credential            | Source                                          | Storage               | Consumed By                    |
| --------------------- | ----------------------------------------------- | --------------------- | ------------------------------ |
| Admin age private key | Derived from `~/.ssh/id_ed25519` via ssh-to-age | User SSH key          | Decrypt all SOPS files locally |
| Domain DNS delegation | Domain registrar                                | NS records → PowerDNS | External DNS resolution        |

**If lost**: Regenerate from the source (GitHub settings, domain registrar).
No downstream regeneration needed — these are read-only inputs.

## L1: SOPS Secrets in Git

Encrypted with admin age key (L0). These are the source of truth for
secrets that Flux and tofu consume.

| File                                                      | Contents                                                                   | Depends On                                                                  | Depended On By                                                                                                                                         |
| --------------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `secrets/nebula/ca.crt`                                   | Nebula CA public cert (plaintext)                                          | None                                                                        | L3: Talos machine configuration; L7: NixOS workers, ansible                                                                                            |
| `secrets/nebula/ca.sops.key`                              | Nebula CA private key (SOPS bin)                                           | Admin age key                                                               | Explicit certificate rotation only                                                                                                                     |
| `secrets/nebula/*.sops.key`                               | Nebula host private keys (binary)                                          | Operator recipients for Talos; host recipients for NixOS; admin-only mobile | L3: Talos machine configuration; L7: NixOS worker mesh, ansible, mobile import generation                                                              |
| `secrets/nebula/*.crt`                                    | Nebula host public certs (plain)                                           | None                                                                        | L3: Talos machine configuration; L7: NixOS worker mesh, ansible, mobile import generation                                                              |
| `secrets/ducktape-automation.<date>.private-key.sops.pem` | GitHub App PEM (RSA, SOPS bin)                                             | Admin age key + cluster-secrets + ci                                        | L6: Flux private/write git auth (mirrored into `cluster/k8s/flux-system/ducktape-automation-github-app.sops.yaml`)                                     |
| `secrets/shared/cluster-secrets-age.yaml`                 | Age keypair (private + public)                                             | Admin age key                                                               | L5: Flux SOPS decryption (`sops-age-cluster-secrets` k8s secret)                                                                                       |
| `secrets/shared/cluster-tokens.yaml`                      | Proxmox API token; legacy HCloud token retained for account-history access | Admin age key + user age keys                                               | `.envrc` -> `PROXMOX_VE_API_TOKEN`                                                                                                                     |
| `secrets/ovh-credentials.sops.yaml`                       | OVH API credentials (AK/AS/CK)                                             | Admin age key + user age keys + cluster-secrets                             | `terraform.tf` OVH provider → `ovh-nodes.tf` Kimsufi provisioning; cluster-secrets so the `infra-drift` tf-runner can plan (`cluster/k8s/infra-drift`) |
| `secrets/ovh-rescue-ssh.sops.yaml`                        | OVH rescue-mode SSH keypair (ED25519)                                      | Admin age key + user age keys + cluster-secrets                             | `ovh_dedicated_server.kimsufi` `rescue_ssh_key`; rescue `remote-exec` during Talos install; same `infra-drift` reader                                  |
| `ssh_keys/*-forgejo.sops.key`                             | Per-host Forgejo SSH private keys for `agentydragon`                       | Admin age key + owning host's user age key                                  | Home Manager Forgejo SSH config; L6: `forgejo-agentydragon` attaches matching public keys                                                              |
| `secrets/k8s-ca.crt`                                      | K8s cluster CA cert (plaintext)                                            | None                                                                        | L7: kubelet TLS on NixOS workers                                                                                                                       |
| `secrets/k8s-worker.yaml`                                 | k8s bootstrap token                                                        | Admin age key                                                               | L7: kubelet TLS bootstrap on NixOS workers                                                                                                             |
| `cluster/k8s/**/*.sops.yaml`                              | App credentials, generated service identities, API keys, tokens            | Admin age key + cluster age key                                             | L6: individual services + L5 Flux git auth (`ducktape-automation-github-app`)                                                                          |

**If nebula CA is lost**: Generate new CA with `nebula-cert ca`, write cert
to `secrets/nebula/ca.crt`, encrypt key to `secrets/nebula/ca.sops.key`.
Then explicitly regenerate and persist every node certificate/key under
`secrets/nebula/`, apply the Talos machine configuration, and update all NixOS
worker Nebula files (L7).

**If `secrets/shared/cluster-secrets-age.yaml` is lost**: regenerate and redeploy per
<secrets.md> § "Rotating the Cluster Age Key".

**If `secrets/shared/cluster-tokens.yaml` is lost**: Re-enter the Proxmox token
(Proxmox UI -> API Tokens), SOPS-encrypt to `secrets/shared/cluster-tokens.yaml`,
commit. `.envrc` picks it up automatically. The legacy HCloud token can be
re-entered separately if account archaeology needs it.

**If `secrets/ovh-credentials.sops.yaml` is lost**: Create new API credentials at
`https://api.us.ovhcloud.com/createToken/` (GET/PUT/POST/DELETE on `/dedicated/server/*`),
SOPS-encrypt to `secrets/ovh-credentials.sops.yaml`, commit.

**If a `k8s/**/\*.sops.yaml` app credential is lost\*\*: re-enter the credential
from the external service (see [App Credentials](#app-credentials) below),
SOPS-encrypt, commit, push. Flux picks it up automatically.

## L2: Persistent Auth (Tofu State)

Created by `tofu apply` Phase 1 (persistent-auth targets). Stored in PG
backend. Resources have `lifecycle { prevent_destroy = true }`.

| Resource                                                                                        | Reads                                         | Creates                                  | Depended On By                          |
| ----------------------------------------------------------------------------------------------- | --------------------------------------------- | ---------------------------------------- | --------------------------------------- |
| `proxmox_virtual_environment_role.persistent`                                                   | L0: Proxmox token                             | Proxmox roles (CSI, TerraformAdmin)      | L2: Proxmox users                       |
| `proxmox_virtual_environment_user.persistent`                                                   | L2: roles                                     | Proxmox users (terraform@pve)            | L2: tokens                              |
| `proxmox_virtual_environment_user_token.persistent`                                             | L2: users                                     | API tokens for CSI + terraform           | L3: Proxmox VM creation; L6: CSI driver |
| `data.local_file.nebula_ca_crt`                                                                 | L1: `secrets/nebula/ca.crt`                   | CA public cert in Terraform              | L3: Talos machine config (embedded)     |
| `data.local_file.nebula_node_crt` / `data.sops_file.nebula_node_key` (Tofu-managed Talos nodes) | L1: `secrets/nebula/{host}.{crt,sops.key}`    | Persisted per-node cert/key in Terraform | L3: Talos machine config (embedded)     |
| `talos_machine_secrets.cluster`                                                                 | `var.talos_version`                           | CA keypairs, bootstrap token, etcd certs | L3: all Talos machine configs           |
| `kubernetes_namespace.flux_system`                                                              | L3: kubeconfig                                | `flux-system` namespace                  | L2: SOPS age secret; L5: Flux           |
| `kubernetes_secret.sops_age_cluster_secrets`                                                    | L1: `secrets/shared/cluster-secrets-age.yaml` | k8s secret in flux-system                | L5: Flux SOPS decryption                |

**If tofu state is lost**: All L2 resources must be recreated. Proxmox
roles/users/tokens that still exist on Proxmox must be deleted first (tofu
can't create over existing). Nebula identities remain recoverable in
`secrets/nebula/`; do not regenerate them as part of state recovery. Then run
bootstrap Phase 1.

**If only Proxmox tokens are lost** (but state intact): `tofu apply` with
persistent-auth targets regenerates them. Update the SOPS CSI secret.

## L3: Infrastructure

Created by `tofu apply` Phase 2.

| Resource                                           | Key Inputs                                        | What It Produces                    |
| -------------------------------------------------- | ------------------------------------------------- | ----------------------------------- |
| `ovh_dedicated_server.*`                           | L0: OVH credentials, Talos image                  | OVH Kimsufi Talos nodes             |
| `proxmox_virtual_environment_vm.talos`             | L0: Proxmox token, Talos disk                     | Proxmox Talos VMs, if configured    |
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

**If Cilium is broken**: Delete the Cilium helm release and re-run bootstrap
Phase 2 (`--start-from=infrastructure`).

## L5: Flux GitOps

Created by `tofu apply` Phase 3. OpenTofu applies the committed bootstrap
manifests from `cluster/k8s/flux-system/` and waits for the root Flux
`GitRepository` and `Kustomization` to become Ready.

| Dependency                              | Why                                    |
| --------------------------------------- | -------------------------------------- |
| L1: cluster age key (via L2 k8s secret) | Flux decrypts `*.sops.yaml` in-cluster |
| L4: nodes Ready, networking functional  | Flux pods must schedule                |

**If Flux is broken but cluster is healthy**: inspect and update the committed
manifests under `cluster/k8s/flux-system/`, then run `tofu apply` to re-apply
the bootstrap manifests. Or delete the `flux-system` namespace and re-run Phase 3.

### GitHub App authentication (runtime)

The raw Terraform bootstrap manifests must not depend on the GitHub App Secret:
the root `flux-system` GitRepository reads the public `ducktape` repo
anonymously, so cold-start bootstrap only needs network access to GitHub and the
Terraform-created SOPS age Secret. After the root Kustomization starts
reconciling, Flux decrypts the SOPS-encrypted GitHub App Secret and uses it for
private/write paths:

- `GitRepository/ducktape-write` in
  `cluster/k8s/flux-image-automation-ghcr/ducktape-write-source.yaml` is the
  authenticated checkout used by `ImageUpdateAutomation/all-images` to push
  image-pin commits back to `ducktape/devel`.
- `GitRepository/gaffer-private` in `cluster/k8s/gaffer-private-source/source.yaml`
  uses the same Secret for private repo reads and image automation pushes to
  `gaffer-private/main`.

The in-cluster Secret `flux-system/ducktape-automation-github-app` holds
`githubAppID`, `githubAppInstallationID`, and `githubAppPrivateKey` — sourced
from `secrets/ducktape-automation.<date>.private-key.sops.pem` and committed as
a SOPS-encrypted Secret manifest at
`cluster/k8s/flux-system/ducktape-automation-github-app.sops.yaml` (encrypted
to admin + cluster-secrets recipients).

**If the App PEM is rotated**: regenerate the App's private key in GitHub UI,
overwrite `secrets/ducktape-automation.<date>.private-key.sops.pem` (bump the
date in the filename — update the path_regex in `.sops.yaml` if needed),
re-encode the cluster-side Secret by re-running the encrypt workflow used to
mint it, commit, push. Flux picks up the new key on next reconcile.

**If the App is uninstalled or its installation ID changes**: edit
`cluster/k8s/flux-system/ducktape-automation-github-app.sops.yaml` via
`sops`, update `githubAppInstallationID`, save (SOPS auto-re-encrypts on
write), commit. Flux picks up the change on next source reconcile.

The legacy SSH deploy key (`secrets/shared/flux-deploy-key.yaml` for ducktape)
is not part of the cold-start bootstrap path. The former tofu-managed
`gaffer-private-deploy-key` and its fine-grained PAT bootstrap path were
successfully destroyed and removed; do not recreate them. Gaffer-private uses
the GitHub App authentication above.

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

### forgejo-agentydragon

The `forgejo-agentydragon` tofu-controller module attaches the public halves of
the SOPS-backed per-host Forgejo SSH keys to the existing OIDC-created
`agentydragon` Forgejo account. It depends on Forgejo and the tofu PG backend,
and writes state to the `forgejo_agentydragon` schema.

**If `flux-webhook` receiver stops working after bootstrap**: Check `flux-webhook-token`
reconciliation. The token and webhook URL use `ignore_changes` and are stable across
re-applies — only resource recreation changes them.

### ActivityWatch access

ActivityWatch is active again with the repo-owned incremental importer. The central
server has three separately bounded surfaces: the Authentik-protected human UI at
`https://activitywatch.allegedly.works`, the bearer-gated write route at
`https://activitywatch-write.allegedly.works`, and the read-only bearer route at
`https://activitywatch-read.allegedly.works` used by Haku's egress substitution.
The importer and route contract are the SSOT in
[`cluster/docs/activitywatch/README.md`](activitywatch/README.md); the SOPS source
for the importer tokens is next to the ActivityWatch manifests.

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

External service credentials and generated bot credentials stored in
`cluster/k8s/**/*.sops.yaml`. If lost, re-enter from the external service or
regenerate, then SOPS-encrypt.

| Category         | File(s)                                                                                                                                                                                                                      | External Source                                                |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| **AWS**          | `cert-manager/config/base/aws-credentials.sops.yaml`, `dns-automation/aws-credentials.sops.yaml`                                                                                                                             | AWS IAM console (DNS-01 / Route 53 access)                     |
| **GitHub**       | `external-creds/github-agentydragon-agent.sops.yaml`, `external-creds/github-agentydragon.sops.yaml`, `flux-system/ducktape-automation-github-app.sops.yaml`                                                                 | GitHub settings → PAT / App                                    |
| **BuildBuddy**   | `x/buildbuddy-executor/api-key.sops.yaml`, `agents/shared-secrets/buildbuddy-api-key.sops.yaml`                                                                                                                              | BuildBuddy org settings                                        |
| **Anthropic**    | `external-creds/anthropic-haku.sops.yaml`                                                                                                                                                                                    | Anthropic console                                              |
| **Google AI**    | `external-creds/gemini.sops.yaml`                                                                                                                                                                                            | Google AI console                                              |
| **Groq**         | `external-creds/groq.sops.yaml`                                                                                                                                                                                              | Groq console                                                   |
| **Analysis AI**  | `external-creds/analysis-ai.sops.yaml`                                                                                                                                                                                       | Analysis AI                                                    |
| **Google OAuth** | `agents/airlock/google-client-credentials.sops.yaml`                                                                                                                                                                         | Google Cloud console                                           |
| **Financial**    | `agents/coinbase-read/coinbase-api-credentials.sops.yaml`                                                                                                                                                                    | Coinbase portal                                                |
| **Messaging**    | `agents/shared-secrets/openclaw-telegram-bot-token.sops.yaml`, `flux-webhook/ntfy-webhook.sops.yaml`, `matrix/secrets/haku-matrix-bot-password.sops.yaml`, `matrix/secrets/public-coder-agent-matrix-bot-password.sops.yaml` | Telegram @BotFather / ntfy.sh / generated Matrix bot passwords |
| **OAuth**        | `agents/airlock/oura-client-credentials.sops.yaml`                                                                                                                                                                           | Oura developer portal                                          |
| **Agent infra**  | `agents/shared-secrets/attic-push-token.sops.yaml`, `agents/claude-sandbox-secrets/claude-web-age-key.sops.yaml`                                                                                                             | Generated / internal                                           |
| **Nix cache**    | `nix-cache/app/jwt-token.sops.yaml`                                                                                                                                                                                          | Generated at bootstrap                                         |
| **CNPG**         | `tofu-state/db/credentials.sops.yaml`                                                                                                                                                                                        | Generated (random password)                                    |

## Recovery Scenarios

### Full bootstrap from zero

1. Ensure L0 credentials exist (SSH key, GitHub access)
2. Ensure L1 SOPS secrets exist in git (nebula-ca, cluster age key, app credentials)
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
3. Regenerate and persist every node identity (see <secrets.md> "Generating a new cert")
4. `tofu apply` to embed the new Talos node certificates and keys
5. `nixos-rebuild switch` on NixOS workers; `ansible-playbook atlas.yaml --tags nebula`

### Lost cluster age key

Regenerate and redeploy per <secrets.md> § "Rotating the Cluster Age Key". The
key is also SOPS-backed in `secrets/shared/cluster-secrets-age.yaml`, so it
survives tofu-state loss.

### Lost single app credential

1. Get new credential from external service
2. Edit the `*.sops.yaml` file: `sops cluster/k8s/<path>.sops.yaml`
3. Commit + push; Flux deploys automatically
