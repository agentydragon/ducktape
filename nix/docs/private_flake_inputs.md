# Private Flake Input Gotchas

Problems encountered when consuming private GitHub repos as Nix flake inputs,
particularly `gaffer-private` (`git+ssh://...?lfs=1`).

**Current status**: The `gaffer-private` flake input and all `google-drive`
module references are commented out in `flake.nix` and the host configs.
Search for `nix/docs/private_flake_inputs.md` to find all disabled sites.

## Problems

### 1. SSH agent not available under `sudo`

`sudo nixos-rebuild switch` runs Nix's git fetcher as root. Root doesn't
inherit the user's `SSH_AUTH_SOCK`, so SSH authentication to GitHub fails:

```
git@github.com: Permission denied (publickey).
```

**Workaround** (clunky — requires the user to remember the incantation):

```bash
sudo SSH_AUTH_SOCK="$SSH_AUTH_SOCK" nixos-rebuild switch --flake .#<host>
```

The SSH agent must have a key loaded (`ssh-add -l` to check).

### 2. `git-lfs` not on root's PATH → `narHash` mismatch

When a flake input uses `?lfs=1`, Nix's git fetcher shells out to `git-lfs`
to smudge LFS pointers into actual file content. If `git-lfs` is only
installed per-user (via home-manager's `programs.git.lfs.enable`), root
can't find it. The fetch silently succeeds but returns LFS pointer stubs
instead of real content, producing a different NAR hash than what `flake.lock`
recorded:

```
error: mismatch in field 'narHash' of input '...'
```

This is confusing because the rev and ref match — only the hash differs, with
no indication that LFS smudging failed.

**Fix** (not yet applied): Install `git-lfs` system-wide in
`nix/nixos/modules/base.nix` so root has it.

### 3. CI can't fetch private SSH inputs

GitHub Actions runners don't have SSH keys for private repos. A
`GITHUB_TOKEN` only works for `github:` scheme inputs, not `git+ssh://`.

## Attempted workaround: SOPS-encrypted GitHub PAT in nix-daemon

Commit `36271d26a` ("Wire gaffer-private fetch token into nix-daemon") tried
to solve problems 1 and 3 by:

- Storing a fine-grained GitHub PAT in SOPS
  (`secrets/shared/gaffer-private-fetch-pat.yaml`)
- Decrypting it via sops-nix into an `EnvironmentFile` for
  `nix-daemon.service`
- Exposing it as `GITHUB_TOKEN_GAFFER_PRIVATE`

This was reverted in `03367e978` ("Unwire drivectl release-asset rollout")
because it didn't actually work: the flake input uses `git+ssh://`, and Nix's
`access-tokens` / `GITHUB_TOKEN` mechanism only applies to `github:` scheme
URLs. The environment variable name (`GITHUB_TOKEN_GAFFER_PRIVATE`) also
wasn't in Nix's expected `access-tokens` format. The SOPS secret file still
exists on disk but is unused.

## Long-term fix

These problems go away if private packages are served from a Nix binary cache
(Attic, Cachix, etc.) instead of fetched as Git flake inputs. The cache
approach decouples build-time secrets (push token, needed only in the
gaffer-private CI) from deploy-time requirements (no SSH key or LFS needed to
consume cached NARs). See `TODO.md` items on private cache setup.
