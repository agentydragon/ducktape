# Scan: pygit2 Type Narrowing Patterns

## Context
@../shared-context.md

## Pattern: Proper Type Narrowing Without Casts

### Seen in: adgn/mcp/git_ro/server.py

```python
# Pattern: Handle Tag vs Commit narrowing
obj_any = repo.revparse_single(objspec)
if isinstance(obj_any, pygit2.Tag):
    obj = obj_any.peel(pygit2.Commit)  # Tag -> Commit
elif isinstance(obj_any, pygit2.Commit):
    obj = obj_any  # Already a Commit, no peel needed
else:
    raise TypeError(f"Unexpected type: {type(obj_any)}")

# obj is now properly typed as pygit2.Commit
```

**Important**: `commit.peel(pygit2.Commit)` is identity (noop) - if you already have a Commit, just use it directly.

## Detection

```bash
# Find isinstance checks that could be simplified
rg --type py "isinstance.*pygit2\.(Tag|Commit)" -A3

# Find potentially unnecessary peels
rg --type py "peel\(pygit2\.Commit\)" -B2
```

## With types-pygit2 Installed

Type stubs (`types-pygit2>=1.15.0`) provide proper return types:
- `repo.index.write_tree()` returns `Oid` (not `Any`)
- `obj.peel(T)` returns `T`
- No casts needed with isinstance narrowing

## References

- [pygit2 Documentation](https://www.pygit2.org/)
- Install: `pip install types-pygit2`
