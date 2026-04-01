# Secrets Strategy

## TL;DR

- **SSOT**: OpenTofu state in PG backend (CNPG `tofu-state-db`, schema `main`)
- **Bootstrap secrets**: SealedSecrets (encrypted in git, decrypted by controller using stable keypair)
- **Runtime secrets**: External Secrets Operator reading from Vault
- **Keypair flow**: tofu state → apply deploys to cluster → controller uses it

## Architecture Overview

### Three-Layer Model

**Layer 0 — Persistent Auth** (`terraform/main/persistent-auth.tf`)

- Sealed secrets keypair (RSA 4096, 10-year validity)
- Proxmox API tokens (CSI, OpenTofu)
- Nebula mesh PKI (CA cert + per-node certs/keys)
- Nix cache signing key, Flux deploy key
- Storage: PG backend (CNPG `tofu-state-db`, schema `main`)
- Resources have `lifecycle { prevent_destroy = true }`
- Note: Talos machine secrets are ephemeral (fresh `cluster.id` per lifecycle)

↓

**Layer 1 — SealedSecrets** (git repo → cluster)

- `k8s/proxmox-csi/secrets/proxmox-csi-sealed.yaml`
- `k8s/nix-cache/app/signing-key-sealed.yaml`
- `k8s/nix-cache/app/jwt-token-sealed.yaml`
- Sealed with keypair from Layer 0, deployed by Flux, decrypted by sealed-secrets controller

↓

**Layer 2 — Vault + ESO** (runtime secrets)

- External Secrets Operator reads from Vault
- Creates K8s secrets from Vault KV paths
- Used for: application passwords, SSO credentials, etc.

## Data Flow

### Bootstrap Flow (tofu apply)

1. Targeted apply creates/uses keypair from tofu state (PG backend)
2. `proxmox` provider manages Proxmox users, roles, and API tokens
   (authenticated via `PROXMOX_VE_API_TOKEN` env var, `root@pam`)
3. `kubeseal` creates SealedSecrets (writes to k8s/\*.yaml)
4. User commits SealedSecrets to git manually
5. Full apply deploys keypair as `kubernetes_secret` to cluster (direct references, same root)
6. Flux deploys SealedSecrets from git
7. Controller decrypts using deployed keypair → creates regular Secrets

### Keypair Locations

| Location                                     | Purpose                                    |
| -------------------------------------------- | ------------------------------------------ |
| PG backend (`tofu-state-db`, schema `main`)  | SSOT (tofu state)                          |
| `k8s/sealed-secrets/sealed-secrets-cert.pem` | Public cert committed to repo (TF-managed) |
| `kube-system/sealed-secrets-key`             | Full keypair deployed to cluster           |
| Git SealedSecrets                            | Encrypted with public cert                 |

The public certificate at `k8s/sealed-secrets/sealed-secrets-cert.pem` is managed by a
`local_file` resource in `persistent-auth` terraform. It contains only the public half
of the keypair (safe to commit — enables encrypting new SealedSecrets, not decrypting
existing ones). `seal-secret.sh` reads this file directly.

## SealedSecrets in Repository

| File                                              | Purpose                 | Namespace   |
| ------------------------------------------------- | ----------------------- | ----------- |
| `k8s/proxmox-csi/secrets/proxmox-csi-sealed.yaml` | CSI driver credentials  | csi-proxmox |
| `k8s/nix-cache/app/signing-key-sealed.yaml`       | Nix cache signing       | nix-cache   |
| `k8s/nix-cache/app/jwt-token-sealed.yaml`         | Attic JWT token         | nix-cache   |
| `k8s/dns-automation/aws-credentials-sealed.yaml`  | AWS Route 53 API access | flux-system |

## Common Failure Modes

### Keypair Mismatch

**Symptom**: `no key could decrypt secret` error on SealedSecret

**Cause**: SealedSecret in git was sealed with a different keypair than what's in tofu state

**Fix**: Re-run `tofu apply` in `terraform/main` to re-seal with correct keypair

### OpenTofu State Lost

**Symptom**: New keypair generated, all SealedSecrets fail

**Prevention**:

- PG backend with backup CronJob (`pg_dump` every 6 hours to `proxmox-csi-retain` PVC)
- Never drop the `main` schema in `tofu-state-db` unless intentional full reset

## Validation

Pre-commit hook validates all SealedSecrets can be decrypted with tofu keypair:

```bash
# Validation uses kubeseal --recovery-unseal (works offline, no cluster needed)
# Runs as part of the unified pre-commit hook:
bazel run //devinfra/precommit
```

## Adding New SealedSecrets

1. Create secret YAML with `kubectl create secret ... --dry-run=client -o yaml`
2. Seal with the committed public cert via the helper script:

   ```bash
   kubectl create secret generic my-secret --from-literal=key=value \
     --dry-run=client -o yaml | ./scripts/seal-secret.sh /dev/stdin k8s/path/my-sealed.yaml
   ```

   The script reads `k8s/sealed-secrets/sealed-secrets-cert.pem` (falls back to tofu state
   if the file is missing).

3. Add to appropriate kustomization.yaml
4. Commit and push

## Keypair Verification

Compare serial numbers (committed cert and cluster should match):

```bash
# Committed cert:
openssl x509 -noout -serial < k8s/sealed-secrets/sealed-secrets-cert.pem

# Cluster:
kubectl get secret sealed-secrets-key -n kube-system -o jsonpath='{.data.tls\.crt}' | \
  base64 -d | openssl x509 -noout -serial
```

## Re-sealing All Secrets

If keypair mismatch occurs:

```bash
cd terraform/main && tofu apply
git add k8s/proxmox-csi/secrets/proxmox-csi-sealed.yaml
git commit -m "chore: re-seal secrets with current keypair"
git push
```
