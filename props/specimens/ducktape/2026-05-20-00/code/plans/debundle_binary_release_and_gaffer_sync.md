# Debundle Binary Release and Gaffer Auto-Sync

## Context

`gaffer-private` currently consumes Ducktape source through Bzlmod and builds the
Rust debundler from that source:

- `@ducktape//devinfra/js/debundle:pipeline.bzl` provides the Starlark
  `debundle_pipeline` rule.
- `@ducktape//devinfra/js/debundle:debundle` is passed as the executable for
  both Tana web and desktop debundle targets.

That makes Gaffer pay the Rust/SWC compile cost when it only needs to execute a
released tool. We can publish `debundle` as a Ducktape release artifact and have
Gaffer consume the binary directly.

For the first version, the Gaffer updater should also move the full Ducktape
source pin to the same commit that produced the binary. That keeps
`pipeline.bzl`, the CLI contract, and the binary in sync. It is a deliberately
coarse coupling: the update is triggered by a debundle release but advances all
Ducktape surfaces Gaffer consumes. The updater must be honest about that in the
PR body and include a broader Gaffer safety gate.

## Goals

- Publish a Linux amd64 `debundle` binary from Ducktape CI.
- Let Gaffer's Tana debundle builds use the released binary instead of compiling
  `@ducktape//devinfra/js/debundle:debundle`.
- Add Gaffer automation that picks up new Ducktape debundle releases.
- Move the Gaffer Ducktape source pin and debundle binary pin together.
- Run the Gaffer validation gate in one BuildBuddy remote script invocation.

## Non-Goals

- Do not make Bazel fetch "latest" dynamically. All builds stay pinned.
- Do not immediately split `pipeline.bzl` into a separate public rules package.
- Do not broaden this into Tana spec authoring or peel-loop changes.
- Do not try to solve every Ducktape/Gaffer dependency coupling in this pass.

## Phase 1 - Ducktape Release Artifact

Add `debundle` to Ducktape's existing release pipeline and expose it through the
same Nix artifact-pin layer used by the other Ducktape tools.

Planned changes:

- Add a release matrix entry in `.github/workflows/release.yml`:
  - `pkg: debundle`
  - `target: //devinfra/js/debundle:debundle`
  - `bazel_output: bb-out/bazel-out/k8-fastbuild/bin/devinfra/js/debundle/debundle`
  - `tests: //devinfra/js/debundle/...`
- Add `Artifact(pkg="debundle", filename="debundle")` in
  `devinfra/ci/artifacts.py`.
- Add a Ducktape Nix package that installs the `debundle` binary from
  `npins/sources.json` once the release has been published and `sync-pins` has
  added the pin.
- Add the package to Ducktape's devshell conditionally, so `nix develop` keeps
  working before the first `debundle` release pin exists and automatically gains
  `debundle` on `$PATH` after the first sync-pins update.
- Publish a small metadata asset with each release, for example
  `debundle.release.json`:

  ```json
  {
    "package": "debundle",
    "git_commit": "<ducktape commit sha>",
    "platform": "linux-amd64",
    "binary": "debundle",
    "sha256": "<hex sha256 of asset>"
  }
  ```

The metadata is intentionally redundant with GitHub release/tag data. It makes
the Gaffer updater simpler and avoids depending on subtle tag-resolution
behavior.

Before enabling Gaffer consumption, verify the artifact shape:

- `file debundle` reports the expected Linux amd64 executable.
- `./debundle --help` and `./debundle run --help` work on a normal GHA runner.
- `ldd debundle` is understood. A glibc-dynamic binary is acceptable initially
  for RBE/GHA use, but a static binary is a better target if rules_rust can
  produce it cleanly.

## Phase 2 - Gaffer Binary Pin

Add a pinned binary repository in `gaffer-private`.

Expected shape in `MODULE.bazel`:

```starlark
http_file = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")

http_file(
    name = "ducktape_debundle_bin",
    downloaded_file_path = "debundle",
    executable = True,
    integrity = "sha256-...",
    urls = ["https://github.com/agentydragon/ducktape/releases/download/debundle-<hash>/debundle"],
)
```

Then add a local wrapper label, for example `//tana/re/tools:debundle`, and point
both Tana debundle rules at that label:

- `//tana/re/web/78d928dca7:debundle`
- `//tana/re/desktop/spec:debundle_v1_515_0`

The Tana packages should keep loading
`@ducktape//devinfra/js/debundle:pipeline.bzl` for now. Only the executable
changes.

## Phase 3 - Gaffer Auto-Updater

Add `.github/workflows/sync-debundle.yml` in `gaffer-private`.

Triggers:

- `workflow_dispatch`
- scheduled, probably every 30 to 60 minutes

Permissions:

- `contents: write`
- `pull-requests: write`

Authentication:

- Prefer the existing `ducktape-automation` GitHub App pattern used by
  Gaffer's `nix-attic-push.yml`.
- The workflow can sparse-clone Ducktape, decrypt the App PEM with the existing
  `SOPS_AGE_KEY`, mint a token scoped to `agentydragon/gaffer-private`, and push
  an updater branch.
- If branch-protection and workflow-trigger behavior are fine with
  `GITHUB_TOKEN`, this can be simplified later.

Updater logic:

1. Query `agentydragon/ducktape` releases and find the newest non-prerelease
   `debundle-*` release.
2. Download `debundle.release.json`.
3. Read `git_commit`, binary URL, and sha256.
4. If Gaffer already pins that release commit and binary hash, exit cleanly.
5. Update the Ducktape source pin in `MODULE.bazel`:
   - `archive_override(... urls = [".../<git_commit>.tar.gz"])`
   - `strip_prefix = "ducktape-<git_commit>"`
   - `integrity = "sha256-..."` computed from the archive bytes.
6. Update `flake.lock` so Gaffer's Nix input for Ducktape points at the same
   commit.
7. Update Gaffer's `DUCKTAPE_REF` workflow constants where they are meant to
   mirror the Ducktape source pin.
8. Update the debundle binary `http_file` URL and integrity.
9. Refresh `MODULE.bazel.lock` as needed.
10. Run the validation gate.
11. Create or update a PR:
    - branch: `automation/debundle-<release-tag>`
    - title: `chore: bump debundle to <release-tag>`
    - body includes the Ducktape commit, release URL, binary hash, BuildBuddy
      invocation URL, and the explicit note that all Ducktape consumption moved
      to the commit that produced this debundle binary.

Initial implementation can be a checked-in Python script, for example
`devinfra/sync_debundle.py`, called by the workflow. Keeping the mutation logic
in a script makes it easier to run locally and test against fixture files.

## Validation Gate

Run one BuildBuddy remote script after the pin edits. Gaffer's
`.github/actions/bb-remote` already wraps `bb remote --script`, so use that
instead of separate build/test workflow steps.

```sh
set -euo pipefail

git lfs install --local
git lfs pull

bazel build --keep_going --config=rbe --config=ci \
  //tana/re/web/78d928dca7:debundle \
  //tana/re/desktop/spec:debundle_v1_515_0

bazel test --keep_going --config=rbe --config=ci \
  //tana/re/web/live_proxy:load_78d928dca7 \
  //x/augur/...
```

This is intentionally broader than "does debundle run?" because the first
automation version advances the full Ducktape source pin. Tana debundling is
the main gate; Augur is the other important Gaffer consumer of Ducktape source.

## Auto-Merge Policy

Start conservative:

- The updater opens or updates a PR.
- It does not auto-merge for the first few successful cycles.
- Once the workflow has proven stable, enable auto-merge only if:
  - the one-shot validation gate passes;
  - normal branch protection checks are green or intentionally superseded by
    the updater's BuildBuddy invocation;
  - the PR changes only the expected pin files and generated lock files.

## Transitional Coupling

The first version intentionally pins all of Ducktape to the commit that produced
the latest debundle release. That is slightly strange because the trigger and
primary validation are debundle-specific.

Mitigations:

- The PR body must call out the coupling explicitly.
- The gate includes `//x/augur/...`, not only Tana debundle targets.
- The updater must only update to a Ducktape release commit, not an arbitrary
  branch head.

Longer-term decoupling options:

- Keep `@ducktape` source pinned independently for Augur and shared Starlark.
- Keep `@ducktape_debundle_bin` as a separate binary pin.
- Publish a small `rules_debundle` artifact containing only `pipeline.bzl` and
  any required Starlark helpers.
- Or vendor the small Starlark rule into Gaffer if it stabilizes and has no
  meaningful shared implementation pressure.

## Rollout Checklist

- [ ] Ducktape release matrix publishes `debundle`.
- [ ] Ducktape release metadata records source commit and binary hash.
- [ ] Ducktape `npins/sources.json` is automatically updated with the released
      `debundle` binary.
- [ ] Ducktape's devshell has the pinned binary on `$PATH` after the first
      `debundle` artifact pin lands.
- [ ] Gaffer has a local `//tana/re/tools:debundle` executable label backed by
      a pinned release asset.
- [ ] Gaffer web and desktop debundle targets use the local binary label.
- [ ] Gaffer sync workflow updates Ducktape source pin, flake lock, workflow
      refs, binary pin, and Bazel module lock together.
- [ ] Gaffer sync workflow runs the one-shot BuildBuddy validation gate.
- [ ] First automated PR is reviewed manually before enabling auto-merge.
