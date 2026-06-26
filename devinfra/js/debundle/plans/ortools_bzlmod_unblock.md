# OR-Tools CP-SAT Bzlmod Unblock Plan

Status: implementation spike only. The root `or-tools@9.15` Bzlmod path
currently fails during analysis before compiling the C++ CP-SAT smoke test.

## Evidence

- Ducktape owns the root Python hub in `MODULE.bazel` via `pip.parse` with
  `hub_name = "pypi"`, Python 3.13, and
  `requirements_lock = "//:requirements_bazel.txt"`.
- Ducktape validation normally runs under BuildBuddy RBE through `bbr`.
- OR-Tools C++ has the needed API surface: `CpModelBuilder::NewIntVar`,
  `AddAllDifferent`, and `AddAllowedAssignments`.
- OR-Tools BCR/source `MODULE.bazel` for `9.15` declares C++ deps and wrapper
  deps: `pybind11_abseil`, `pybind11_bazel`, `pybind11_protobuf`, and `swig`.
- `pybind11_abseil@202402.0` declares `pip.parse(hub_name = "pypi", ...)` and
  `use_repo(pip, "pypi")`.
- Bazel module overrides must be expressed by the root module; overrides in a
  dependency's `MODULE.bazel` are ignored.
- RBE failures observed:
  - `//devinfra/js/debundle/solver_backends/ortools_spike/...`:
    `Duplicate cross-module pip hub named 'pypi'`.
  - Re-running with `--config=nolint` failed the same way, so this is module
    extension resolution, not a target lint action.

## Option A: Root Override For `pybind11_abseil`

Patch only the transitive module that collides with Ducktape's root hub.

Expected changes:

- Keep root direct deps:
  - `bazel_dep(name = "rules_cc", version = "0.2.16")`
  - `bazel_dep(name = "or-tools", version = "9.15")`
- Add a root `git_override` or `archive_override` for `pybind11_abseil`.
- Add `third_party/patches/pybind11_abseil_rename_pypi_hub.patch` that changes
  the module-local pip hub from `pypi` to `pybind11_abseil_pypi`, while
  preserving apparent `@pypi` labels inside that module:

```starlark
pip.parse(
    hub_name = "pybind11_abseil_pypi",
    ...
)
use_repo(pip, pypi = "pybind11_abseil_pypi")
```

Risk:

- Low-to-medium. This directly fixes the observed failure and matches the
  repo's existing practice of root-module overrides plus small patches.
- It may expose the next OR-Tools integration issue: protobuf/rules version
  skew, heavy SCIP/HiGHS downloads, or C++ toolchain warnings.
- The root override must track whichever `pybind11_abseil` source OR-Tools
  actually resolves to.

First validation:

```bash
nix develop -c bbr test //devinfra/js/debundle/solver_backends/ortools_spike/... --cache_test_results=no
```

## Option B: Root Override For A C++-Only OR-Tools Module

Patch OR-Tools' own module metadata so the root graph never includes Python
wrapper dependencies for a C++-only sidecar.

Expected changes:

- Keep the spike target under
  `devinfra/js/debundle/solver_backends/ortools_spike/`.
- Add a root `archive_override` or `git_override` for `or-tools`.
- Add `third_party/patches/or_tools_cxx_only_module.patch` that removes wrapper
  deps and unused module extensions from OR-Tools' `MODULE.bazel`, at minimum:
  `pybind11_abseil`, `pybind11_bazel`, `pybind11_protobuf`, `swig`, and Python
  pip extension setup.

Risk:

- Medium-to-high. It avoids the known `pybind11_abseil` collision more cleanly,
  but carries a larger upstream-module patch.
- OR-Tools BUILD packages may still load non-C++ rule files at package-load
  time, so this may still require Java/Go/Python rule deps even if no wrapper
  targets are built.
- More maintenance burden when OR-Tools updates.

First validation:

```bash
nix develop -c bbr test //devinfra/js/debundle/solver_backends/ortools_spike/... --cache_test_results=no
```

## Option C: Isolated Or Prebuilt Sidecar Package

Avoid putting OR-Tools' Bzlmod graph into Ducktape's root module.

Expected changes:

- Either create a nested sidecar module with its own `MODULE.bazel` under
  `devinfra/js/debundle/solver_backends/ortools_sidecar/`, or register an
  official OR-Tools C++ release tarball as a sidecar-only external repo.
- For the prebuilt path, the official v9.15 release includes an Ubuntu 24.04
  C++ tarball close to the repo's BuildBuddy worker baseline.
- Add a small protobuf/stdin-stdout contract between Rust and the sidecar
  process. Do not link OR-Tools into the production Rust binary.

Risk:

- Medium. This isolates dependency churn best, but it weakens monorepo-native
  validation and packaging unless a dedicated sidecar CI target is added.
- The nested-module `bbr` workflow must be proven; `bbr` normally mirrors the
  git repo and runs remote Bazel from the selected workspace.
- The prebuilt binary path is platform-specific and may need `cc_import`,
  runtime library path handling, and license review.

First validation for nested module:

```bash
cd devinfra/js/debundle/solver_backends/ortools_sidecar
nix develop -c bbr test //... --cache_test_results=no
```

First validation for prebuilt sidecar:

```bash
nix develop -c bbr test //devinfra/js/debundle/solver_backends/ortools_binary_spike/... --cache_test_results=no
```

## Recommendation

Try Option A first. It is the smallest dependency unblock that addresses the
observed failure without changing solver code or carrying a broad OR-Tools fork.
If Option A reaches C++ compilation but reveals deeper OR-Tools module churn,
switch to Option C with an isolated sidecar boundary. Reserve Option B for the
case where root-graph OR-Tools integration remains desirable but wrapper deps
keep causing unrelated analysis failures.
