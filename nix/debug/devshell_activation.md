# Devshell (direnv) activation slowness

Open investigation. Symptom: after `git pull`, direnv stalls on `copying '/home/agentydragon/code/ducktape/' to the store`, often tens of seconds.

## Problem

A nix-direnv cache miss (most pulls) costs ~37 s cold on wyrm2. Three independent costs, each per miss:

1. **Hash** the whole repo: Nix ≥2.35 reads every tracked byte (~260 MB, ~30k files) to name the flake source. 1.3–1.75 s with a warm page cache, 26 s cold.
2. **Copy** the whole repo: nix-direnv's `nix flake archive` (GC-roots flake inputs, `self` included) forces the store copy 2.35 avoided. 17.6 s on a new tree.
3. **Eval + realise** the devshell: 7–9 s (eval CPU 4.2 s).

Miss frequency is the multiplier: `sync-pins.yml` rewrites `nix/artifact-pins.json` ~17×/day.

Why the whole repo: a flake's source is its enclosing git repo for every flakeref except `path:` — `.`, `./nix/flake` and `git+file:…?dir=nix/flake` all take all tracked files. Same content → same `<hash>-source` store path, but Nix must read everything to know the hash. Its fetcher cache short-circuits only clean, already-seen commits (new worktree at a seen commit: 0.95 s); dirty trees get no per-file cache.

## Mechanics

- `.envrc`: `use flake` on repo root. Largest tracked dirs: `props/specimens` 110 MB, `devinfra/js` 23 MB, `devinfra/claude` 16 MB.
- Cache misses: any change to `flake.{nix,lock}` or a `watch_file` (`nix/artifact-pins.json`, `nix/flake/{devtools,packages}.nix`). 122 `sync-pins` commits 2026-11-19..26.
- Devshell really uses pins: `nix/flake/devtools.nix` pulls `ducktapePkgs.{bb,bbr,bbapi,gazelle,ducktape-git-hooks,claude-hook,claude-statusline,skills}`; `skills` bundles every `skill-*` pin.
- Dirty tree (usual) → Nix eval cache off → full eval every miss.
- nix-direnv's cache lives in each worktree's `.direnv/` → the first activation in any new worktree is always a miss.

## Profile (2026-11-26, wyrm2)

Single runs, scratch worktree, Nix 2.35.6, nix-direnv 3.1.2 (home-manager).

| Step                                                          | Time                                                     |
| ------------------------------------------------------------- | -------------------------------------------------------- |
| Full `direnv export`, first activation, new tree              | 37.5 s                                                   |
| Same, nix-direnv cache hit                                    | 0.38 s                                                   |
| Same, watched file touched, tree unchanged                    | 14.5 s                                                   |
| `.envrc` secrets (`devinfra/secrets/cli_env.sh`, 2× `sops`)   | 0.13 s                                                   |
| Source hash, new tree (`nix flake metadata`), page cache warm | 1.75 s                                                   |
| Same, page cache cold (`/tmp` disk under load)                | 26 s                                                     |
| Same, new worktree at an already-seen clean commit            | 0.95 s                                                   |
| `nix print-dev-env --profile`                                 | 7–9 s (eval CPU 4.2 s, 3.7M thunks, 400 MiB heap)        |
| nix-direnv `nix flake archive --json`, new tree               | 17.6 s                                                   |
| Same, `self` already in store                                 | 3.3 s                                                    |
| `nix flake archive --dry-run --json`, new tree                | 2.75 s (`self` not copied; 33/34 paths already in store) |

Nix 2.34.9 vs 2.35.6, new tree, source step only: 19.0 s vs 2.3 s. 2.34 streamed the whole NAR to the daemon before learning the path existed; 2.35 hashes client-side ([NixOS/nix#15711](https://github.com/NixOS/nix/pull/15711)); hosts moved to 2.35 in #20519.

Eval flamegraph (`--option eval-profiler flamegraph`): ordinary nixpkgs stdenv/lib work; largest repo-local site `nix/packages/system-libs.nix` (`makeLibraryPath` over mesa/cups/pango/gobject-introspection/… Chromium + GUI libs) ≈17%. Evaluates `nixpkgs` and `nixpkgs-unstable`; `nixpkgs-master` not touched.

## Options

Columns: which of the three costs (hash / copy / eval) or the miss frequency it removes. Estimates are sums of measured steps, not measured runs.

| Option                                                                              | Removes                                   | Est. cold miss                                 | Cost / risk                                                                    |
| ----------------------------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------ |
| A. Live with it                                                                     | —                                         | ~37 s (warm cache ~27 s)                       | Stall on most pulls and every new worktree                                     |
| B. nix-direnv [#837](https://github.com/nix-community/nix-direnv/pull/837) upstream | copy                                      | ~13 s (hash 1.75 + eval 8 + inputs archive ~3) | Waits on upstream; or carry it as a home-manager patch                         |
| C. `path:./nix/flake` sub-flake                                                     | hash + copy of the repo (only the subdir) | ~11 s (eval 8 + inputs archive ~3)             | Move devshell deps inside; second `flake.lock`; pins move                      |
| D. Plain Nix file, `use nix nix/devshell.nix`                                       | hash + copy + archive                     | ~8 s (eval only)                               | Hand-written lock reader; explicit `watch_file`s; no flake interface in direnv |
| E. Fewer misses                                                                     | frequency                                 | unchanged per miss                             | Partial; pins still churn                                                      |
| F. `nix_direnv_manual_reload`                                                       | the stall (deferred)                      | 0 until reload                                 | Stale tools until manual reload; breaks the auto-pin expectation               |
| G. Shrink the tracked tree                                                          | part of hash + copy                       | ~37 s × (1 − removed share)                    | Moves data out of the repo                                                     |
| H. Faster eval                                                                      | part of eval                              | −≤1.5 s                                        | Small; orthogonal                                                              |

### A. Live with it

Status quo after #20519. Each pull touching pins costs ~27 s (warm) to ~37 s+ (cold); hashing gets worse as the repo grows. No work, no new moving parts.

### B. nix-direnv without the `self` copy

Upstream HEAD (`02f6fcea5d`, 2026-11-22, v3.2.0) still archives `self` unconditionally. Open fixes, same author:

- [#837](https://github.com/nix-community/nix-direnv/pull/837): `nix flake archive` on a temp copy of `flake.{nix,lock}` → same inputs rooted, project not copied; falls back to the old path on failure. No maintainer reply yet.
- [#833](https://github.com/nix-community/nix-direnv/pull/833): `fetchTree` each lock node. Maintainer: "significantly more complicated … net benefit is marginal".

Our flake's inputs are all remote (`github:`), so #837's temp copy covers them. Hash + eval remain. Variants: wait; carry #837 via home-manager overlay; local `--dry-run` patch (considered, owner: doesn't feel right).

### C. `path:` sub-flake for the devshell

`.envrc`: `use flake path:./nix/flake` (the `path:` prefix is required; bare `./nix/flake` resolves to the git repo). Source = that directory only, including untracked/ignored files there; it cannot read outside it.

- Must move inside: devshell definition (`nix/flake/devtools.nix`), `nix/packages/**`, pins, and `skills/skills_registry.json` (or drop `skills` from the devshell, see E).
- `nix/artifact-pins.json` non-Nix readers to repoint: `.github/workflows/{bazel-ci,pr-visuals-publish}.yml`, `devinfra/ci/{artifacts,sync_pins,prune_releases}.py`, image path triggers (`devinfra/ci/nix_images.json`, `.github/workflows/{nix-images,buildbuddy-remote-runner-image}.yml`), docs.
- Own `flake.lock`. Root flake consumes it as an input (`inputs.nixpkgs.follows`), but direnv uses the sub-flake's own lock → keep the two in sync or `nix develop` and direnv diverge.
- Auto-pin flow survives if `sync-pins` writes the pins at the new path.

### D. Plain Nix file for direnv

`nix/devshell.nix`: a function `{ pkgs, pkgsUnstable, fenix, … }: mkShell { … }`. Root `flake.nix` `devShells.default` calls it (keeps `nix develop`); a thin direnv entry reads `flake.lock`, `builtins.fetchTree`s each locked input (cached by `narHash`), imports the function. `.envrc`: `use nix nix/devshell-direnv.nix` → `nix print-dev-env --file …`, no `flake archive` step.

- Files read in place; a path is copied only when a derivation uses it (today: `nix/packages/prettier/prettier.nix` `src = ./.`, tiny).
- One `flake.lock` stays the single source of truth; pins stay put.
- Impure eval by default; no eval cache (unused today anyway: dirty trees).
- `use nix` watches only the entry file + `shell.nix`/`default.nix` → add `watch_file` for `flake.lock`, pins, `nix/flake/**`, `nix/packages/**`.
- Must not touch `self`. flake-compat is not an option: it copies the source too.

### E. Fewer misses

- Drop `skills` from the devshell; most pin churn is `skill-*`. Cheap, owner OK with it (draft: #20544): only the `devtools` profile reads
  `share/claude-hooks/skills` (`web_setup.sh` profile mode, Haku `env_setup.sh` via `agent-haku`); local hosts and claude-web HM use
  `nix/home/skills.nix`. Also stops the devshell reading `skills/skills_registry.json`.
- Narrow `watch_file`s to what the devshell reads.
- `bbr`/`bbapi`/`claude-hook` bumps still miss. Combine with any of B–D.

### F. Manual reload

`nix_direnv_manual_reload` keeps the old shell until `nix-direnv-reload`. Hides the stall; tools silently lag `devel`, contradicting the auto-pin goal.

### G. Shrink the tracked tree

`props/specimens` is 110 MB of 260 MB. Moving it out (own repo, release artifact, LFS-style fetch) cuts hash and copy ~40% for every flake consumer (devshell, hosts, CI). Separate decision; affects Props workflows.

### H. Faster eval

`system-libs.nix` (Chromium/GUI libs) ≈17% of eval; two nixpkgs instances. Worth ≤1.5 s; only matters once hash + copy are gone.

### Rejected

- Determinate Nix lazy trees: devenv attempt (`cebd7903fd`) shows a second Nix is unwelcome; upstream 2.35 covers the hashing side. Whether its lazy trees skip hashing entirely is unverified.
- Flake in a separate repo: breaks "my checkout's pins drive my devshell".
- `git+file:…?dir=`: still the whole repo.

## Constraints

- Keep "lands on `devel` → `sync-pins` → devshell/host picks it up on reload/switch".
- Pins left flake inputs (#982, 2026-03-24): per-tag release URLs need `flake.nix` URL edits; no tool rewrites flake input URLs. npins lacked a plain-file pin type (`48b7991455`).
- `gaffer-private` consumes Ducktape as a flake input (`git+ssh://git.allegedly.works/agentydragon/ducktape`, for `bb`/`bbr`), CI `nix profile add github:agentydragon/ducktape/<ref>#citools`, and Bazel `DUCKTAPE_COMMIT`; unaffected by devshell layout except C if `packages` move.

## Reproduce

Scratch worktree; append a line to `README.md` for a new tree; then:

```bash
nix flake metadata --json >/dev/null
NIX_SHOW_STATS=1 NIX_SHOW_STATS_PATH=stats.json \
  nix print-dev-env --option eval-profiler flamegraph --option eval-profile-file eval.folded >/dev/null
nix flake archive --json --no-write-lock-file >/dev/null
direnv allow . && time direnv export bash >/dev/null
```
