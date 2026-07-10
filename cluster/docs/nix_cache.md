# Nix Binary Cache (Attic)

Attic server at `cache.allegedly.works`, backed by PostgreSQL (CNPG) for
metadata and a SeaweedFS S3 bucket for NAR chunk storage. Manifests in
`k8s/nix-cache/`.

## Architecture

- **Server**: `ghcr.io/zhaofengli/attic:latest` (busybox-based Rust image)
- **Database**: CNPG cluster `attic-db` (2 instances, OVH-HA, `local-path-ovh`)
- **Cache storage**: SeaweedFS S3 bucket `attic` (`seaweedfs-s3.seaweedfs:8333`), replicated `001` across OVH volume servers
- **Caches** (priority 41, server-generated ED25519 keypairs):
  - `main` — private; ducktape CI's general-purpose cache (flake outputs)
  - `gaffer` — private; gaffer-private CI's cache (drivefs and friends)
  - `public` — anonymous-readable (`is_public: true`); carries only the Claude Code
    web/Haku bootstrap closures (`devtools`/`bb`/`bbr`/`bbapi`/`agent-haku`/
    `devShells.default`) — see "Public bootstrap cache" below
- **Trusted public keys** (consumer side): single source of truth is
  `nix/attic-pubkeys.json`, consumed by both
  `nix/nixos/modules/attic-substituter.nix` and the `nix-attic-push` CI
  workflow. Update that file on cluster rebuild (see Bootstrap below).

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
`rotators.yaml`); admin tokens are minted ad hoc via `kubectl exec`.

(Cache **signing** keypairs — distinct from the JWT signing secret — live in
attic's Postgres DB per cache, server-generated, never extracted.)

## Bootstrap

The bootstrap Job in `cluster/k8s/nix-cache/bootstrap/` runs on every Flux
reconcile of the `nix-cache-bootstrap` Kustomization (`job.yaml`'s
`kustomize.toolkit.fluxcd.io/force: enabled` recreates it on every change so it
always re-runs). It mints a 5-minute admin JWT via `kubectl exec deploy/attic
-- atticadm`, then for each `--cache`/`--public-cache` arg
(`cluster/rotators/attic_jwt_rotation/rotate.py`'s `bootstrap-caches`
subcommand — the previous shell-script implementation this doc used to
describe is gone):

1. `GET /_api/v1/cache-config/<name>` — exists?
2. If 200: log "exists", skip (existing caches are never reconfigured — flipping
   `is_public` on one needs a direct API call, not this command).
3. If 4xx: `POST /_api/v1/cache-config/<name>` with
   `{"keypair":"Generate","is_public":<bool>,"store_dir":"/nix/store","priority":41,"upstream_cache_key_names":[]}`
   — `is_public` is `true` only for `--public-cache` args (currently just `public`).

Public keys aren't auto-published back to git (TODO); after a cluster wipe (or
after adding a new cache — e.g. `public`), fetch the new pubkeys via:

```bash
JWT=$(kubectl -n nix-cache exec deploy/attic -- \
  atticadm -f /config/server.toml make-token \
  --sub fetch-pubkey --validity '5 minutes' --pull '*')
for cache in main gaffer public; do
  curl -sSf -H "Authorization: Bearer $JWT" \
    "https://cache.allegedly.works/_api/v1/cache-config/$cache" \
    | jq -r '.public_key'
done
```

…and paste into `nix/attic-pubkeys.json` (the single source of truth, read by
both `nix/nixos/modules/attic-substituter.nix` and the `nix-attic-push` workflow).

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
needed ever), carrying **only** the web/Haku bootstrap closures —
`devtools`/`bb`/`bbr`/`bbapi`/`agent-haku`/`devShells.default` — pushed
alongside the existing `main` push by
`devinfra/ci/nix_attic_build_and_push.sh` (same content, deduped by NAR hash;
cheap). `devinfra/claude/web_setup.sh` lists `public` before `main` in
`extra-substituters`, so the very first install substitutes anonymously and
every later one still gets `main`+`gaffer` once authenticated. `main`/`gaffer`
stay exactly as private as before — `public` is a strict addition, not a
downgrade of either.

**Rollout after adding a cache:** (1) Flux reconciles the bootstrap Job,
creating the cache; (2) fetch its pubkey (above — for a public cache,
`/_api/v1/cache-config/<name>` answers anonymously) and commit it to
`nix/attic-pubkeys.json` — until then, Nix would refuse anything substituted
from it on signature-verification grounds even though the substituter is
configured; (3) scope changes in `rotators.yaml` (e.g. a writer JWT gaining
`push: [main, public]`) propagate on the next hourly `attic-jwt-rotation` run
by themselves — `rotate_one` re-mints when the stamped `pull_unencrypted` /
`push_unencrypted` scope diverges from the configured one, not just on
staleness; (4) run `nix-attic-push.yml` (push to `devel` or
`workflow_dispatch`) so CI pushes the new cache's closures for the first time.

## CI Push

Both ducktape and gaffer-private CI push to their respective caches. Writer
JWTs auto-rotated by the cluster (1-year validity; re-mint on <24h remaining
or on pull/push scope drift vs. `rotators.yaml`):

| Repo           | Workflow                               | Cache            | Reads token from                                                         |
| -------------- | -------------------------------------- | ---------------- | ------------------------------------------------------------------------ |
| ducktape       | `.github/workflows/nix-attic-push.yml` | `main`, `public` | `secrets/ci/attic-main-writer.sops.yaml`                                 |
| gaffer-private | `.github/workflows/nix-attic-push.yml` | `gaffer`         | `secrets/ci/attic-gaffer-writer.sops.yaml` (sparse-cloned from ducktape) |

Both files are encrypted with the CI age key, already on both repos as
`SOPS_AGE_KEY` (synced by `tf/gitops/github-secrets-sync/main.tf`).

## Private-binary isolation (drivefs/drivectl)

`drivefs` (the Google Drive binary) and `drivectl` must stay in the restricted
`gaffer` cache and **never** land in the broadly-readable `main` cache. The boundary that
enforces this is the **narinfo**: each cache signs and serves its own narinfos,
gated by per-cache JWT scope. `main` and `gaffer` physically share the one
`attic` S3 bucket of content-addressed chunks, but chunks are useless without
the gaffer narinfo (NAR hash → ordered chunk list), so narinfo scoping is the
real access boundary even though chunk bytes coexist.

The leak it guards against: `nix/home/modules/google-drive.nix`, when
`services.google-drive.enable = true` (wyrm2, rugged), pulls `drivefs` and
`drivectl` via
`builtins.fetchClosure` from `gaffer` into the local store **at eval time**. If a
closure containing them were pushed to `main`, their narinfos would land in
`main`, pullable by anyone with `main:pull`.

So `devinfra/ci/nix_attic_build_and_push.sh` forces google-drive **off** for any
config that enables it before pushing to `main`, detected by reading the
`services.google-drive.enable` bool (cheap; it does not fetch `drivefs`, which
lives behind `config = lib.mkIf cfg.enable`):

- NixOS hosts: for each home-manager user reading `enable = true` (wyrm2,
  rugged), `extendModules` injects
  `home-manager.sharedModules += { services.google-drive.enable = mkForce false; }`.
- Home configs: the same check on the config directly (none enable it today).

Configs where the option is absent or false build as-is — they can't reference
`drivefs`, and injecting an undeclared option would error. Crucially this covers
configs that have **home-manager but not the google-drive module** (`bazel-test`'s
`root` user; the standalone `claude-web` profile): the bool read errors → treated
as not-enabled → built untouched.

With it off, `config = lib.mkIf cfg.enable {…}` never references those private
packages, so they are never fetched and never enter a pushed closure. Real hosts
deploy with google-drive **on**: `nixos-rebuild switch` pulls `drivefs` and
`drivectl` straight from `gaffer` (their reader JWT carries `--pull gaffer`) and
rebuilds only the cheap home-manager generation diff.

**Invariant:** the override targets exactly the configs whose
`services.google-drive.enable` reads `true`, so a new google-drive host is covered
automatically and a config that lacks the module is never mis-targeted (the bug
that the earlier "force off everywhere with home-manager" approach hit on
`bazel-test`).

**Storage-layer follow-up (not done):** for defense-in-depth — so even a broad
`attic`-bucket reader key (SeaweedFS `claude-reader`, which holds `Read:attic`)
can't touch `drivefs` chunks — `gaffer` would need its **own S3 bucket** with
separate credentials. The narinfo boundary above is the current line of defense.

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
