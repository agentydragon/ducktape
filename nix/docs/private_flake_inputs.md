# Private Gaffer Artifacts

`gaffer-private` is **not** a flake input. Ducktape never fetches its source
code while evaluating or deploying this flake.

Private binaries built by `gaffer-private` CI—currently `drivefs` and
`drivectl`—are consumed as pinned Nix store closures from the private Attic
cache:

```
gaffer-private CI
  -> cache.allegedly.works/gaffer
  -> nix/gaffer-pins.json
  -> nix/packages/gaffer.nix (builtins.fetchClosure)
  -> nix/home/modules/google-drive.nix
```

This keeps source-repository authentication, Git LFS, and CI checkout concerns
on the producer side. Consumer hosts need only an authorized cache reader and
the pinned closure paths.

## Consumer contract

- [`nix/gaffer-pins.json`](../gaffer-pins.json) is the checked-in record of
  `store_path`, source revision, and version for each published artifact.
- [`nix/packages/gaffer.nix`](../packages/gaffer.nix) turns those pins into
  hermetic `builtins.fetchClosure` dependencies from
  `https://cache.allegedly.works/gaffer`.
- [`nix/nixos/modules/attic-substituter.nix`](../nixos/modules/attic-substituter.nix)
  configures the cache URL, its trusted signing key, and a SOPS-rendered
  per-host reader JWT. The token must permit `gaffer:r`.
- [`nix/home/modules/google-drive.nix`](../home/modules/google-drive.nix) is
  the only current consumer. A host opts in with
  `services.google-drive.enable = true`.

If a pin is absent, its package is absent; do not add a source flake input as a
fallback. If the cache is unavailable, Nix cannot realize these private
closures because Ducktape intentionally has neither their source nor a local
build recipe.

## Publishing and updating

1. Build and push the artifact in `gaffer-private` CI to the `gaffer` cache.
2. Update `nix/gaffer-pins.json` with the resulting store path, revision, and
   version through the producer's pin-update workflow.
3. Deploy the Ducktape configuration to a host with an enabled Attic
   substituter and a `gaffer:r` reader token.

Keep the private closures out of the `main` and `public` caches. The complete
cache isolation and CI publishing contract is documented in
[`cluster/docs/nix_cache.md`](../../cluster/docs/nix_cache.md).

## Retired flake-input approach

The previous `git+ssh://...?lfs=1` `gaffer-private` flake input was removed.
Its failure modes—root lacking an SSH agent or `git-lfs`, and CI lacking a
private-repository deploy key—no longer apply to consumers. The old
SOPS-encrypted GitHub fetch PAT is unused; its eventual deletion remains
tracked in the repository TODO list.
