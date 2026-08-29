# devinfra/ci

CI-side utilities and configuration.

## PR visual review

The publisher moved with its code to <../pr_visuals/README.md>.

## `artifact_targets.json`

Single source of truth for Bazel targets, artifact outputs, and release
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
    target:   Bazel label that produces `output`
    output:   file path under bb-out/
    release:  release group this pin ships in
```

Every pin corresponds 1:1 to a same-named entry in `nix/artifact-pins.json`.

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
uploads every output. For backward compatibility, a one-asset release retains
the SHA-256 of that asset as its release identity.

`nixPackage: false` means release.yml still publishes it but the PR gate
skips it — typical for binary drops (bbapi, claude-hook-rs, debundle,
skills) whose nix package is a trivial `install`/`autoPatchelfHook`;
bazel-ci already rebuilds them from source so the gate would be redundant.

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
Changing either output changes the release identity and publishes both assets.

**`haku-approvals`**

The GNOME Shell extension, native GTK/WebKit window, desktop entry, symbolic
icon, and canonical Haku logo come out of the Bazel-built
`//haku/console/gnome:haku_approvals_zip` archive. The Nix package consumes
that release artifact and installs its contents; it does not maintain a second
source-based copy of the desktop assets.
