# CI Unification Plan

**Status**: Phases 1-4 complete. Remaining: Phase 4 k8s validation BUILD.bazel (sh_test wrappers), Phase 5 ansible-lint in Bazel (blocked on galaxy dependency).

## Architecture

CI consolidated around two pillars:

- **Pre-commit**: fast, change-aware, catches all files (including those without Bazel targets). Formatters, syntax checks, trivial linters.
- **Bazel**: hermetic builds/tests with RBE caching. All language linting (ruff, mypy, eslint, clippy), test execution, artifact builds.

### What stays in pre-commit

Conflict markers, YAML/TOML syntax, `terraform_fmt`, ansible playbook syntax, prettier, ruff format, buildifier, shfmt. These are fast, universal, and need to catch files without Bazel targets.

### What moved to Bazel

Python linting (ruff, mypy), JS/TS (eslint, svelte-check), Rust (clippy, rustfmt), Terraform validation (rules_tf with hermetic provider mirror), k8s validation binaries (kubeconform, flux, kustomize via http_archive).

## External tool installation

Same pre-commit hooks work in three environments:

| Environment    | How tools are installed                                                   |
| -------------- | ------------------------------------------------------------------------- |
| Local dev      | Nix/direnv (`.envrc`)                                                     |
| GitHub Actions | Official setup-\* actions (`setup-opentofu`, `setup-tflint`, flux action) |
| Claude Code    | Session start hook binary downloads                                       |

## Terraform in Bazel (Phase 3 — done)

rules_tf (v0.0.10) with provider mirror: `mirror` dict in `tf.download()` pre-fetches providers at Bazel fetch time. Both tflint and `tofu validate` run hermetically (no network at test time). Works on RBE.

**Adding a new terraform module**: add providers to `mirror` dict in `MODULE.bazel` (versions from `.terraform.lock.hcl`), create `BUILD.bazel` with `tf_providers_versions` + `tf_module`.

## K8s validation in Bazel (Phase 4 — binaries done, BUILD.bazel remaining)

http_archive binaries added to MODULE.bazel: `@kustomize`, `@kubeconform`, `@flux`, `@gitstatusd`. Still need sh_test wrappers in `cluster/k8s/BUILD.bazel` to actually invoke them.

Existing Python validation scripts (`validate-kustomizations.py`, `validate-flux-build.py`) can be wrapped as py_test.

## Ansible-lint (Phase 5 — not started)

Blocked on galaxy dependency question: ansible-lint needs galaxy collections, which require network. Options: pre-fetch, network tag, or skip.

## Remaining work

- [ ] Create k8s validation BUILD.bazel with sh_test wrappers for kubeconform/flux
- [ ] Evaluate ansible-lint in Bazel (galaxy dependency)
