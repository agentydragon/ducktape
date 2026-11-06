# Linting architecture

## Configuration owners

- [.bazelrc](../../.bazelrc) enables build-time lint/typecheck aspects and their output groups;
  [linters.bzl](../lint/linters.bzl) owns the Ruff, mypy and ESLint integrations.
- [.pre-commit-config.yaml](../../.pre-commit-config.yaml) owns commit-time checks,
  formatters and file filters. Its tools come from the shared Nix
  [preCommitPackages](../../nix/flake/devtools.nix), also exposed as `.#precommit`.
- [ruff.toml](../../ruff.toml), [mypy.ini](../../mypy.ini),
  [eslint.config.js](../../eslint.config.js) and [.prettierrc.cjs](../../.prettierrc.cjs)
  own each tool's rules. Do not add package-level Ruff or mypy configuration.

ESLint and mypy run through Bazel, not pre-commit. Buildifier's Nix and Bazel
pins are separate; changing one does not automatically update the other.

## Bazel Aspect Configs

Lint runs by default on builds. The enabled aspects and failure policy are in
[.bazelrc](../../.bazelrc); use the repository's `bbr build` workflow.

`--config=nolint` drops the check output groups and disables **all** validation
actions, including validations unrelated to lint. Use it only where that is
intended, such as the [lockfile workflows](lockfiles.md).

## Exclusions

[.gitattributes](../../.gitattributes) holds `rules-lint-ignored=true` flags;
[.pre-commit-config.yaml](../../.pre-commit-config.yaml) holds hook-specific file
filters. Ruff, mypy and ESLint also have exclusions in their own configuration
files above. Check each relevant owner when changing coverage; a copied list of
common directory patterns is not the source of truth.

## CI boundaries

[pre-commit.yml](../../.github/workflows/pre-commit.yml) checks changed files on
PRs and all files on pushes/manual runs.
[bazel-ci.yml](../../.github/workflows/bazel-ci.yml) selects test/build targets;
builds use the default lint aspects. Full ansible-lint runs separately in
[ansible-lint.yml](../../.github/workflows/ansible-lint.yml), which installs its
Galaxy roles and collections; the fast syntax check remains a pre-commit hook.

Terraform validation/linting also runs through `rules_tf`'s `tf_module` test
targets. Cluster structural validation is the `//cluster/validation:test_*`
Bazel suite, not a pre-commit hook. Cluster/Terraform hook scopes are defined in
[.pre-commit-config.yaml](../../.pre-commit-config.yaml).

## Go race instrumentation and publication

`.bazelrc` enables the rules_go race setting at the `build` command level, so
`bazel test` and the following `bazel build` retain the same global setting and
can reuse analysis. Go tests and standalone `go_library` targets inherit race
instrumentation. Each non-pure `go_binary` root that produces a normal
executable sets `race = "off"`; its linked Go dependencies use that mode too.
`pure = "on"` binaries and tests retain their no-cgo mode. Apply the same
`race = "off"` policy to new non-pure Go binaries, including test helpers and
generated Gazelle binaries. Publication reuses output digests only from the
build invocation; test verdicts come separately from the test invocation.
