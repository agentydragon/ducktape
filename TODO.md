# Repository TODOs

## Agentplane

- [ ] Extend the Agentplane platform agent instructions once the interfaces are ready: explain
      subagent operation and safe lookup of past conversations without putting task-specific procedures
      in the shared prompt.

## LSP in Claude Code

- [ ] Configure Python LSP (pyright) similarly to rust-analyzer — investigate whether pyright supports repo-local config files that Claude Code's LSP client would pick up, or if the marketplace plugin's default behavior already works with this Bazel repo. See <devinfra/rust/LSP_NOTES.md> for the approach used with rust-analyzer.

## Linting

- [ ] Catch **over-declared** `ts_library`/`js_library` deps. Under-declaration already fails the
      build — `ts_library` compiles each target's srcs as its own program against its deps' `.d.ts`,
      so a missing dep is a TS2307 — but a dep nothing imports is invisible, and it widens the
      action's inputs and its cache key. Nothing in the repo checks for it today; gazelle here is
      Python-only. The candidate is `aspect-build/aspect-gazelle`'s `language/js`, which generates
      JS/TS deps from the import graph and so would remove the class rather than lint it. Deliberately
      not hand-rolled — a bespoke import scanner would be a third parser of the same TypeScript.

## AWS

- [ ] Consider keeping a persistent AWS admin credential in user-level SOPS (readable by
      agentydragon's machine keys, like `ducktape.sopsEnv`). Hand-provisioned IAM changes, such as
      the Route 53 policies in <cluster/docs/dns_automation.md>, currently need an interactive
      `aws login`; the cluster's own Route 53 keys are deliberately not user-decryptable.

## System Configuration

- [ ] Add to small laptop installation: nmap, other hacking tools
- [ ] Start Signal minimized (difficult: settings in encrypted sqlite)
- [ ] Combine ActivityWatch + HALinuxCompanion to report: session events (login/logout, lock/unlock, suspend/resume), battery charge level, and other device telemetry

## Agent scratch in `/tmp`

- [ ] Make agents write scratch under a per-agent (ideally per-session) dir instead of loose in
      `/tmp`. On wyrm2 (2026-10-29), about 3,900 top-level per-task dirs (`claude-*`, `tana-*`,
      `openclaw-*`, `tmp.*`, …) held 318 G, next to 6,200 loose files. Nothing scopes or ages
      them apart from the 14d `/tmp` rule. Candidate levers:
  - Codex (<nix/home/codex/default.nix>): `exclude_slash_tmp = true`, plus
    `shell_environment_policy` setting `TMPDIR=/tmp/codex-<uid>`. Hard-coded `/tmp/x` paths then
    fail inside Codex, and `/tmp/ducktape-worktrees` needs to be a writable root.
  - Claude Code: export `TMPDIR=<session scratchpad>` from the session-start hook
    (<devinfra/claude/claude_hook>) via `$CLAUDE_ENV_FILE`. Today all sessions share
    `/tmp/claude-<uid>`.
  - <AGENTS.md> § Environment: one rule to put scratch in `$TMPDIR`, never loose in `/tmp`. First
    pick one home for large outputs and worktrees (`/tmp/...` vs `/var/cache/agentydragon/...`).
  - Once the roots exist, give them their own short tmpfiles age (e.g. `bcmBM:3d`).

## Build System

- [ ] Extend trusted fork CI to `main` if it becomes an active PR target. Branch protection
      requires checks there, but <.github/workflows/trusted-fork-pr-ci.yml> currently
      replaces fork checks only for `devel`; keep this deferred while `main` is unused.
- [ ] File upstream BB issue + revert `--script` workaround in `bazel-ci.yml` once fixed. See <devinfra/debug/bb_remote_peers_exhausted.md> (post-run `downloadOutputs` fails with "Exhausted all peers"). Also audit `push-images.yml` which also uses `bb remote build … --remote_download_regex=…`.
- [ ] Migrate all Python packages to Bazel monorepo style (colocated tests and flat package structure)
- [ ] Remove `--per_file_copt=external/or-tools[+]/.*@-Wno-sign-compare` from `.bazelrc` once OR-Tools cleans up signed/unsigned comparisons in `external/or-tools+/`. Currently `or-tools 9.15`.
- [ ] Pre-commit lint for <devinfra/ci/image_targets.json> completeness: fail if any in-cluster `oci_image` target lacks an entry, and warn on entries whose `target` label points at a non-existent target. Would have caught `airlock`/`fc-vm-pod`-style coverage gaps during the runner-side push rewrite (PR #1290). Easiest implementation: extend `//devinfra/ci:test_plan_image_pushes` to run `bazel query 'kind("oci_image_rule", //...)'` and diff it against `load_images`.
- [ ] Decide the fate of `secrets/shared/gaffer-private-fetch-pat.yaml` after the private-cache path exists: keep it for another workflow, rotate it, or delete it.
- [ ] Cache `bazel-diff` base-commit hashes across PR runs. Today `bazel-ci.yml` regenerates the merge-base's Merkle hashes on every PR CI run (checks out the base, runs `bazel-diff generate-hashes` — a `bazel query`, so a chunk of RAM + wall-time per run) even though for a given devel commit the hashes are stable. Add a workflow on devel push that generates + uploads the hashes JSON keyed by SHA (GHA `actions/cache` with `key: bazel-diff-hashes-<sha>` is the simplest fit; SeaweedFS via `AWS_ENDPOINT_URL` or BuildBuddy CAS are alternatives), and have PR CI attempt a fetch before falling back to regeneration. PR CI then only pays the HEAD-hash cost.
- [ ] Revisit Bazel repository-content caching after the Bazel upgrade: evaluate BuildBuddy or another backend for the experimental remote repository-content cache; measure wyrm2 local-cache hits after deployment and test the known output-base symlink cases on Bazel 9; then decide whether to explicitly configure it on my other developer machines. Account for wyrm2's 100G repository-cache mount and age-only garbage collection.
- [ ] Package `hetzner-vnc-screenshot` with its skill as an independently released tool.

## Dependency patch retirement

- [ ] Remove `pybind11_abseil_rename_pypi_hub.patch` and its `single_version_override` after a released pybind11_abseil version contains [upstream PR #73](https://github.com/pybind/pybind11_abseil/pull/73); bump the dependency and rerun the OR-Tools consumer build together.
- [ ] Remove the two `rules_tf` provider-mirror exit-code patches after [rules_tf PR #28](https://github.com/yanndegat/rules_tf/pull/28) merges; retain `rules_tf_exclude_dotterraform.patch`, which is a separate source-packaging fix.
- [ ] Retire the rugged Mutter patch after nixpkgs selects Mutter 51.0 or newer and rugged passes fresh-login, reboot, and external-monitor auto-rotate checks without the local patch. GNOME Mutter [MR !4962](https://gitlab.gnome.org/GNOME/mutter/-/merge_requests/4962) merged to `main` on 2026-08-04 and its fix is in Mutter 51.0 (GNOME 51, released 2026-09-16). As of 2026-10-22, the lock selects 50.5 from stable and 50.4 from unstable/master; current nixpkgs branch definitions still select 50.5. Reconcile the overlapping patch hunks at the eventual bump before testing rugged.
- [ ] On the next OpenClaw bump, re-audit the remaining local `2026.9.5` dist patch: retire it only after both images build and the corresponding Nix-mode, plugin, and startup contracts are proven against upstream.

## Skills

- [ ] Decide how `skills/frontmatter_validation.py` should handle `allowed-tools`. `skills/AGENTS.md` requires explicit user approval before adding it, but enforcement needs a policy that distinguishes approved exceptions from accidental permission grants.

## Terraform

- [ ] Unify manual `tofu` runs with Bazel-managed providers. Currently manual `tofu plan/apply` resolves providers independently from the `tf.download(mirror={...})` pins in `MODULE.bazel`. Create a wrapper (script or `bazel run` target) that sets `TF_CLI_CONFIG_FILE` pointing at the Bazel-fetched filesystem mirror (`<output_base>/external/@tf_toolchains/mirror/`), so manual runs use the exact same provider versions as `bazel test`.
- [ ] Restore proper Forgejo branch protection for `agentydragon/ducktape:devel` and `agentydragon/gaffer-private:main` once Forgejo or the `svalabs/forgejo` provider can express "protected branch, but allow `agentydragon` force-push". Forgejo 15.x / Gitea 1.22 API only has a normal push whitelist, so protected branches still reject `git push --force`.

## Renovate Coverage gaps

Already covered (verified on dashboard; the first three by built-in managers):

- `bazel-module`: `bazel_dep()` AND `oci.pull()` blocks in MODULE.bazel (both tags and digests)
- `terraform`: `required_providers` version constraints in `.tf` files
- Container images in k8s manifests, Dockerfiles, etc.
- `tf.download(mirror = {...})` exact pins in `MODULE.bazel` — the authoritative provider versions for hermetic Bazel builds, which the loose `>=` constraints in `.tf` files can drift from. Covered by the `terraform-provider` regex custom manager in `renovate.json5`, which matches each `"name": "owner/repo:x.y.z"` entry.

Not covered — need custom regex managers or restructuring:

- [ ] OpenTofu version in `MODULE.bazel` (`version = "1.12.6"` in `tf.download`) — a sibling key of the covered `mirror` map, so the mirror manager's shape does not match it.
- [ ] `tfdoc_version` and `tflint_version` in `MODULE.bazel`
- [ ] Talos installer/imager versions — the `var.talos_version` and `var.proxmox_talos_version` defaults in `cluster/terraform/main/variables.tf`, and the `factory.talos.dev/metal-installer/<schematic>` image URLs they interpolate into. The `siderolabs/talos` provider pin is covered (by the `terraform` manager's `~> 0.11.0` in `cluster/terraform/main/terraform.tf` and by the mirror manager above); these are not.

### LLM-powered update summaries

- [ ] Add a scheduled GitHub Action that collects open Renovate PRs / dashboard state and produces an LLM-generated summary of breaking changes, notable features, and update recommendations. Options: `actions/ai-inference` (free, single LLM call, action must pre-fetch changelogs) or Copilot coding agent (assign `@copilot` to issue, agent can browse, costs premium requests). Consider storing verbose LLM-facing context on a branch to enable incremental analysis across runs.

## Repository

- [ ] Add an AGPL-3.0 `LICENSE` file at the repo root and standardize AGPL-3.0 license headers across source files (`README.md` declares AGPL 3.0, but there is no `LICENSE` file and headers are inconsistent)
