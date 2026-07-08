# CI Unification Plan

**Status**: Phases 1-4 complete. Remaining: Phase 5 ansible-lint in Bazel (blocked on galaxy dependency).

## Architecture

CI consolidated around two pillars:

- **Pre-commit**: fast, change-aware, catches all files (including those without Bazel targets). Formatters, syntax checks, trivial linters.
- **Bazel**: hermetic builds/tests with RBE caching. All language linting (ruff, mypy, eslint, clippy), test execution, artifact builds.

### What stays in pre-commit

Conflict markers, YAML/TOML syntax, `terraform_fmt`, ansible playbook syntax, prettier, ruff format, buildifier, shfmt. These are fast, universal, and need to catch files without Bazel targets.

### What moved to Bazel

Python linting (ruff, mypy), JS/TS (eslint, svelte-check), Rust (clippy, rustfmt), Terraform validation (rules*tf with hermetic provider mirror), k8s structural validation (kustomize builds, Flux dependency checks, etc. as the `//cluster/validation:test*\*`py_test suite). Current tool wiring: see`devinfra/docs/linting.md`.

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

## K8s validation (Phase 4 — done; superseded by `devinfra/docs/linting.md`)

`kubeconform` is not a Bazel target — it runs only as a pre-commit hook (see
`devinfra/docs/linting.md`). Structural validation (kustomize builds, CRD
layering, orphaned files, Flux dependencies, health checks, blueprint
completeness) is implemented as the `//cluster/validation:test_*` py_test
suite (e.g. `test_flux_build`, `test_cluster_integration`) — not as sh_test
wrappers. The `kustomize`/`flux` CLIs themselves are fetched via
`rules_multitool` (`devinfra/lockfile.json`, exposed as
`@multitool//tools/{kustomize,flux}`) and used internally by
`cluster/validation/kustomize.py` and `cluster/validation/flux.py`.

`devinfra/docs/linting.md` is the current source of truth for this wiring;
this section is historical planning context only.

## Ansible-lint (Phase 5 — not started)

Blocked on galaxy dependency question: ansible-lint needs galaxy collections, which require network. Options: pre-fetch, network tag, or skip.

## Remaining work

- [x] K8s structural validation — implemented as `//cluster/validation:test_*` py_test suite (see `devinfra/docs/linting.md`)
- [ ] Evaluate ansible-lint in Bazel (galaxy dependency)
