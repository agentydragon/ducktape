@README.md

## Target Platform

Unless stated otherwise, we target Linux. When writing code, scripts, or configuration, default to Linux assumptions (paths, syscalls, packaging, etc.).

Some components are macOS-specific and only relevant on macOS:

- **Seatbelt** (`sandbox-exec`) — macOS kernel sandbox
- **Sandboxer** — macOS-specific sandboxing tooling

If a component is macOS-only, document it explicitly. Do not silently assume macOS conventions.

@STYLE.md

## Sandbox

Run `bazel`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl`, and other
network/system commands **outside the sandbox** (`dangerouslyDisableSandbox: true`). These
tools require network access for RBE, BES, provider downloads, cluster connectivity, and
local service inspection. The sandbox blocks their network calls (including localhost
connections like `kubectl` → haproxy on `localhost:7445`).

<!-- TODO: Write a Claude Code hook that reminds the agent to use dangerouslyDisableSandbox
     when it runs kubectl/systemctl/etc. inside the sandbox. -->

## Refactoring

When renaming, moving, or deleting files/directories/symbols, search for **all references** across the entire codebase before committing: imports, BUILD files, CI configs, documentation, Dockerfiles, Kubernetes manifests, `AGENTS.md`/`README.md` files. Use Grep broadly. Missing a reference is worse than being thorough.

## Before Hand-off

```bash
bazel build //...
bazel test //...
```

This runs ruff + mypy lint checks and all tests (lint runs by default).
Use `--config=nolint` to skip lint for faster iterative builds.

If you touched `ansible/`, also follow the checklist in `ansible/AGENTS.md`.

## Git

**NEVER amend a commit that has already been pushed.** Once a commit exists on the remote,
create a new commit instead. Amending a pushed commit rewrites history and requires a force
push, which is disruptive and can lose work.

## Debug Notes

Subprojects with complex debugging investigations use a `debug/` directory for
persistent investigation notes (root cause analyses, hypothesis tracking,
network/routing debug logs). These are markdown files committed to git, not
ephemeral — they capture hard-won knowledge about subtle bugs.

Convention: `<subproject>/debug/<topic>.md`. Examples:

- `cluster/kubespand/debug/kubespan-nixos-routing.md` — rp_filter routing analysis
- `cluster/kubespand/debug/qemu-test-architecture.md` — QEMU test structure reference

## Development Practices

### Testing

- Test files: `test_*.py` in same directory as code
- Framework: pytest with pytest-asyncio
- Fixtures for shared setup
- **No `t.Skip` for missing tools**: Tests must not skip when a required tool is missing.
  Use the tool directly and let the test fail if it's unavailable. Required tools are
  provided via Bazel runfiles, the RBE worker image (`devinfra/rbe_image/Dockerfile`),
  or other mechanisms. If a test needs a new tool, add it to the appropriate provider.

**IMPORTANT: Running tests and Python code**

Always use Bazel to run tests and Python code, not direct pytest or Python invocations:

```bash
# Run tests (CORRECT)
bazel test //path/to:test_target
bazel test //...  # Run all tests

# Run Python code (CORRECT)
bazel run //path/to:binary_target

# Do NOT use these (INCORRECT - they may not have correct paths/deps):
# pytest path/to/test_*.py
# python -m path.to.module
# direnv exec . python -m ...
```

Bazel properly sets up PYTHONPATH, dependencies, and the test environment. Direct pytest/python invocations may fail to find modules or have incorrect configurations.

#### pytest and Bazel

**CRITICAL**: All `py_test` targets MUST have a `pytest_bazel.main()` entry point:

```python
import pytest_bazel

# ... test code ...

if __name__ == "__main__":
    pytest_bazel.main()
```

Without this, Bazel runs the test file directly as a script, which imports and exits 0 (success) without actually running any tests. This caused 99% of tests to silently pass without executing.

Also add `@pypi//pytest_bazel` to the test's deps in BUILD.bazel.

#### pytest-asyncio auto mode

pytest-asyncio is configured in **auto mode** via `pytest_configure(config)` hooks in package-level `conftest.py` files (e.g., `agent_core/conftest.py`, `mcp_infra/conftest.py`, `props/conftest.py`). This automatically detects and runs `async def test_*()` functions without requiring explicit `@pytest.mark.asyncio` decorators.

**Do NOT add** `@pytest.mark.asyncio` decorators to new async tests - they are unnecessary and redundant with auto mode.

**Note**: There's also a root `//:conftest` py_library target available, but most tests use their package-level conftest.py which already configures auto mode. No special Bazel dependency is needed - package conftest.py files are automatically discovered by pytest when included in test `srcs`.

For async fixtures, use:

```python
@pytest.fixture
async def my_fixture():
    # async setup
    yield value
    # async teardown
```

#### Live OpenAI API tests

Tests that call the real OpenAI API use the `@pytest.mark.live_openai_api` marker and Bazel macros from `//openai_utils/testing:testing.bzl`.

**Two-tier pattern: mock + live in one file.** A single test file can contain both mock tests (verifying our code behaves correctly given expected OpenAI responses) and live tests (verifying OpenAI actually responds as we expect). Use `live_openai_py_test` in BUILD.bazel — it generates `.mock` and `.live` Bazel targets from one declaration:

```python
# test_foo.py
async def test_our_logic_with_mock(mock_client):
    ...

@pytest.mark.live_openai_api
async def test_our_logic_against_real_api(live_openai):
    ...
```

```python
# BUILD.bazel
load("//openai_utils/testing:testing.bzl", "live_openai_py_test")

live_openai_py_test(
    name = "test_foo",
    srcs = ["test_foo.py"],
    deps = [...],
)
# Generates: test_foo.mock (runs non-live tests) and test_foo.live (runs live tests with API key)
```

**Gating:** `.live` targets get `OPENAI_API_KEY` via `env_inherit` and the `live_openai_api` tag. CI excludes them with `--test_tag_filters=-live_openai_api`. The root `conftest.py` also skips live-marked tests at runtime when the key is absent.

### Shared Utility Libraries

**`util`** (`//util`): Shared utility libraries.

- `util.workspace.get_build_workspace_directory()` — repo root (`BUILD_WORKSPACE_DIRECTORY` under `bazel run`, cwd otherwise). Use this in `py_binary` targets that need to find source files on the real filesystem.
- `util.workspace.get_build_working_directory()` — cwd where `bazel run` was invoked.
- `util.runfiles` — runfiles resolution utilities (`//util:runfiles`).
- `util.bazel_subprocess` — subprocess execution helpers for Bazel (`//util:bazel_subprocess`).
- `util.bazel.workspace` — `BazelLabel`, `BazelWorkspace`, and workspace env helpers (`//util/bazel:workspace`).
- `util.env.get_required_env(name)` — get env var or raise `KeyError` (`//util:env`).
- `util.env.get_required_env_path(name)` — get env var as `Path`.
- `util.env.get_required_existing_path(name)` — get env var as `Path`, verify it exists.
- `util.env.get_optional_env(name, default=None)` — get optional env var.
- `util.env.get_optional_env_path(name)` — get optional env var as `Path`.
- `util.net` — TCP port utilities (`//util:net`).
- `util.docker` — async Docker network utilities (`//util:docker`).
- `util.decorators` — CLI decorators like `async_run` (`//util:decorators`).
- `util.logging` — structured logging configuration for CLI apps (`//util:logging`).
- `util.fmt` — formatting utilities for lists and truncation (`//util:fmt`).
- `util.oci` — OCI image loading and pushing utilities (`//util:oci`, testonly).

### JavaScript / TypeScript (Bazel with rules_js)

Frontend sub-projects use `@aspect_rules_js` with `js_library` targets. ESLint linting is handled by the workspace lint aspect (runs by default). Declare precise deps — each `js_library` lists only the files it directly imports; transitive deps propagate automatically via `JsInfo`.

**Adding JS dependencies:**

1. Add the dependency to the relevant `package.json` (workspace member under `pnpm-workspace.yaml`)
2. Run any Bazel build touching JS — the first build will fail with "pnpm-lock.yaml file updated. Please run your build again." This is expected: Bazel's `update_pnpm_lock = True` auto-regenerates the lockfile using the pinned pnpm (v9)
3. Run the build again — it succeeds with the updated lockfile
4. Commit the updated `pnpm-lock.yaml`

**Do NOT run raw `pnpm install`** — Bazel manages the pnpm version (pinned in `MODULE.bazel`) and lockfile format. Using a system pnpm may produce an incompatible lockfile.

See <props/frontend/AGENTS.md> for frontend-specific conventions.
