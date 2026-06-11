# Nix Binary Cache (Attic)

Attic server at `cache.allegedly.works`, backed by PostgreSQL (CNPG) and local
storage on a `local-path` PVC. Manifests in `k8s/nix-cache/`.

## Architecture

- **Server**: `ghcr.io/zhaofengli/attic:latest` (busybox-based Rust image)
- **Database**: CNPG cluster `attic-db` (1 instance, Proxmox-single, `local-path`)
- **Cache storage**: 30Gi `local-path` PVC at `/cache`
- **Caches** (private, priority 41, server-generated ED25519 keypairs):
  - `main` — ducktape CI's general-purpose cache (flake outputs)
  - `gaffer` — gaffer-private CI's cache (drivefs and friends)
- **Trusted public keys** (consumer side, pinned in
  `nix/nixos/modules/attic-substituter.nix`):
  - `main:cy5xhwCNq/T7R55I9TaLv0z6SM6EipXvdFhqrbxC7nc=`
  - `gaffer:Z8sM2kptUUDGk4ARVD/YkcpzWdMgmZX7nVLV5joK7r8=`

Caches are created (and signing keypairs generated) by the bootstrap Job in
`cluster/k8s/nix-cache/bootstrap/`. Re-running the Job is idempotent: GET the
cache config first, only POST when missing. On a full cluster wipe, the
new server generates fresh keypairs — consumer pubkeys must then be updated.

## Secrets

| Secret            | Source                                         | Contains                                                             |
| ----------------- | ---------------------------------------------- | -------------------------------------------------------------------- |
| `attic-jwt-token` | SOPS (`k8s/nix-cache/app/jwt-token.sops.yaml`) | HS256 secret. atticadm signs JWTs with it; server validates with it. |
| `attic-db-app`    | CNPG-generated                                 | PostgreSQL connection URI                                            |

Tokens (admin, per-host readers, CI writers) are HS256 JWTs signed with the
`attic-jwt-token` secret. Reader/writer tokens are auto-rotated by
`cluster/k8s/agents/attic-jwt-rotation/` (single CronJob driven by
`rotators.json`); admin tokens are minted ad hoc via `kubectl exec`.

(Cache **signing** keypairs — distinct from the JWT signing secret — live in
attic's Postgres DB per cache, server-generated, never extracted.)

## Bootstrap

The bootstrap Job in `cluster/k8s/nix-cache/bootstrap/` runs on every Flux
reconcile of the `nix-cache-bootstrap` Kustomization. It mints a 5-minute
admin JWT via `kubectl exec deploy/attic -- atticadm`, then for each cache
in `bootstrap.sh`'s `$CACHES` list:

1. `GET /_api/v1/cache-config/<name>` — exists?
2. If 200: log "exists", skip.
3. If 4xx: `POST /_api/v1/cache-config/<name>` with
   `{"keypair":"Generate","is_public":false,"store_dir":"/nix/store","priority":41,"upstream_cache_key_names":[]}`.

Public keys aren't auto-published back to git (TODO); after a cluster wipe,
fetch the new pubkeys via:

```bash
JWT=$(kubectl -n nix-cache exec deploy/attic -- \
  atticadm -f /config/server.toml make-token \
  --sub fetch-pubkey --validity '5 minutes' --pull '*')
for cache in main gaffer; do
  curl -sSf -H "Authorization: Bearer $JWT" \
    "https://cache.allegedly.works/_api/v1/cache-config/$cache" \
    | jq -r '.public_key'
done
```

…and paste into `nix/nixos/modules/attic-substituter.nix` `trusted-public-keys`.

## CI Push

Both ducktape and gaffer-private CI push to their respective caches. Writer
JWTs auto-rotated by the cluster (1-year validity, mint-on-staleness):

| Repo           | Workflow                               | Cache    | Reads token from                                                         |
| -------------- | -------------------------------------- | -------- | ------------------------------------------------------------------------ |
| ducktape       | `.github/workflows/nix-attic-push.yml` | `main`   | `secrets/ci/attic-main-writer.sops.yaml`                                 |
| gaffer-private | `.github/workflows/nix-attic-push.yml` | `gaffer` | `secrets/ci/attic-gaffer-writer.sops.yaml` (sparse-cloned from ducktape) |

Both files are encrypted with the CI age key, already on both repos as
`SOPS_AGE_KEY` (synced by `tf/gitops/github-secrets-sync/main.tf`).

## Pulling (NixOS Hosts)

Substituter wiring is via `ducktape.attic-substituter.enable` in
`nix/nixos/modules/attic-substituter.nix`. It renders `/run/secrets/rendered/attic-netrc`
from a per-host SOPS reader JWT (`secrets/hosts/<host>-attic.yaml`,
auto-rotated) and points `nix.settings.netrc-file` at it.

```nix
ducktape.attic-substituter = {
  enable = true;
  sopsFile = ../../../../secrets/hosts/wyrm2-attic.yaml;
};
```

The reader token is minted with `--pull main --pull gaffer`, so the same
file unlocks both caches.

## Environment Variables

Attic uses serde defaults for env var fallback — values must be **absent** from
`server.toml` for the env var to take effect (TOML values always win).

| Env Var                                  | TOML Field                              | Source                   |
| ---------------------------------------- | --------------------------------------- | ------------------------ |
| `ATTIC_SERVER_DATABASE_URL`              | `database.url`                          | `attic-db-app` secret    |
| `ATTIC_SERVER_TOKEN_HS256_SECRET_BASE64` | `jwt.signing.token-hs256-secret-base64` | `attic-jwt-token` secret |

## Known Issues

The Attic image is busybox-based (no bash on PATH for runc startup, no
default `/tmp`); the bootstrap Job runs the rotator image
(`ghcr.io/agentydragon/attic-jwt-rotation`) instead, kubectl-execing into
the live attic pod to mint JWTs and curling the REST API for cache CRUD.

The `signing-key` Secret mounted at `/var/lib/secrets` in the attic
deployment is vestigial — leftover from a prior nix-serve/harmonia setup.
attic only reads keys from its DB. Schedule for removal in a follow-up.

The previous incarnation of this repo had a plaintext private signing key
checked into `cluster/terraform/main/nix-cache-key.json`. The cache it
described was never actually created in attic, so the leaked key never
signed any live closures; it has been deleted and the matching pubkey
removed from `trusted-public-keys`. The `main` cache now exists with a
fresh server-generated keypair.
