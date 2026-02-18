@README.md

This file provides guidance to LLM agents for working with this repository.

## Target Platform

Unless stated otherwise, this repository targets **Linux**. When writing code, scripts, or configuration, default to Linux assumptions (paths, syscalls, packaging, etc.).

Some components are macOS-specific and only relevant on macOS:

- **Seatbelt** (`sandbox-exec`) — macOS kernel sandbox. Do not apply to Linux code.
- **Sandboxer** — macOS-specific sandboxing tooling. Linux equivalents use different mechanisms (seccomp, namespaces, etc.).

If a component is macOS-only, document it explicitly. Do not silently assume macOS conventions.

@STYLE.md

## Before Hand-off

```bash
bazel build --config=check //...
bazel test //...
```

This runs ruff + mypy lint checks and all tests. For Rust code, also run `bazel build --config=rust-check //finance/...`.

If you touched `ansible/`, also follow the checklist in `ansible/AGENTS.md`.

## Development Practices

### Testing

- Test files: `test_*.py` in same directory as code
- Framework: pytest with pytest-asyncio
- Fixtures for shared setup

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

**`bazel_util`** (`//bazel_util`): Bazel runtime utilities.

- `bazel_util.workspace.get_build_workspace_directory()` — repo root (`BUILD_WORKSPACE_DIRECTORY` under `bazel run`, cwd otherwise). Use this in `py_binary` targets that need to find source files on the real filesystem.
- `bazel_util.workspace.get_build_working_directory()` — cwd where `bazel run` was invoked.
- `bazel_util.runfiles` — runfiles resolution utilities.
- `bazel_util.subprocess` — subprocess execution helpers.

**`env_utils`** (`//env_utils`): Environment variable access with validation.

- `env_utils.env_utils.get_required_env(name)` — get env var or raise `KeyError`.
- `env_utils.env_utils.get_required_env_path(name)` — get env var as `Path`.
- `env_utils.env_utils.get_required_existing_path(name)` — get env var as `Path`, verify it exists.
- `env_utils.env_utils.get_optional_env(name, default=None)` — get optional env var.
- `env_utils.env_utils.get_optional_env_path(name)` — get optional env var as `Path`.

### Deployment

```bash
cd ansible
ansible-playbook <hostname>.yaml --ask-become-pass
```
