# GitPython Migration Plan

## Current Status
We currently use `subprocess.run()` to execute git commands and parse the raw output. While this works for basic cases, it has several limitations.

## Why GitPython?
[GitPython](https://gitpython.readthedocs.io/) is the de facto standard Python library for git operations:
- ✅ Maintained and widely used (10k+ stars on GitHub)
- ✅ Handles edge cases (unicode paths, renamed files, etc.)
- ✅ Type-safe API with proper error handling
- ✅ No need to parse raw git output
- ✅ Handles git binary location automatically
- ✅ Better error messages

## Migration Tasks

### Phase 1: Add GitPython as optional dependency
- [ ] Add `gitpython` to `pyproject.toml` as optional dependency
- [ ] Keep current parser as fallback
- [ ] Add environment variable to switch between implementations

### Phase 2: Implement GitPython-based parser
```python
# Example implementation
from git import Repo

def parse_git_diff_gitpython(diff_args: list[str] | None = None) -> list[FileChange]:
    """Parse git diff using GitPython library."""
    repo = Repo(search_parent_directories=True)

    if diff_args:
        # Parse commit references
        diff = repo.git.diff('--numstat', *diff_args)
    else:
        # Unstaged changes
        diff = repo.git.diff('--numstat')

    return parse_numstat_output(diff)
```

### Phase 3: Testing
- [ ] Add tests for GitPython implementation
- [ ] Test with edge cases (unicode, spaces, renamed files)
- [ ] Performance comparison with subprocess implementation
- [ ] Integration tests for both implementations

### Phase 4: Migration
- [ ] Make GitPython the default (if installed)
- [ ] Update documentation
- [ ] Add installation instructions for GitPython
- [ ] Deprecate subprocess-based parser

## Edge Cases GitPython Handles Better

1. **Renamed files**: GitPython can detect renames and provide old/new paths
2. **Unicode file names**: Proper encoding handling
3. **Git repository detection**: Automatically finds .git directory
4. **Error handling**: Better exceptions for invalid refs, missing commits, etc.
5. **Binary detection**: More reliable than parsing '-' in output

## Backwards Compatibility

Keep subprocess implementation as fallback:
```python
try:
    from git import Repo
    USE_GITPYTHON = True
except ImportError:
    USE_GITPYTHON = False

def parse_git_diff(diff_args=None):
    if USE_GITPYTHON:
        return parse_git_diff_gitpython(diff_args)
    else:
        return parse_git_diff_subprocess(diff_args)
```

## Dependencies Impact

Adding GitPython adds these dependencies:
- `gitpython` (required)
- `gitdb` (indirect, for object database)
- `smmap` (indirect, for memory mapping)

Total size: ~2MB installed

## Alternative: Stay with subprocess

If we want to avoid the dependency, we should at least:
- [ ] Use `shutil.which('git')` to check git availability
- [ ] Add proper timeout handling
- [ ] Validate diff_args to prevent injection
- [ ] Better error messages for common git errors
- [ ] Handle renamed files in numstat parsing
- [ ] Add unicode path tests

## Recommendation

**Use GitPython for production, keep subprocess for testing.**

Rationale:
- GitPython handles edge cases we haven't thought of
- Standard library approach (don't reinvent the wheel)
- Better long-term maintainability
- Minimal dependency cost (~2MB)
- Fallback to subprocess still available

## References

- [GitPython Documentation](https://gitpython.readthedocs.io/)
- [Git diff --numstat format](https://git-scm.com/docs/git-diff#Documentation/git-diff.txt---numstat)
- [Similar tools using GitPython](https://github.com/topics/gitpython)
