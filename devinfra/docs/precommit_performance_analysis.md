# Pre-commit Performance Analysis (Historical)

These measurements describe a former pre-commit implementation. They are not current
benchmarks or a description of today's hook runner.

## Recorded measurements

| Scope                         | Historical result                                                                                                                                                            |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Full hook run                 | About 160s before the runner change and 78s after parallel batching through a script-path runner.                                                                            |
| Former `bazel-precommit` hook | About 42s: 22s building and 14s across 16 parallel batches.                                                                                                                  |
| Other hooks                   | `checkov` 13s; `terraform_validate` 11s; `terraform_tflint` 4s; `markdownlint-cli2` 3s; other YAML/AST/TOML checks and Ruff 5s. The three Terraform hooks totaled about 28s. |
| Work within each batch        | Prettier about 6s over 100+ files (about 60ms/file); Ruff about 1s over 60+ files; buildifier about 0.2s; Kustomize, Flux and GitOps validators about 4–5s each.             |
| File listing                  | `pygit2.status()` took about 12s; reading `pygit2.index` took about 0.02s.                                                                                                   |
| Previous split Bazel hooks    | Separate format and validate hooks each took about 55s when serialized on the Bazel client lock.                                                                             |

## Lessons from the former runner

The former unified Bazel binary ran Prettier, Ruff, shfmt and buildifier alongside
Terraform-centralization and Kustomize/Flux/GitOps/Helm validators. Its `bazel run
--script_path` runner let pre-commit's batches run without holding the Bazel client
lock: the first batch created a runner under `.git/precommit-runners/`, while batches
from the same pre-commit process (identified by PPID) waited on `flock` and reused it.
The old profiling command expected two `Running command line` messages after
batching.

The `pytest-main` check moved to a Bazel aspect on normal builds, including Bazel CI,
so it left pre-commit. The old note also suggested pre-generating the runner in a
warmed CI cache, and parallelizing Terraform hooks while caching initialization;
those were proposals, not implemented steps. These are lessons from that
implementation, not instructions for the current setup.

## Current ownership

[`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) defines the current hooks.
The `ducktape-precommit` entry point is declared by the
[`//:ducktape_git_hooks_wheel` target](../../BUILD.bazel); its file checks live in
[`git_hook.py`](../precommit/git_hook.py).
