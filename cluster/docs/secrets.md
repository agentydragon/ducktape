# Secrets Strategy

## TL;DR

- **Bootstrap secrets**: SOPS-encrypted in git (`*.sops.yaml`), decrypted by Flux
- **Encryption keys**: Age keypairs in `.sops.yaml` (admin + cluster keys)
- **Full dependency graph**: <bootstrap_dependencies.md>

## Architecture

Secrets are age-encrypted YAML files committed to git (`*.sops.yaml`). Flux
decrypts them using the cluster age key (`sops-age-cluster-secrets` in `flux-system`).

Files in `cluster/k8s/**/*.sops.yaml` contain app credentials, API keys, and
infrastructure tokens. Files in `secrets/*.yaml` contain infrastructure secrets
(Nebula CA, cluster age keypair, legacy auth keys).

## Age Keys

Generic SOPS rules — `.sops.yaml` path matching (encrypt in-place at the final repo
path; `sops -e /tmp/...` fails with "no matching creation rules") and `SOPS_AGE_KEY`
derivation from `~/.ssh/id_ed25519` — live in root `AGENTS.md` § SOPS. The
cluster-specific keys and their storage:

Defined in `.sops.yaml` creation rules:

| Key                              | Purpose                                       | Storage                                                                                        |
| -------------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Admin age key (`age1u858...`)    | Decrypt all secrets locally                   | Derived from `~/.ssh/id_ed25519` via ssh-to-age                                                |
| Cluster age key (`age1nywe...`)  | Flux decrypts `k8s/**/*.sops.yaml` in-cluster | `secrets/shared/cluster-secrets-age.yaml` → deployed to `flux-system/sops-age-cluster-secrets` |
| Host keys (wyrm2, rugged, atlas) | Per-host sops-nix secrets                     | Derived from host SSH keys                                                                     |

## Adding New SOPS Secrets

```bash
# Create a new SOPS-encrypted secret
sops cluster/k8s/<app>/secrets/my-secret.sops.yaml
```

SOPS uses `.sops.yaml` creation rules to determine which age keys encrypt the
file based on its path. Commit and push — Flux deploys automatically.

The owning flux-kustomization must declare `spec.decryption`, or Flux applies
the `ENC[...]` ciphertext literally and silently (no error — see the failure
mode below). Every kustomization applying a `.sops.yaml` needs:

```yaml
spec:
  decryption:
    provider: sops
    secretRef:
      name: sops-age-cluster-secrets
```

Enforced at PR time by the `test_sops_decryption` validation check.

## Rotating Credentials

1. Get new credential from external service
2. `sops cluster/k8s/<path>.sops.yaml` — edit the value
3. Commit + push; Flux deploys; Stakater Reloader restarts affected pods

## Rotating the Cluster Age Key

1. Generate: `age-keygen -o /dev/stdout`
2. Update `secrets/shared/cluster-secrets-age.yaml` with new keypair
3. Update `.sops.yaml` with new public key
4. Re-encrypt all cluster secrets: `for f in $(find cluster/k8s -name '*.sops.yaml'); do sops updatekeys "$f"; done`
5. Redeploy the k8s secret: `cd terraform/main && tofu apply -target=kubernetes_secret.sops_age_cluster_secrets`
6. Commit + push

## Common Failure Modes

### SOPS Decryption Failure in Flux

**Symptom**: Kustomization shows `sops decryption error`

**Cause**: Cluster age key in `flux-system/sops-age-cluster-secrets` doesn't
match the key used to encrypt the file.

**Fix**: the deployed cluster age key is stale. Confirm it's present, then
re-encrypt and redeploy per [Rotating the Cluster Age Key](#rotating-the-cluster-age-key):

```bash
kubectl get secret sops-age-cluster-secrets -n flux-system
```

### SOPS Secret Applied as Ciphertext (Silent)

**Symptom**: the consumer gets garbage bytes or auth failures (401, bogus
credentials), but the Kustomization shows `Ready=True` with no error. The live
Secret's data values are `ENC[AES256_GCM,…]` instead of plaintext.

**Cause**: the flux-kustomization is missing `spec.decryption` (or declares
`provider: sops` without a `secretRef`), so Flux never decrypts — it applies
the ciphertext literally. Unlike the loud failure above, this emits no error.

**Fix**: add the `decryption` block (see
[Adding New SOPS Secrets](#adding-new-sops-secrets)). Incident history and
detail: <lessons_learned/2026_07_04_flux_sops_ciphertext_applied_literally.md>.
Enforced at PR time by `test_sops_decryption`.

### OpenTofu State Lost

**Symptom**: SOPS age secret not deployed to cluster; Flux can't decrypt

**Prevention**:

- PG backend in the `tofu-state-db-ovh` CNPG cluster (automated offsite backup is a pending TODO — see <plan.md>)
- Age keypair also stored in `secrets/shared/cluster-secrets-age.yaml` (SOPS-encrypted
  with admin key) — survives tofu state loss

## Nebula Certificate Material

Every Nebula identity lives in `secrets/nebula/`: a plaintext public `.crt` and
a SOPS binary private `.sops.key`. For Tofu-managed Talos nodes, OpenTofu reads
those files with `local_file` and the `carlpett/sops` provider, then embeds them
in the machine-config Nebula extension. It never generates a node key during an
apply. Non-Talos nodes (wyrm2, rugged, iguana, atlas, and mobile clients such as
pixel6) consume the same file pattern through their host configuration.

```text
secrets/nebula/
  ca.crt              # plaintext PEM — CA public cert (shared)
  ca.sops.key         # SOPS binary — CA private key (admin only)
  wyrm2.crt           # plaintext PEM — host public cert
  wyrm2.sops.key      # SOPS binary — host private key (admin + host)
  ...
```

Certs are inspectable without decryption: `nebula-cert print -path secrets/nebula/wyrm2.crt`

### Generating a new cert

```bash
# Decrypt CA key (requires admin age key)
TMPCA=$(mktemp -d)
sops -d secrets/nebula/ca.sops.key > "$TMPCA/ca.key"

# Sign — FQDN must be {host}.nebula.allegedly.works, IP from the cert being rotated
# (or pick a free 10.42.0.x/16 for new nodes)
nebula-cert sign \
  -ca-crt secrets/nebula/ca.crt \
  -ca-key "$TMPCA/ca.key" \
  -name "HOST.nebula.allegedly.works" \
  -ip "IP/16" \
  -out-crt secrets/nebula/HOST.crt \
  -out-key "$TMPCA/host.key"

# Encrypt the private key as SOPS binary
cp "$TMPCA/host.key" secrets/nebula/HOST.sops.key
sops -e -i secrets/nebula/HOST.sops.key

# Clean up
rm -rf "$TMPCA"
```

### Deploying

- **NixOS workers** (wyrm2, rugged, iguana): `nixos-rebuild switch` — certs
  deployed via `environment.etc`, key via sops-nix binary format
  (`nix/nixos/modules/k8s-worker-sops.nix`)
- **atlas**: `ansible-playbook atlas.yaml --tags nebula` — certs copied from
  plaintext files, key decrypted from SOPS binary
- **Mobile Nebula clients**: render a plaintext import file locally, transfer it
  to the device, import it with Mobile Nebula's "From file" flow, then delete the
  plaintext copy from transit/storage locations.

### Mobile Nebula import config

Mobile Nebula imports a normal Nebula YAML file with inline `pki.ca`,
`pki.cert`, and `pki.key`. The app stores the private key in the phone's app
keystore after import. Generate the plaintext file locally from the repo's SOPS
key and shared lighthouse topology:

```bash
bb run //cluster/scripts:render_mobile_nebula_config -- \
  pixel6 \
  --output /tmp/pixel6.mobile-nebula.yaml
```

The generated config reads lighthouse IPs and public endpoints from
`nebula-mesh.json`, so VPS endpoint changes do not need hand-edits in the phone
config. It also lowers the mobile TUN to the smallest declared
`destination_mtu`, because mobile platforms do not expose Linux-style
per-destination route MTUs. Regenerate and re-import after either topology or
MTU policy changes. DNS is omitted by default because Android applies Mobile
Nebula's resolvers to the whole VPN rather than implementing true per-domain
split DNS. Use `--dns` only when that global behavior is intentional;
otherwise, reach mesh services by direct `10.42.x.y` addresses.

### After cert rotation

1. Commit the new `.crt` and `.sops.key` files
2. Deploy (nixos-rebuild, ansible, or push for Flux)
3. Restart nebula: `sudo systemctl restart nebula`

## Validation

Pre-commit validates SOPS files can be decrypted:

```bash
pre-commit run --all-files
```
