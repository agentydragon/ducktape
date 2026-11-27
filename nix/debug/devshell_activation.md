# Devshell (direnv) activation slowness

Open investigation. Symptom: after `git pull`, direnv stalls on `copying '/home/agentydragon/code/ducktape/' to the store`, often tens of seconds.

## Mechanics

- `.envrc`: `use flake` on repo root → every nix-direnv cache miss evaluates the root flake. Source = all git-tracked files: ~260 MB, ~30k files (`props/specimens` 110 MB, `devinfra/js` 23 MB, `devinfra/claude` 16 MB).
- Cache misses: any change to `flake.{nix,lock}` or a `watch_file` (`nix/artifact-pins.json`, `nix/flake/{devtools,packages}.nix`). `sync-pins.yml` rewrites the pins ~17×/day (122 commits 2026-11-19..26) → most pulls miss.
- Devshell really uses pins: `nix/flake/devtools.nix` pulls `ducktapePkgs.{bb,bbr,bbapi,gazelle,ducktape-git-hooks,claude-hook,claude-statusline,skills}`; `skills` bundles every `skill-*` pin.
- Dirty tree (usual) → Nix eval cache off → full eval every miss.

## Profile (2026-11-26, wyrm2)

Single runs, scratch worktree, Nix 2.35.6, nix-direnv 3.1.2 (home-manager).

| Step                                                                  | Time                                                     |
| --------------------------------------------------------------------- | -------------------------------------------------------- |
| Full `direnv export`, first activation, new tree                      | 37.5 s                                                   |
| Same, nix-direnv cache hit                                            | 0.38 s                                                   |
| Same, watched file touched, tree unchanged                            | 14.5 s                                                   |
| `.envrc` secrets (`devinfra/secrets/cli_env.sh`, 2× `sops`)           | 0.13 s                                                   |
| Source hash, new tree (`nix flake metadata`), page cache warm         | 1.75 s                                                   |
| Same, page cache cold (`/tmp` disk under load)                        | 26 s                                                     |
| `nix print-dev-env --profile`                                         | 7–9 s (eval CPU 4.2 s, 3.7M thunks, 400 MiB heap)        |
| nix-direnv `nix flake archive --json` (GC-roots all inputs), new tree | 17.6 s                                                   |
| Same, `self` already in store                                         | 3.3 s                                                    |
| `nix flake archive --dry-run --json`, new tree                        | 2.75 s (`self` not copied; 33/34 paths already in store) |

Nix 2.34.9 vs 2.35.6, new tree, source step only: 19.0 s vs 2.3 s. 2.35 hashes local flakes in place instead of copying ([NixOS/nix#15711](https://github.com/NixOS/nix/pull/15711)); hosts moved to 2.35 in #20519.

Eval flamegraph (`--option eval-profiler flamegraph`): ordinary nixpkgs stdenv/lib work; largest repo-local site `nix/packages/system-libs.nix` (`makeLibraryPath` over mesa/cups/pango/gobject-introspection/… Chromium + GUI libs) ≈17%. Evaluates two nixpkgs instances (`nixpkgs`, `nixpkgs-unstable`); `nixpkgs-master` not touched.

## Where the time goes

- **nix-direnv `flake archive`** forces the full `self` copy that 2.35 just avoided; unconditional in upstream nix-direnv 3.2.0 (`direnvrc`, after `print-dev-env`), no opt-out.
- **Eval + realise** ~7–9 s per miss.
- **Hashing** reads every tracked byte; cheap only while the page cache holds the tree.

## Options considered

- **Patch nix-direnv to `flake archive --dry-run`**, GC-root only existing paths → est. ~12–13 s cold (sum of steps, not run). Owner: doesn't feel right; not pursued for now.
- **Non-flake `shell.nix` for direnv** (read `flake.lock`, `builtins.fetchTree` inputs, import `nix/flake/devtools.nix` from the working tree). Avoids the source copy/hash; doesn't touch eval cost.
- **`path:` sub-flake for the devshell**: can't read outside its dir; pins + `nix/packages` + `devtools.nix` + `skills/skills_registry.json` would have to move in. `nix/artifact-pins.json` has non-Nix readers (`.github/workflows/{bazel-ci,pr-visuals-publish}.yml`, `devinfra/ci/{artifacts,sync_pins,prune_releases}.py`, image path triggers) → keep in place. `git+file?dir=` still copies the whole repo.
- **Move `skills` out of the devshell** (home-manager concern) → fewer pin-driven misses.
- `nix_direnv_manual_reload`: hides the stall, risks stale tools.

## Constraints

- Keep "lands on `devel` → `sync-pins` → devshell/host picks it up on reload/switch".
- Pins left flake inputs (#982, 2026-03-24): per-tag release URLs need `flake.nix` URL edits; no tool rewrites flake input URLs. npins lacked a plain-file pin type (`48b7991455`).
- devenv rejected 2025-12 (`cebd7903fd`): needs its own Nix with `lazy-trees`.
- `gaffer-private` consumes Ducktape as a flake input (`git+ssh://git.allegedly.works/agentydragon/ducktape`, for `bb`/`bbr`), CI `nix profile add github:agentydragon/ducktape/<ref>#citools`, and Bazel `DUCKTAPE_COMMIT`; unaffected by devshell layout.

## Reproduce

Scratch worktree; append a line to `README.md` for a new tree; then:

```bash
nix flake metadata --json >/dev/null
NIX_SHOW_STATS=1 NIX_SHOW_STATS_PATH=stats.json \
  nix print-dev-env --option eval-profiler flamegraph --option eval-profile-file eval.folded >/dev/null
nix flake archive --json --no-write-lock-file >/dev/null
direnv allow . && time direnv export bash >/dev/null
```
