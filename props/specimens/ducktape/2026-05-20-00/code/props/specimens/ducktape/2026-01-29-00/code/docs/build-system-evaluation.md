# Build System Evaluation: Pants vs Bazel

Notes on what Pants or Bazel could buy for this repo.

## What Pants Does

### Dependency Inference + Hermetic Execution

```python
# BUILD file can be nearly empty
python_sources()
```

Pants parses imports and infers deps automatically. But execution is still hermetic:

- Each target runs with only its inferred deps
- If `agent_pkg.runtime` imports `adgn`, Pants catches it (inferred dep)
- You can **forbid** deps explicitly for layering:

```python
# agent_pkg/runtime/BUILD
python_sources(
    dependencies=["!//adgn/**"],  # Forbid imports from adgn
)
```

Best of both worlds: no boilerplate, but still enforced boundaries.

### Replaces Pre-commit for Python

Current pre-commit pain points Pants solves:

- 12 separate mypy hooks with duplicated `additional_dependencies`
- Manual sync between pyproject.toml and pre-commit config
- Slow (each hook installs its own venv)

With Pants:

```bash
# Pre-commit hook becomes:
pants lint check --changed-since=HEAD
```

- Caches results (only re-checks changed code)
- Uses dependency inference (no manual dep lists)
- Parallel execution

### What It Replaces

| Current Tool          | Pants Equivalent     | Notes                     |
| --------------------- | -------------------- | ------------------------- |
| pre-commit mypy hooks | `pants check ::`     | With caching              |
| pre-commit ruff       | `pants lint ::`      | Same ruff, better caching |
| pytest invocation     | `pants test ::`      | Parallel, cached          |
| `uv build`            | `pants package ::`   | Builds wheels/sdists      |
| Manual dep tracking   | Dependency inference | From imports              |

### What It Doesn't Replace

| Tool                        | Why Pants Doesn't Cover It                                                |
| --------------------------- | ------------------------------------------------------------------------- |
| **devenv/Nix**              | Pants doesn't manage system packages, interpreters, or shell environments |
| **direnv**                  | No auto-activation of environments                                        |
| **process-compose**         | No service orchestration                                                  |
| **pre-commit (non-Python)** | Keep for ansible-lint, eslint, prettier, buildifier                       |
| **uv**                      | Pants has its own lockfile format (pex), but can use requirements.txt     |

Pants is a **build system**, not a dev environment manager. It assumes tools exist; devenv provides them.

## Layering Architecture

```toml
# pants.toml
[python.dependency_rules]
# agent_pkg.runtime must not import from adgn or agent_server
[[python.dependency_rules]]
path = "agent_pkg/runtime/**"
deny = ["adgn/**", "agent_server/**"]

# agent_server can import from agent_pkg but not adgn internals
[[python.dependency_rules]]
path = "agent_server/**"
deny = ["props/**"]  # Example: props is a separate package
```

Explicit, documented, enforced at build time.

## Git Hook Integration

Option A: Pants-native hook

```bash
# .git/hooks/pre-commit
#!/bin/bash
pants lint check --changed-since=HEAD
```

Option B: Via pre-commit framework

```yaml
- repo: local
  hooks:
    - id: pants
      name: pants lint+check
      entry: pants lint check
      language: system
      pass_filenames: true
      types: [python]
```

## Migration Path

1. Add `pants.toml` to repo (coexists with existing setup)
2. Run `pants tailor ::` to generate BUILD files
3. Run `pants check ::` and fix issues
4. Add layering rules
5. Add Pants to CI alongside existing checks
6. Once stable, remove pre-commit mypy hooks
7. Keep pre-commit for non-Python (yaml, ansible, js, nix)

## Pants + devenv Interaction

They're complementary layers:

```
┌─────────────────────────────────────────┐
│ devenv/Nix                              │
│ - Python interpreter                    │
│ - System libraries (libpq, etc.)        │
│ - CLI tools (git, docker, etc.)         │
│ - Shell environment                     │
└─────────────────────────────────────────┘
         ▼ provides tools to
┌─────────────────────────────────────────┐
│ Pants                                   │
│ - Dependency resolution                 │
│ - Hermetic builds                       │
│ - Test execution                        │
│ - Type checking                         │
│ - Packaging                             │
└─────────────────────────────────────────┘
         ▼ produces artifacts for
┌─────────────────────────────────────────┐
│ Deployment (Docker, k8s, etc.)          │
└─────────────────────────────────────────┘
```

Pants can use the Python from devenv:

```toml
# pants.toml
[python]
interpreter_constraints = ["==3.12.*"]
# Uses whatever python3.12 is on PATH (from devenv)
```

---

## Bazel Alternative

If planning to add more Rust (or other non-Python languages), Bazel may be the better long-term choice.

### Language Support Comparison

| Language          | Bazel                  | Pants           |
| ----------------- | ---------------------- | --------------- |
| Python            | ✅ rules_python        | ✅ First-class  |
| Rust              | ✅ rules_rust (mature) | ❌ None         |
| Go                | ✅ rules_go            | ✅              |
| Java/Kotlin/Scala | ✅                     | ✅              |
| C/C++             | ✅ Native              | ❌              |
| Docker            | ✅ rules_oci           | ✅              |
| Protobuf          | ✅                     | ✅              |
| JavaScript/TS     | ✅ rules_js            | ⚠️ Experimental |
| Nix integration   | ✅ rules_nixpkgs       | ❌              |

### Bazel Linting: rules_lint

[rules_lint](https://github.com/aspect-build/rules_lint) from Aspect brings Pants-like linting to Bazel:

```python
# WORKSPACE or MODULE.bazel
bazel_dep(name = "aspect_rules_lint", version = "1.0.0")
```

Supported linters out of the box:

- **Python**: ruff, mypy (via rules_mypy)
- **Rust**: clippy, rustfmt (via rules_rust)
- **JavaScript/TS**: eslint, prettier
- **Shell**: shellcheck, shfmt
- **Protobuf**: buf
- **Java**: PMD, checkstyle
- **Kotlin**: ktlint
- **Go**: golangci-lint
- **Bazel**: buildifier

Usage:

```bash
# Lint everything
bazel build //... --aspects=@aspect_rules_lint//lint:linters.bzl%lint

# Or as a test (fails on lint errors)
bazel test //... --test_tag_filters=lint
```

### Bazel + Rust

rules_rust is one of the most mature Bazel rulesets:

```python
# BUILD
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_library", "rust_test")

rust_library(
    name = "worthy_lib",
    srcs = glob(["src/**/*.rs"]),
    deps = ["@crates//:serde", "@crates//:tokio"],
)

rust_test(
    name = "worthy_test",
    crate = ":worthy_lib",
)
```

Features:

- Clippy integration: `bazel build //... --aspects=@rules_rust//rust:defs.bzl%rustfmt_aspect`
- Cargo.toml → BUILD generation via `cargo-bazel` or `crate_universe`
- Incremental compilation caching
- Cross-compilation support

### Bazel + Python (rules_python + rules_mypy)

```python
# BUILD
load("@rules_python//python:defs.bzl", "py_library", "py_test")
load("@rules_mypy//mypy:mypy.bzl", "mypy_test")

py_library(
    name = "adgn",
    srcs = glob(["src/**/*.py"]),
    deps = [
        requirement("fastapi"),
        requirement("pydantic"),
    ],
)

mypy_test(
    name = "adgn_mypy",
    deps = [":adgn"],
)
```

### Bazel Trade-offs vs Pants

| Aspect               | Bazel                     | Pants               |
| -------------------- | ------------------------- | ------------------- |
| **Config verbosity** | High (explicit deps)      | Low (inferred deps) |
| **Multi-language**   | Excellent                 | Python-focused      |
| **Linting**          | Via rules_lint (newer)    | Built-in            |
| **Learning curve**   | Steeper                   | Gentler             |
| **Remote cache**     | Native, mature            | Native              |
| **Ecosystem**        | Huge (Google, Meta, etc.) | Smaller             |
| **Rust support**     | Excellent                 | None                |

### Bazel Layering Enforcement

Via visibility:

```python
# agent_pkg/runtime/BUILD
py_library(
    name = "runtime",
    srcs = glob(["**/*.py"]),
    visibility = [
        "//agent_pkg/host:__subpackages__",
        "//agent_server:__subpackages__",
    ],
    # NOT visible to //adgn
)
```

Or via [Gazelle](https://github.com/bazelbuild/bazel-gazelle) directives for Go-style import path enforcement.

### Bazel + devenv/Nix

rules_nixpkgs lets Bazel use Nix-provided toolchains:

```python
# WORKSPACE
load("@rules_nixpkgs//nixpkgs:nixpkgs.bzl", "nixpkgs_python_configure")

nixpkgs_python_configure(
    python3_attribute_path = "python312",
    repository = "@nixpkgs",
)
```

This gives hermetic, Nix-managed Python to Bazel builds.

---

## Recommendation

| If you...                      | Choose                                           |
| ------------------------------ | ------------------------------------------------ |
| Only care about Python         | **Pants** - less config, batteries included      |
| Plan to add Rust               | **Bazel** - rules_rust is excellent              |
| Want multi-language uniformity | **Bazel** - one system for everything            |
| Already know Bazel             | **Bazel** - leverage existing knowledge          |
| Want fastest setup             | **Pants** - `pants tailor` generates BUILD files |

For this repo with Python + potential Rust: **Bazel** is probably the better long-term bet, especially given existing Bazel familiarity. The rules_lint addition closes the gap on linting ergonomics.

---

## Open Questions

### Pants-specific

- How well does Pants work with uv workspaces? (May need to choose one)
- Pants has its own lockfile format (pex-based) - migration effort?
- BUILD file generation: how much manual tweaking needed?

### Bazel-specific

- rules_python + uv integration? (or use pip_parse)
- Gazelle for Python BUILD generation?
- Remote cache setup (already have bazel-remote on VPS)

## Links

### Pants

- <https://www.pantsbuild.org/>
- <https://www.pantsbuild.org/docs/python>
- <https://www.pantsbuild.org/docs/python-check-goal> (mypy)
- <https://blog.pantsbuild.org/dependency-inference/>

### Bazel

- <https://github.com/aspect-build/rules_lint>
- <https://github.com/bazelbuild/rules_rust>
- <https://github.com/bazelbuild/rules_python>
- <https://github.com/tweag/rules_nixpkgs>
- <https://github.com/thundergolfer/rules_mypy>
