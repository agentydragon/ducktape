# Findings: Available Type Stubs

## Summary

Both `types-sqlalchemy` and `types-pygit2` exist on PyPI but are not currently installed in this project.

## Available Packages

### types-sqlalchemy
- **Latest version**: 1.4.53.38
- **Purpose**: Type stubs for SQLAlchemy ORM
- **Status**: Not installed in project
- **Usage in codebase**: Extensive (29 files use sqlalchemy)
  - `adgn/src/adgn/rspcache/responses_db.py` - Main usage
  - `gatelet/` - Multiple files

### types-pygit2
- **Latest version**: 1.15.0.20250319 (March 2025)
- **Purpose**: Type stubs for pygit2 Git library
- **Status**: Not installed in project
- **Usage in codebase**: Extensive (26 files use pygit2)
  - `wt/` worktree management - Heavy usage
  - `adgn/src/adgn/git_commit_ai/` - Git commit AI
  - `adgn/src/adgn/mcp/git_ro/` - Git MCP server

## Current Cast Usage

### pygit2 Casts (could potentially be eliminated)
```python
# adgn/src/adgn/git_commit_ai/core.py:50
oid = repo.index.write_tree()
return cast(pygit2.Oid, oid)

# adgn/src/adgn/mcp/git_ro/server.py
obj: pygit2.Commit = cast(pygit2.Commit, maybe_commit)
```

These casts suggest that pygit2's type annotations are incomplete. Installing `types-pygit2` may eliminate the need for these casts.

## Recommendation

### Add to project requirements:
```txt
# Type stubs for better type checking
types-sqlalchemy>=1.4.53.38
types-pygit2>=1.15.0.20250319
```

### Validation Process
1. Install type stubs
2. Run mypy on files with casts
3. Remove unnecessary casts if types now infer correctly
4. Keep casts only where genuinely needed, with comments explaining why

### Expected Benefits
- Better IDE autocomplete for SQLAlchemy ORM
- Reduced need for casts in pygit2 code
- Earlier detection of type errors in database/git operations
- More confidence in refactoring

## Next Steps
1. Add to appropriate requirements file (likely `adgn/requirements.txt` or per-package `pyproject.toml`)
2. Test that existing casts can be safely removed
3. Document which casts remain necessary and why
