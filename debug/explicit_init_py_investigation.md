# `--incompatible_default_to_explicit_init_py` sys.path breakage

## Status: root cause found

## Problem

Enabling `--incompatible_default_to_explicit_init_py` globally (commit c5e569949) causes
stdlib module shadowing in subprocess invocations. The flag is needed to fix protobuf
namespace packages and FreeCAD conda C extension shadowing (see
`debug/legacy-create-init-investigation.md`).

## Root cause

Two independent mechanisms interact to produce the bug:

### 1. rules_python bootstrap always prepends the test's package directory

The stage2 bootstrap (`stage2_bootstrap_template.py:499-517`) prepends the main file's
directory to sys.path[0]:

```python
if not getattr(sys.flags, "safe_path", False):
    prepend_path_entries = [
        os.path.join(runfiles_root, os.path.dirname(main_rel_path))
    ]
sys.path[0:0] = prepend_path_entries
```

For `//util/bazel:test_subprocess`, this prepends `_main/util/bazel/` to sys.path[0].
This happens **identically** with flag on or off — same `MAIN_PATH`, same config
transition, same bootstrap file.

### 2. pytest's `resolve_package_path` walks up `__init__.py` and resets sys.path[0]

When pytest collects test files, `_pytest/pathlib.py:import_path` resolves the test's
package root by walking up looking for `__init__.py` files (`resolve_package_path`,
line 839). It then **inserts `pkg_root` at sys.path[0]** (line 583):

```python
pkg_root, module_name = resolve_pkg_root_and_module_name(path, ...)
if mode is ImportMode.prepend:
    if str(pkg_root) != sys.path[0]:
        sys.path.insert(0, str(pkg_root))
```

**With `__init__.py` stubs (flag off):**

`resolve_package_path` walks: `util/bazel/` (has `__init__.py`) → `util/` (has
`__init__.py`) → `_main/` (no `__init__.py`, **stop**). Returns `util/`, so
`pkg_root = _main/` (parent of the highest `__init__.py`-bearing directory).

pytest inserts `_main/` at sys.path[0], **displacing** `_main/util/bazel/` to position 1.
Now sys.path[0] = `_main/` (no `subprocess.py` at root level) → no shadowing.

**Without stubs (flag on):**

`resolve_package_path` walks: `util/bazel/` (no `__init__.py`, **stop immediately**).
Returns `None` → falls through to `pkg_root = path.parent = _main/util/bazel/`.

`str(pkg_root) == sys.path[0]` (already `_main/util/bazel/` from the bootstrap) →
**nothing is inserted**. sys.path[0] remains `_main/util/bazel/` which contains
`subprocess.py` → stdlib shadowing in subprocesses via `python_env()` PYTHONPATH
propagation.

### 3. `python_env()` propagates the poisoned sys.path to subprocesses

`util/bazel/subprocess.py:python_env()` copies all of `sys.path` into PYTHONPATH for
child processes. The child Python interpreter processes PYTHONPATH before importing,
so `_main/util/bazel/subprocess.py` is found before stdlib's `subprocess`.

## Test failures

| Target                                  | Root cause                                                                    |
| --------------------------------------- | ----------------------------------------------------------------------------- |
| `//util/bazel:test_subprocess`          | `util/bazel/subprocess.py` shadows stdlib in `run_python_module` subprocesses |
| `//mcp_infra/exec:test_mcp_integration` | `mcp_infra/exec/subprocess.py` same mechanism                                 |
| `//x/claude_linter_v2:test_integration` | `run_python_module` subprocess fails, same class                              |
| `//wt/shared:test_style`                | Different: `wt` becomes namespace package, `__file__` is `None`               |
| `//props/backend/routes:test_runs`      | TIMEOUT, likely related                                                       |

## Key finding: `python_env()` / PYTHONPATH propagation is unnecessary

`python_env()` exists to propagate the parent's sys.path as PYTHONPATH to child
processes, because the venv bootstrap sets up sys.path in-process and subprocesses
don't go through that bootstrap.

**But the venv DOES work for subprocesses.** `sys.executable` points to the venv's
Python (`_test_subprocess.venv/bin/python3`). When a child process uses that
interpreter, the venv activates automatically via `pyvenv.cfg` discovery, which
triggers `site.py` → `bazel.pth` → `_bazel_site_init._setup_sys_path()`. The child
gets all the correct paths — repo root, pip packages, stdlib — without PYTHONPATH.

Critically, the child does NOT get the bootstrap's sys.path[0] prepend (that only
happens in `stage2_bootstrap_template.py:main()`), so there's no stdlib shadowing.

Verified empirically on RBE: a subprocess spawned with `sys.executable -c` and no
PYTHONPATH (only `PYTHONSAFEPATH=1`) can `import util.bazel.subprocess` AND
`import subprocess` (stdlib) correctly.

## Fix: stop propagating PYTHONPATH

The fix is to remove (or drastically simplify) PYTHONPATH propagation in
`python_env()`. Instead of dumping sys.path into PYTHONPATH, just let the venv handle
it. The only thing `python_env()` should still do is set `PYTHONSAFEPATH=1` to prevent
CWD pollution.

**Caveat**: `generate_shell_wrapper()` bakes PYTHONPATH into shell scripts. These
wrappers run outside the venv (e.g., as standalone executables). They may still need
PYTHONPATH. Need to check if those shell wrappers also have access to the venv
Python or if they use a different interpreter.

Also need to verify: does this work for the Nix devShell case (non-Bazel subprocess
invocations)? The docstring mentions "Nix site.addsitedir paths" — these might not
be in the venv.

## Other failures

`//wt/shared:test_style` is a separate issue: `wt` becomes a namespace package
(no `__init__.py`), so `importlib.import_module("wt").__file__` returns `None`.
Fix: use `__path__` instead of `__file__`, or add an explicit `wt/__init__.py`.

## Reproduction

```bash
# Flag ON (broken) — sys.path[0] = _main/util/bazel/
bbr test //util/bazel:test_subprocess --test_arg=-k --test_arg=test_run_python_module_version

# Flag OFF (works) — sys.path[0] = _main/
bbr test //util/bazel:test_subprocess --test_arg=-k \
  --test_arg=test_run_python_module_version \
  --incompatible_default_to_explicit_init_py=false
```
