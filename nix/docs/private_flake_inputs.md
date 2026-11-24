# Private Gaffer artifacts

Ducktape consumes `drivefs` and `drivectl` as pinned Nix store closures. The
[producer workflow](https://github.com/agentydragon/gaffer-private/blob/main/.github/workflows/nix-attic-push.yml)
pushes the closures to the restricted `gaffer` Attic cache before updating
[`nix/gaffer-pins.json`](../gaffer-pins.json) on Ducktape's `devel` branch.
Each pin records the store path, version, and source revision.

## Deploy and diagnose

- Opt in with `services.google-drive.enable = true` in the host's
  [Home Manager configuration](../home/modules/google-drive.nix). Apply it using
  the [normal host rebuild procedure](../README.md#usage).
- [The Nix package adapter](../packages/gaffer.nix) uses `builtins.fetchClosure`
  against the signed `gaffer` cache. Ducktape does not fetch Gaffer source. If
  evaluating or building the enabled config fails, check the pin, then check
  cache reachability, the trusted signing key and the host's `gaffer:r` scope.
  See [Attic reader configuration](../../cluster/docs/nix_cache.md#pulling-machines-and-users)
  for the separate daemon and per-user Nix credential paths.
- A missing pin means that package is unavailable. The service requires
  `drivefs`; `drivectl` is optional. Restore the closure and pin through the
  producer workflow. See [cache isolation](../../cluster/docs/nix_cache.md#private-binary-isolation-drivefsdrivectl)
  before changing publish targets.

The retired `git+ssh://…?lfs=1` flake input required consumer SSH, Git LFS,
and a private repository deploy key in CI. The producer now owns source
checkout. The unused fetch PAT is still tracked in the
[repository TODO](../../TODO.md#build-system).
