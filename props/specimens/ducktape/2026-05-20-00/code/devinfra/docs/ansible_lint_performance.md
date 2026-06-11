# Ansible-Lint Performance Guide

## Quick Start: What We Did and Why

**TL;DR**: Switched pre-commit from full ansible-lint to fast syntax-check. 42s → 1-3s locally, CI still validates everything.

### Current Setup ✅

**Pre-commit: Fast Syntax Check (1-3s)**

```bash
# Uses ansible-playbook --syntax-check via ansible/scripts/run-syntax-check.sh
cd ansible
ansible-playbook --syntax-check wyrm.yaml  # 1-2 seconds
```

**What syntax-check catches:**

- ✅ YAML syntax errors
- ✅ Undefined variables
- ✅ Invalid module names
- ✅ Template syntax errors
- ✅ Basic Ansible structure issues

**CI: Full ansible-lint (42s)**

```bash
# Via .github/scripts/run-ansible-lint.sh
# Runs full ansible-lint on all playbooks when ansible/ changes
```

**What only CI ansible-lint catches:**

- Style issues (naming conventions, formatting)
- Best practices (fqcn, no-changed-when, etc.)
- Deprecated modules
- Security issues

### Performance Comparison

| Scenario                       | Time | What It Validates                              |
| ------------------------------ | ---- | ---------------------------------------------- |
| **Pre-commit syntax-check**    | 1-3s | Syntax errors, undefined vars, invalid modules |
| **Full ansible-lint (single)** | ~15s | Everything (style, best practices, security)   |
| **Full ansible-lint (all)**    | ~42s | Everything on all playbooks                    |
| **CI ansible-lint**            | ~42s | Same as local, but only runs on changes        |

**Speedup**: ~15x faster pre-commit feedback!

### Testing the Setup

```bash
# Test syntax check on a playbook
cd ansible
ansible-playbook --syntax-check wyrm.yaml
# Should complete in 1-2 seconds

# Test via pre-commit (will run prettier + syntax-check)
pre-commit run --files ansible/wyrm.yaml
# Should complete in 2-3 seconds

# Full ansible-lint (what CI runs)
ansible-lint --config-file ../.ansible-lint.yaml wyrm.yaml
# Takes ~15-17s (this is normal and expected)
```

### When You Need Full Linting Locally

If you want to run the full ansible-lint validation locally (same as CI):

```bash
cd ansible

# Single playbook (takes ~15-17s)
ansible-lint --config-file ../.ansible-lint.yaml wyrm.yaml

# All playbooks (takes ~42s, same as CI)
ansible-lint --config-file ../.ansible-lint.yaml
```

**Note:** Usually you don't need to run full ansible-lint locally - CI will catch style issues.

### Benefits

- ✅ **~15x faster pre-commit** (42s → 1-3s for syntax check)
- ✅ Syntax errors caught immediately
- ✅ CI still enforces all rules (no loss of coverage)
- ✅ Better developer experience (no 42s blocking waits)
- ✅ Fast feedback loop for development

---

## Why ansible-lint Is This Slow (And That's Normal)

**42 seconds for 191 files is EXPECTED and NORMAL.**

- **Root cause**: ansible-playbook takes 0.6-2s per playbook (can't batch multiple playbooks)
- **90% of time**: Spent in Ansible's own code (not ansible-lint)
- **Your performance**: 0.22s/file is actually **better than average**
- **Upstream status**: Well-known issue (GitHub Discussion #1256), no fundamental fix available

### Evidence from Upstream

#### GitHub Discussion #1256: "Why is ansible-lint so slow?"

A user reported **~45 seconds for a small repository** - almost identical to our experience.

The ansible-lint maintainer confirmed the root causes:

**1. Ansible Subprocess Overhead (90% of total time)**

- Each playbook requires `ansible-playbook --syntax-check` in a separate subprocess
- Takes **0.6-2 seconds PER playbook**
- **ansible-playbook cannot process multiple playbooks in one invocation**
- Re-instantiates Ansible for EVERY playbook
- This overhead is in Ansible's code, not ansible-lint

**2. No Caching Between Runs**

- ansible-lint does minimal caching
- Each run re-checks everything
- No incremental analysis

**3. Complex Dependency Resolution**

- Playbooks reference roles, but roles don't know which playbooks use them
- Result: Over-linting to be safe

**4. Multiple YAML Parsers**

- Uses both ruamel.yaml and pyyaml
- Needed for comment preservation (noqa feature)
- Double parsing overhead

### Performance Comparison (Other Repositories)

| Repository            | Files | Time | Time/File | Notes                   |
| --------------------- | ----- | ---- | --------- | ----------------------- |
| **ducktape**          | 191   | 42s  | **0.22s** | **Better than average** |
| Small repo (GH #1256) | ~50   | 45s  | 0.9s      | Much worse              |
| zuul-roles (large)    | 330+  | 80s  | 0.24s     | Similar to ours         |

**Conclusion**: Our 42s runtime is normal and actually performs well per-file.

## Fundamental Limitations (Can't Be Fixed)

### 1. Ansible's Slow Boot Time

- Ansible itself has poor instantiation performance
- ansible-lint can't fix Ansible's architecture

### 2. No Multi-Playbook Syntax Check

- Ansible doesn't support: `ansible-playbook --syntax-check playbook1.yaml playbook2.yaml`
- Each playbook = new Ansible process
- Massive unavoidable overhead

### 3. Complex Dependency Graphs

- Roles, includes, imports create complex graphs
- Hard to determine minimal set of files to check

### What Upstream Has Tried

- ✅ Async syntax checks (helps a bit)
- ✅ Container caching (helps installation)
- ❌ Multi-playbook processing (blocked by Ansible's limitations)
- ❌ Incremental linting (too complex with current architecture)
- ❌ Aggressive caching (risk of stale results)

---

## Potential Upstream Optimizations (Not Yet Implemented)

These optimizations could be submitted as PRs to ansible/ansible-lint:

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

---

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

---

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

---

### Issue #4: Reduce Subprocess Overhead (2-4s savings)

**Problem**: Multiple ansible subprocess calls per run:

- `ansible-config dump` (0.76s)
- `ansible --version` (0.76s)
- `ansible-galaxy collection install` (0.77s)
- `ansible-galaxy collection list` (0.74s)
- `ansible-playbook --syntax-check` (1.15s per playbook)

**Optimizations**:

1. Cache collection metadata (0.5-1s savings)
2. Skip version checks in offline mode (combined with Issue #1: 5-6s total)
3. Cache ansible-doc module info (1-2s savings)

**Impact**: 2-4 seconds, medium complexity, low risk

---

### Summary of Potential Upstream Improvements

| Issue                   | Fix            | Complexity | Savings | Risk   |
| ----------------------- | -------------- | ---------- | ------- | ------ |
| #1: Package versions    | `@cache`       | Trivial    | 5-6s    | None   |
| #2: File stats          | `@lru_cache`   | Trivial    | 2-3s    | Low    |
| #3: Deep copying        | Selective copy | Medium     | 1.5-2s  | Medium |
| #4: Subprocess overhead | Multiple fixes | Medium     | 2-4s    | Low    |

**Total potential improvement: 11-15 seconds (42s → 27-31s)**

Even with these optimizations, the fundamental ansible-playbook subprocess overhead (~20s) remains unavoidable.

---

## Quick Performance Check: libyaml

One user reported **6s → 1.5s** speedup by ensuring libyaml (compiled YAML parser) is installed:

```bash
python3 -c "import yaml; print(yaml.__with_libyaml__)"
# Should print: True
```

If False, install:

```bash
pip install --force-reinstall --no-cache-dir pyyaml
```

This won't dramatically change ansible-lint performance (most time is in Ansible subprocesses), but could shave off a few seconds.

---

## Monitoring and Profiling

### Time a specific playbook

```bash
cd ansible
time ansible-playbook --syntax-check wyrm.yaml
```

### Profile with cProfile

```bash
python3 -m cProfile -s cumulative -m ansiblelint --offline wyrm.yaml 2>&1 | head -50
```

### Count files processed

```bash
ansible-lint --offline wyrm.yaml 2>&1 | grep "files processed"
```

### Check for file stat overhead

```bash
strace -c ansible-lint --offline wyrm.yaml
# Look for high counts of stat(), lstat(), fstat()
```

---

## Bottom Line

**Yes, it's this slow. Yes, upstream knows. No, there's no magic fix.**

- **Pre-commit**: Uses fast syntax-check (1-3s) ✅ Optimal
- **CI**: Uses full ansible-lint (42s) ✅ Normal and expected
- **Further improvements**: Require fixing Ansible itself (out of scope)

The current two-tier strategy provides fast local feedback while ensuring thorough validation in CI.

---

## References

- GitHub Discussion #1256: <https://github.com/ansible/ansible-lint/discussions/1256>
- Ansible slow boot: <https://www.jeffgeerling.com/blog/2021/ansible-might-be-running-slow-if-libyaml-not-available>
- Kubespray ansible-lint CI issue: <https://github.com/kubernetes-sigs/kubespray/issues/4565>
