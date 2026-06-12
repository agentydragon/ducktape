# Debundle Binary Release and Gaffer Auto-Sync

Last trimmed: 2026-06-12.

Status: the Ducktape-side binary release path exists. `debundle` is in the
release matrix, release metadata is emitted, and the Nix artifact package is
defined. This plan now tracks only the remaining Gaffer-side synchronization
work.

Gaffer-local pinning notes live in
`../gaffer-private/tana/re/DUCKTAPE_PINNING.md`.

## Current Ducktape State

Ducktape publishes the Linux amd64 debundler as a normal release artifact:

- `.github/workflows/release.yml` has a `pkg: debundle` matrix row for
  `//devinfra/js/debundle:debundle`.
- `devinfra/ci/artifacts.py` registers the `debundle` release artifact.
- Release metadata is covered by `devinfra/ci/test_release_metadata.py`; Gaffer
  can read `debundle.release.json` to recover the source commit, platform, binary
  name, and hash.
- `nix/packages/default.nix` exposes the pinned artifact as the `debundle`
  package once `nix/artifact-pins.json` has a release pin.

The original compile-cost problem is therefore solved on the producer side:
Gaffer no longer needs a Ducktape change to consume a released binary.

## Remaining Gaffer Work

Gaffer currently has two independent Ducktape pins:

- `@ducktape` source via `archive_override(...)`, used for Starlark rules,
  generated runfiles, and the source-built debundler target.
- `@ducktape_debundle_bin` via `http_file(...)`, the released debundler binary
  selected with Gaffer's `--config=released-debundler`.

The remaining automation should update those pins deliberately, not by fetching
"latest" during Bazel evaluation.

## Sync Workflow Shape

Add a Gaffer workflow or checked-in script that:

1. Finds the newest non-prerelease `agentydragon/ducktape` `debundle-*` release.
2. Downloads `debundle.release.json` and the binary asset.
3. Updates Gaffer's `MODULE.bazel` `archive_override(module_name = "ducktape")`
   to the Ducktape commit that produced the binary.
4. Updates `http_file(name = "ducktape_debundle_bin")` to the matching release
   binary URL and integrity.
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
