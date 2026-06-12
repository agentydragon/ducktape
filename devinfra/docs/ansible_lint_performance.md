# Ansible-Lint Performance

## Setup

Two-tier: pre-commit runs `ansible-playbook --syntax-check` per playbook
(~1–3s, via `ansible/scripts/run-syntax-check.sh`) — catches YAML/template
syntax errors, undefined variables, invalid module names. CI runs full
`ansible-lint` on all playbooks when `ansible/` changes (~42s, via
`.github/scripts/run-ansible-lint.sh`) — adds style, best-practice,
deprecation, and security rules. Full lint locally when needed:

```bash
cd ansible
ansible-lint --config-file ../.ansible-lint.yaml [wyrm.yaml]
```

## Why CI takes 42s (expected)

`ansible-playbook --syntax-check` spawns a fresh Ansible per playbook
(0.6–2s each; no multi-playbook invocation exists) — upstream limitation
confirmed in [ansible-lint discussion #1256](https://github.com/ansible/ansible-lint/discussions/1256);
no fix available. Our 191 files at 0.22s/file is better than typical
(zuul-roles: 330+ files, 80s). If `python3 -c "import yaml; print(yaml.__with_libyaml__)"`
prints `False`, reinstalling pyyaml with libyaml shaves a few seconds.

## Potential Upstream Optimizations (Not Yet Submitted)

From profiling this repo's runs; could be PRs to ansible/ansible-lint:

### Issue #1: Cache Package Version Lookups (5-6s savings)

**Location**: `src/ansiblelint/config.py:282` in `get_deps_versions()`

**Problem**: Function called **3,652 times** per run, queries package metadata each time.

**Fix**: Add `@functools.cache` decorator

```python
from functools import cache

@cache
def get_deps_versions() -> dict[str, Version | None]:
    """Return versions of most important dependencies."""
    # ... existing code ...
```

**Impact**: 5-6 seconds, trivial complexity, zero risk

### Issue #2: Cache File Type Detection (2-3s savings)

**Location**: `src/ansiblelint/file_utils.py:139` in `kind_from_path()`

**Problem**: **39,334 calls** to `posix.stat()`, up to 7 stat operations per file.

**Fix**: Add `@functools.lru_cache` decorator

```python
from functools import lru_cache

@lru_cache(maxsize=1024)
def kind_from_path(path: Path, *, base: bool = False) -> FileType:
    """Determine the file kind based on its name."""
    # ... existing code ...
```

**Impact**: 2-3 seconds, trivial complexity, low risk

### Issue #3: Optimize Deep Copying (1.5-2s savings)

**Location**: `src/ansiblelint/utils.py:712` in `_sanitize_task()`

**Problem**: **1.3 million calls** to `copy.deepcopy()`, full recursive copy of task structures.

**Fix**: Use selective copying instead of full deep copy

```python
def _sanitize_task(task: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Return a stripped-off task structure compatible with new Ansible."""
    # Shallow copy the top level
    result = dict(task)

    # Remove forbidden keys
    for key in [SKIPPED_RULES_KEY, FILENAME_KEY, LINE_NUMBER_KEY]:
        result.pop(key, None)

    # Selectively deep copy only mutable nested values
    for key, value in result.items():
        if isinstance(value, MutableMapping):
            result[key] = _sanitize_dict(value)
        elif isinstance(value, list):
            result[key] = copy.deepcopy(value)

    return result
```

**Impact**: 1.5-2 seconds, medium complexity, medium risk

### Issue #4: Reduce Subprocess Overhead (2-4s savings)

**Problem**: Multiple ansible subprocess calls per run: `ansible-config dump`,
`ansible --version`, `ansible-galaxy collection install/list` (~0.75s each),
plus the per-playbook syntax check (1.15s each).

**Optimizations**: cache collection metadata, skip version checks in offline
mode, cache ansible-doc module info.

**Impact**: 2-4 seconds, medium complexity, low risk

### Summary

| Issue                   | Fix            | Complexity | Savings | Risk   |
| ----------------------- | -------------- | ---------- | ------- | ------ |
| #1: Package versions    | `@cache`       | Trivial    | 5-6s    | None   |
| #2: File stats          | `@lru_cache`   | Trivial    | 2-3s    | Low    |
| #3: Deep copying        | Selective copy | Medium     | 1.5-2s  | Medium |
| #4: Subprocess overhead | Multiple fixes | Medium     | 2-4s    | Low    |

Total potential: 11-15s (42s → 27-31s); the ~20s of per-playbook subprocess
overhead remains unavoidable regardless.
