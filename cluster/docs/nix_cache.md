# Nix Binary Cache (Attic)

Attic server at `cache.allegedly.works`, backed by PostgreSQL (CNPG) for
metadata and a replicated Piraeus volume for NAR chunk storage. Manifests in
`k8s/nix-cache/`.

## Architecture

- **Server**: `attic-next`, pinned to the previous server's image digest
- **Database**: database `attic_local` in CNPG cluster `attic-db` (2 instances on OVH Piraeus SSD)
- **Recovery**: the database is not backed up. Bootstrap recreates cache identities using
  stored signing keys, but rediscovery of existing chunks after database loss is unverified.
- **Cache storage**: `attic-local-storage`, a 100 GiB `piraeus-hdd-2` volume with two synchronous copies in OVH
- **Caches** (priority 41, ED25519 keypairs supplied from SOPS — see Bootstrap):
  - `main` — private; ducktape CI's general-purpose cache (flake outputs)
  - `gaffer` — private; gaffer-private CI's cache (drivefs and friends)
  - `public` — anonymous-readable (`is_public: true`); carries the Claude Code
    web/Haku and pre-commit CI bootstrap closures (`devtools`/`precommit`/`bb`/
    `bbr`/`bbapi`/`agent-haku`/`devShells.default`) — see "Public bootstrap cache" below
- **Trusted public keys** (consumer side): `nix/attic-pubkeys.json`, consumed
  by both `nix/nixos/modules/attic-substituter.nix` and the `nix-attic-push`
  CI workflow. Generated from `cache-keys.sops.yaml` (see Bootstrap below) —
  that SOPS file is the real source of truth.

Caches are created by the bootstrap Job in `cluster/k8s/nix-cache/`, with the
signing keypair supplied from `cache-keys.sops.yaml` rather than server-generated.
Re-running the Job is idempotent: GET the cache config first, only POST when
missing. Because the keypair is supplied and stable, a full cluster wipe no
longer invalidates trust or requires updating consumer pubkeys — the recreated
cache signs with the same key as before.

## Secrets

| Secret             | Source                                      | Contains                                                                                 |
| ------------------ | ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `attic-jwt-token`  | SOPS (`k8s/nix-cache/jwt-token.sops.yaml`)  | HS256 secret. atticadm signs JWTs with it; server validates with it.                     |
| `attic-cache-keys` | SOPS (`k8s/nix-cache/cache-keys.sops.yaml`) | Per-cache ED25519 signing keypairs, one `nix key generate-secret` string per cache name. |
| `attic-db-app`     | CNPG-generated                              | PostgreSQL connection URI                                                                |

Tokens (admin, per-host readers, CI writers) are HS256 JWTs signed with the
`attic-jwt-token` secret. Reader/writer tokens are auto-rotated by the
`attic-jwt-rotation` CronJob (`cluster/cdk8s/nix_cache/attic.py`; its
`ROTATOR_CONFIG` is serialized as the `rotators.yaml` key in the
`attic-rotators-config` ConfigMap). The generated config, its
`attic-jwt-rotator` ServiceAccount, and the bootstrap Job that reuses it apply
in one ordered pass, rather than deadlocking across two Kustomizations depending
on each other. Admin tokens are minted ad hoc via `kubectl exec`.

Cache **signing** keypairs — distinct from the JWT signing secret — are sourced
from `attic-cache-keys` (mounted into the bootstrap Job at
`/secrets/cache-keys/<cache-name>`) and supplied to attic's cache-config API on
creation, rather than left to attic's own `Generate` option. This trades
attic's "never extracted" default for keys that survive a wipe of the Postgres
DB they'd otherwise live in exclusively.

## Bootstrap

The bootstrap Job is generated in `cluster/k8s/nix-cache/nix-cache.k8s.yaml` with
`kustomize.toolkit.fluxcd.io/force: enabled` so Flux can recreate it when its
immutable template changes. It mints a 5-minute admin JWT via `kubectl exec deploy/attic-next
-- atticadm`, then for each `--cache`/`--public-cache` arg
(`cluster/rotators/attic_jwt_rotation/rotate.py`'s `bootstrap-caches`
subcommand — the previous shell-script implementation this doc used to
describe is gone):

1. `GET /_api/v1/cache-config/<name>` — exists?
2. If successful: log the public key and skip creation (existing caches are never reconfigured — flipping
   `is_public`, or swapping in a different signing key, on one needs a direct
   API call, not this command).
3. If 404: `POST /_api/v1/cache-config/<name>` with a body carrying the cache's
   key from `--keypair-dir` (mounted `attic-cache-keys` Secret) when present:
   `{"keypair":{"Keypair":"<name>:<base64>"},"is_public":<bool>,"store_dir":"/nix/store","priority":41,"upstream_cache_key_names":[]}`
   — or `{"keypair":"Generate",...}` for a cache with no file under
   `--keypair-dir`. `is_public` is `true` only for `--public-cache` args
   (currently just `public`). Other GET failures stop the Job.

### Adding a new cache

1. Generate a keypair: `nix key generate-secret --key-name <cache>`.
2. Add it to `cluster/k8s/nix-cache/cache-keys.sops.yaml` (`sops <file>` to edit
   in place; an operator's age key is required — this repo's machine-level keys
   are encryption-only for cluster secrets, see AGENTS.md § SOPS). Add
   `--cache=<cache>` / `--public-cache=<cache>` to the bootstrap Job's args in
   `cluster/cdk8s/nix_cache/attic.py`, then run `bb run //cluster/cdk8s:generate_manifests`.
3. Regenerate `nix/attic-pubkeys.json` from the same file:
   `bb run //cluster/rotators/attic_jwt_rotation:sync_pubkeys_bin -- sync`
   (also needs that operator decrypt access — deliberately not something the
   in-cluster bootstrap Job or its container image can do, see
   `rotate.py`'s module docstring). Until this runs, Nix refuses anything
   substituted from the new cache on signature-verification grounds even
   though the substituter is configured.
4. Flux reconciles the bootstrap Job, which creates the cache with the
   supplied key on its next run.

## Public bootstrap cache

`main` and `gaffer` are both private (reader JWT required), which is fine for
every consumer except one: a fresh Claude Code web session's **first**
`nix profile install` runs as the environment-manager init script, before any
session credential (not just the Attic reader JWT — nothing session-scoped)
reaches it. Against a private-only cache that request 401s, Nix disables the
substituter, and the session builds ~42 devtools derivations from source
(~8 minutes) instead of substituting ~490 already-built paths (~1.4 GiB).
Root-caused and tracked as `claude-web-cold-start-attic-cache-2026-07` in
Haku's state.

Fix: a third cache, **`public`** (`is_public: true`, anonymous reads, no JWT
needed ever), carrying the web/Haku and pre-commit CI bootstrap closures —
`devtools`/`precommit`/`bb`/`bbr`/`bbapi`/`agent-haku`/`devShells.default` — pushed
alongside the existing `main` push by
`devinfra/ci/nix_attic_build_and_push.sh` (same content, deduped by NAR hash;
cheap). `devinfra/claude/web_setup.sh` lists `public` before `main` in
`extra-substituters`, so the very first install substitutes anonymously and
every later one still gets `main`+`gaffer` once authenticated. `main`/`gaffer`
stay exactly as private as before — `public` is a strict addition, not a
downgrade of either.

**Rollout after adding a cache:** (1) follow "Adding a new cache" above
(generate + SOPS-seal a keypair, regenerate `nix/attic-pubkeys.json`, let Flux
create it); (2) update token scopes in `ROTATOR_CONFIG` in
`cluster/cdk8s/nix_cache/attic.py` (e.g. give a writer JWT `push: [main,
public]`) and run `bb run //cluster/cdk8s:generate_manifests`. After Flux applies
the generated config, the next hourly `attic-jwt-rotation` run re-mints tokens
whose stamped `pull_unencrypted` / `push_unencrypted` scope differs from the
configured scope; it does not wait for token staleness; (3) run
`nix-attic-push.yml` (push to `devel` or
`workflow_dispatch`) so CI pushes the new cache's closures for the first time.

## CI Push

Both ducktape and gaffer-private CI push to their respective caches. Writer
JWTs auto-rotated by the cluster (1-year validity; re-mint on <24h remaining
or on pull/push scope drift vs. `ROTATOR_CONFIG` in
`cluster/cdk8s/nix_cache/attic.py`):

| Repo           | Workflow                               | Cache            | Reads token from                                         |
| -------------- | -------------------------------------- | ---------------- | -------------------------------------------------------- |
| ducktape       | `.github/workflows/nix-attic-push.yml` | `main`, `public` | Actions secret `ATTIC_TOKEN`, propagated from Kubernetes |
| gaffer-private | `.github/workflows/nix-attic-push.yml` | `gaffer`         | Actions secret `ATTIC_TOKEN`, propagated from Kubernetes |

The rotator publishes each writer only to its Kubernetes Secret. OpenTofu
propagates each token to the corresponding repository Actions secret; CI does
not decrypt a SOPS token file.

## Private-binary isolation (drivefs/drivectl)

Gaffer publishes `drivefs` and `drivectl` only to the restricted `gaffer` cache.
Each cache authorizes and signs its own narinfos. The caches share stored NAR chunks,
so publishing any closure that references a private binary to `main` or `public`
would make that binary available to readers of that cache.

[The flake's Attic targets](../../devinfra/ci/nix_attic_targets.nix) enforce this
boundary. `main` takes its top-level packages from `ducktapePkgs`. The full flake
package set also exports the private Gaffer packages. Before evaluating a NixOS or
standalone Home Manager closure for `main`, the target checks whether
`services.google-drive.enable` is true and forces it off for that configuration.
On NixOS it checks each Home Manager user. Missing options count as disabled, so
profiles that omit the module (such as `claude-web` and the `bazel-test` root user)
build without that override. `public` publishes only the bootstrap subset.
[The CI publisher](../../devinfra/ci/nix_attic_build_and_push.sh) selects one of
these target sets by cache name.

Keep this check outside the Google Drive module. Detecting the module's own
option from inside it previously caused recursive evaluation. Running a normal
host rebuild uses the real Google Drive setting and fetches the pinned closures
from `gaffer`. See [the Nix consumer guide](../../nix/docs/private_flake_inputs.md)
for pin updates, host opt-in, and cache access diagnostics.

## Pulling (Machines and Users)

Substituter wiring is via `ducktape.attic-substituter.enable` in
`nix/nixos/modules/attic-substituter.nix`. It renders `/run/secrets/rendered/attic-netrc`
from a per-host SOPS reader JWT (`secrets/hosts/<host>-attic.yaml`,
auto-rotated) and points the daemon's `nix.settings.netrc-file` at it.

```nix
ducktape.attic-substituter = {
  enable = true;
  sopsFile = ../../../../secrets/hosts/wyrm2-attic.yaml;
};
```

Home Manager's `ducktape.attic` module decrypts the same per-host SOPS file for
the machine's user and renders `~/.config/nix/attic-netrc`. Its `nix.settings`
adds both private substituters and their trusted public keys. This user-level
copy is required because `builtins.fetchClosure` opens its explicit cache URL
from the evaluating process rather than relying only on the daemon's root-only
netrc. Atlas is not NixOS, so it has only this Home Manager side.

The reader token is minted with `--pull main --pull gaffer`, so one per-host JWT
unlocks both caches for the daemon and the machine's user without a second
user-specific Attic credential.

## Environment Variables

Attic uses serde defaults for env var fallback — values must be **absent** from
`server.toml` for the env var to take effect (TOML values always win).

| Env Var                                  | TOML Field                              | Source                        |
| ---------------------------------------- | --------------------------------------- | ----------------------------- |
| `ATTIC_SERVER_DATABASE_URL`              | `database.url`                          | `attic-local-database` secret |
| `ATTIC_SERVER_TOKEN_HS256_SECRET_BASE64` | `jwt.signing.token-hs256-secret-base64` | `attic-jwt-token` secret      |

## Known Issues

The Attic image is busybox-based (no bash on PATH for runc startup, no
default `/tmp`); the bootstrap Job runs the rotator image
(`git.allegedly.works/ducktape-ci/attic-jwt-rotation`) instead, kubectl-execing into
the live attic pod to mint JWTs and curling the REST API for cache CRUD.

The previous incarnation of this repo had a plaintext private signing key
checked into `cluster/terraform/main/nix-cache-key.json`. The cache it
described was never actually created in attic, so the leaked key never
signed any live closures; it has been deleted and the matching pubkey
removed from `trusted-public-keys`. The `main` cache's current keypair is
unrelated, supplied from `cache-keys.sops.yaml` (see Bootstrap above) — SOPS
encryption is the intended way private key material lives in this repo,
unlike that earlier plaintext leak.

### CI token propagation

The rotator publishes separate Ducktape CI reader and writer JWTs to
`nix-cache/attic-ducktape-ci-reader` and
`nix-cache/attic-ducktape-ci-writer`. The reader pulls from `main` and `gaffer`
without push scope. After a successful token check, the rotator patches distinct
reader and writer version annotations on the GitHub secrets OpenTofu Workspace.
Crossplane's annotation watch queues reconciliation; `github-secrets-sync` reads
both Secrets and owns `ATTIC_READ_TOKEN` and `ATTIC_TOKEN` in the Ducktape
Actions secrets. Annotations carry Secret identity/version, never token
contents. The rotator retries notification on runs that do not mint a token, so
a failed notification does not wait for expiry. The cache signing keys,
per-host readers, and gaffer writer are unchanged.
