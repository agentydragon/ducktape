---
title: Truthfulness
kind: behavior
---

All text and identifiers truthfully represent reality: code, names, docs, comments, logs, metrics, schemas, and help reflect actual behavior and intent.

## Acceptance criteria (checklist)

- Names reflect actual semantics and primary effect; avoid names that suggest different behavior than implemented
- Docstrings summarize purpose, inputs, outputs, side effects, and are updated alongside behavior changes
- Comments explain non‑obvious "why"/constraints; remove stale or contradictory/historical comments
- Identifiers are free of typos that reduce clarity (e.g., `isValidUTF8`, not `isValidUt8`)
- Help/CLI usage text and schemas (required/optional flags, defaults, units) match runtime behavior
- Log messages and metrics names/labels truthfully describe the actual event/measurement
- Error/diagnostic messages accurately state failure causes and actionable remediation
- Examples in docs and tests demonstrate supported behavior and fail if behavior changes
- Behavior changes are accompanied by updates to all affected user‑facing text (docs, help, comments) in the same change

## Negative examples

Misleading name - `getFileExtension` returns fake output path, not an extension:

```go
func getFileExtension(format string) string {
    if format == "markdown" {
        return "fetch.md"
    } else if format == "python" {
        return "fetch.py"
    ...
}
```

Typo:

```go
func isValidUt8(b []byte) bool { /* ... */ }
```

Stale/contradictory comment says `path` is required, but code accepts `None` and defaults:

```python
# path is required
path = ...
...
if path is None:
    return DEFAULTS
```

Docstring claims caching but function doesn't actually cache, only loads+parses:

```python
def _load_yaml_file(self, path: Path) -> Any:
    """Load and cache YAML file content."""  # <-- false
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
```

## Positive examples

Fixed name correctly reflects returned value:

```go
func syntheticFormatPath(format string) string {
    // ...
}
```

```python
def load_config(path: str | None) -> dict:
    """Load configuration.

    If path is None, load from defaults; otherwise read from given file.
    Returns a dictionary with validated keys only.
    """
    # ...
```

Non-obvious rationale captured briefly (good):

```python
# Trim trailing whitespace because upstream API mishandles it (issue #1234)
text = text.rstrip()
```

## Cross-references

- [Self‑describing names](./self-describing-names.md)
- [No useless docs](./no-useless-docs.md)
