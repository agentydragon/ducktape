# Debundle Binary Release and Gaffer Auto-Sync

Last trimmed: 2026-07-05.

Status: the Ducktape-side binary release path exists. The `debundle` release
ships the binary and its CP-SAT sidecar, release metadata is emitted, and the Nix
artifact package is defined. This plan now tracks only the remaining Gaffer-side
synchronization work.

Gaffer-local pinning notes live in
`../gaffer-private/tana/re/DUCKTAPE_PINNING.md`.

Ducktape-side work remains only until `sync-pins.yml` commits the `debundle` and
`debundle-ortools-cpsat-solver` pins of the first release that carries the sidecar;
`nix/packages/default.nix` exposes `.#debundle` once both exist. Ducktape cannot
observe whether the Gaffer side has landed. Manual syncs have been done and their
gates passed; which pin Gaffer currently carries is a fact about Gaffer, so it is
tracked there rather than restated here where it silently goes stale. The remaining
value in this file is the workflow shape and validation gate below.

## Current Ducktape State

Ducktape publishes the Linux amd64 debundler as a normal release artifact:

- `devinfra/ci/artifact_targets.json` declares the `debundle` release, which
  `.github/workflows/release.yml` fans out over, with two pins:
  `//devinfra/js/debundle:debundle` and its CP-SAT sidecar
  `//devinfra/js/debundle/solver_backends/ortools_cpsat:selector_cpsat_solver`
  (`debundle-ortools-cpsat-solver`). Both are assets of each `debundle-*` release.
- `debundle.release.json` (<../devinfra/ci/test_release_metadata.py>) gives the
  source commit, platform, binary name, and hash, plus the sidecar's hash under
  `sidecars`.
- The binary finds the sidecar beside itself
  (<../devinfra/js/debundle/docs/cli.md> § Selector sidecar); `run` and
  `spec validate` fail without it.
- `nix/packages/default.nix` exposes `debundle`, binary and sidecar installed
  together, once `nix/artifact-pins.json` pins both.

The original compile-cost problem is therefore solved on the producer side:
Gaffer no longer needs a Ducktape change to consume a released binary.

## Remaining Gaffer Work

Gaffer currently has two independent Ducktape pins:

- `@ducktape` source via `archive_override(...)`, used for Starlark rules,
  generated runfiles, and the source-built debundler target.
- `@ducktape_debundle_bin` via `http_file(...)`, the released debundler binary
  selected with Gaffer's `--config=released-debundler`.

The released sidecar is a third artifact to pin with them: `debundle_pipeline`
takes its solver from the `@ducktape//devinfra/js/debundle:ortools_cpsat_solver`
label flag, which defaults to the sidecar built from the `@ducktape` source pin
(<../devinfra/js/debundle/docs/bazel_integration.md>).

The remaining automation should update those pins deliberately, not by fetching
"latest" during Bazel evaluation.

Manual repins are acceptable while this automation is absent, but they should be
treated as the reference workflow the script is expected to encode: update both
pins together, run the Gaffer gates, and land a reviewed Gaffer PR.

## Sync Workflow Shape

Add a Gaffer workflow or checked-in script that:

1. Finds the newest non-prerelease `agentydragon/ducktape` `debundle-*` release.
2. Downloads `debundle.release.json` and the binary and sidecar assets.
3. Updates Gaffer's `MODULE.bazel` `archive_override(module_name = "ducktape")`
   to the Ducktape commit that produced the binary.
4. Updates `http_file(name = "ducktape_debundle_bin")` and the sidecar's
   `http_file` to the matching release asset URLs and integrity.
5. Updates any Gaffer workflow `DUCKTAPE_REF` constants only when those workflow
   tool pins are intentionally supposed to move with the source pin.
6. Refreshes Bazel locks as needed.
7. Opens or updates a PR whose body calls out that the Ducktape source pin and
   debundle binary pin moved together.

Keep the mutation logic in a script, not only inline YAML, so it can be run and
tested locally against fixtures.

## Validation Gate

The first automated Gaffer PR should run one BuildBuddy remote gate broad enough
to cover both debundling and other important Ducktape consumers:

```sh
git lfs install --local
git lfs pull

bazel build --keep_going --config=rbe --config=ci \
  //tana/re/web/78d928dca7:debundle \
  //tana/re/desktop/spec:debundle_v1_515_0

bazel test --keep_going --config=rbe --config=ci \
  //tana/re/web:load_78d928dca7 \
  //x/augur/...
```

Start with manual review. Enable auto-merge only after several successful
cycles and only if the PR changes the expected pin and lock files.

## Decoupling Options

The first sync can move Ducktape source and the binary together. Longer term,
decouple them only if the coarse pin starts blocking unrelated Gaffer work:

- keep `@ducktape` source pinned independently for Starlark and shared rules;
- keep `@ducktape_debundle_bin` as a separate binary pin;
- publish a small `rules_debundle` artifact containing only `pipeline.bzl` and
  required Starlark helpers;
- vendor the small Starlark rule into Gaffer if it stabilizes and stops sharing
  useful implementation pressure with Ducktape.

Delete this plan once the Gaffer sync workflow is implemented and its operating
contract is documented in Gaffer.
