---
title: Uses str.removeprefix / str.removesuffix for fixed prefix/suffix removal
kind: outcome
---

Agent-edited Python uses `str.removeprefix` and `str.removesuffix` for removing fixed prefixes/suffixes instead of manual slicing.

## Acceptance criteria (checklist)

- For fixed prefix removal, use `s.removeprefix(prefix)` instead of `s[len(prefix):]` or `s[4:]`
- For fixed suffix removal, use `s.removesuffix(suffix)` instead of `s[:-len(suffix)]` or `s[:-4]`
- Logic that conditionally removes only when present should not duplicate checks; `removeprefix`/`removesuffix` are already safe

## Positive examples

```python
name = "prod_db"
assert name.removeprefix("prod_") == "db"

path = "file.tmp"
assert path.removesuffix(".tmp") == "file"
```

```python
# Conditional removal without extra checks
branch = "feature/foo"
branch = branch.removeprefix("feature/")
```

## Negative examples

```python
name = "prod_db"
name = name[len("prod_"):]

path = "file.tmp"
path = path[:-len(".tmp")]

branch = "feature/foo"
if branch.startswith("feature/"):
    branch = branch[len("feature/"):]
```
