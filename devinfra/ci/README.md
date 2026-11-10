# devinfra/ci

CI-side utilities and configuration.

## PR visual review

The publisher moved with its code to <../pr_visuals/README.md>.

## `artifact_targets.json`

Single source of truth for Bazel targets, released filenames, and release
grouping for every artifact pinned in `nix/artifact-pins.json` (except `bb`,
which is an external buildbuddy-io binary). Consumed by:

- <../../.github/workflows/release.yml> — publishes each release group. Its
  `matrix` job builds nothing: `plan_releases.py` turns this file into a
  `matrix.include` of only the groups whose content changed, reading what
  bazel-ci already built. How that works, and what was rejected on the way:
  <docs/publish_planning.md>.
- <../../.github/workflows/nix-wheel-check.yml> — the PR-time nix
  imports-check gate. Rebuilds artifacts from PR source and feeds them into
  `nix build .#packages.x86_64-linux.<pin>` via `DUCKTAPE_ARTIFACT_OVERRIDES`
  (see `flake.nix`).

### Schema

Two tables, symmetric on pins:

```text
pins:
  <pin_name>:
    target:    Bazel label to build; external targets use the canonical `@@` spelling BES reports
    filename:  released asset name — the built file basename and download-URL component
    release:   release group this pin ships in
```

Every published pin corresponds 1:1 to a same-named entry in `nix/artifact-pins.json`. A new
release can be listed here before its first publish; PR-time source overrides build and check the
new package, then `sync-pins.yml` adds its pin after the release is reachable from `devel`.

```text
releases:
  <release_name>:
    tests:             (optional) Bazel test targets to run before publishing
    nixPackage:        (optional, default false) has a `.#packages.x86_64-linux.<release_name>`
                       flake output the PR gate builds + imports-checks
    bazelFlags:        (optional) extra flags passed to bazel build
    releaseMetadata:   (optional, default false) upload a <pkg>.release.json alongside
    metadataPlatform:  (optional) `platforms` string in that release.json
```

Every pin assigned to a release has equal standing. `release-artifact` hashes
the complete, filename-addressed asset set to build the release tag suffix and
uploads every asset. For backward compatibility, a one-asset release retains
the SHA-256 of that asset as its release identity.

`release_assets.py` resolves each asset path from the build event stream by target
label and checks its basename against `filename`. The planner uses the same
target/name pair to derive release identities; uncertain results stay in the
matrix for the asset job to resolve after building.

`nixPackage: false` means release.yml still publishes it but the PR gate
skips it — typical for binary drops (bbapi, claude-hook, debundle,
skills) whose nix package is a trivial `install`/`autoPatchelfHook`;
bazel-ci already rebuilds them from source so the gate would be redundant.

### Automatic pin selection

`sync-pins.yml` runs every 30 minutes. It selects published content from a
completed push CI run for the exact checked-out, current `devel` SHA, using the
newest run/attempt for that SHA. Release creation dates never order promotions:
an A → B → A revert selects the existing A release again. Old or feature-branch
CI runs cannot select pins, including after a newer source requested A without
changing any pins.

| Artifact category                                       | Evidence for the desired release tag                                                                                                                    |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Normal workspace targets and skills                     | Deterministic normal-build invocation, exact source, full-sweep patterns, Bazel options and RBE image                                                   |
| Manual targets, external targets, custom configurations | Same run/attempt's successful package release job and successful deterministic unstamped build, with exact source, targets and observable Bazel options |
| Multiple assets on one release                          | Complete target/filename/digest set from one invocation; all pins in the group update together                                                          |
| Stamped assets                                          | Tag identity comes from the unstamped build; downloaded bytes must match GitHub's published asset SHA-256                                               |

A terminal failed workspace build can identify complete outputs already present
in a published release, just as the release planner skips unchanged content.
This is not a per-target passing-build verdict: the export does not expose that
bit. New content still goes through the existing per-release publication gate.
Custom Starlark options are not exposed in Bazel's explicit command line; their
provenance comes from the successful package job and its registered configuration.

Pin selection trusts the repository publisher's immutable content-tag contract.
GitHub's asset digest verifies the downloaded asset bytes, not equality between
stamped bytes and the unstamped tag hash. A matching complete release originally
created on a feature branch is reusable when exact devel evidence independently
selects its content; unrelated feature releases cannot enter the selection.

Pending CI, missing/expired BuildBuddy evidence, missing assets or asset digests,
and failed verification retain the affected pins. An unrelated CI failure does
not block successful package jobs. API failures before the final write leave the
pin file untouched. Once devel is stable, CI completes, and its releases and
evidence are available, a later scheduled sync converges automatically. A
continuously moving devel can postpone convergence; expired evidence requires a
rerun of the current devel CI. A concurrent devel advance rejects the push;
sync never rebases an old decision and recomputes next cycle.

No-op syncs write neither pins nor provenance metadata, avoiding a commit/CI
loop. A pin-only sync commit runs CI normally and settles without another pin
commit when the artifact content is unchanged. Sync reads one normal build and
only necessary package builds (four concurrent reads, 45 seconds per read), plus
one exact-tag lookup per release group. Unchanged pins need no asset downloads.
The workflow retains its 10-minute budget; a timeout leaves the remote pins
unchanged and the next cycle retries.

To inspect a checked-out source without writing pins:

```bash
uv run devinfra/ci/sync_pins.py --source-commit "$(git rev-parse HEAD)" --dry-run
```

This needs `GH_TOKEN`, `BUILDBUDDY_API_KEY`, and the devshell's pinned `bbapi`.
Dry runs may inspect a historical devel source; use that source's artifact
registry, image pin and existing pins when interpreting the plan.

### Notes on specific entries

**`debundle.bazelFlags: "-c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1"`**

Ship an optimized binary — opt-level=3 LLVM optimizations, debug-assertions
off. Cuts `modules propose` wall on the tana corpus from ~5 min to ~45 s
(7x). `-Cdebuginfo=1` (line tables only) preserves addr2line + inlined-frame
resolution for downstream perf profiling at ~5–10% size cost.

**`aiquota` / `aiquota-extension`**

Two pins ship on one release tag: the Python wheel and the GNOME Shell
extension zip come out of one `bazel build //aiquota:aiquota_wheel
//aiquota/gnome:aiquota_zip` invocation, and `nix/packages/gnome-shell-aiquota.nix`
consumes both via `artifacts.aiquota` and `artifacts.aiquota-extension`.
Changing either asset changes the release identity and publishes both assets.

## Bootstrap dependencies

The shared [Bazel action](../../.github/actions/bb-remote/action.yml) invokes
`bb remote` directly. Runner defaults (including its image) come from the repo
`.bbrc`; the action image comes from `devinfra/image_pins.json`.
Keep that bootstrap command independent of the standalone `bbr` wheel when
changing CI prerequisites: an older pinned wheel cannot supply a newly added
entry point. The wheel pin is in
[`nix/artifact-pins.json`](../../nix/artifact-pins.json); interactive
[`bbr`](../buildbuddy/bbr.py) delegates repository defaults to the same native `.bbrc`.
