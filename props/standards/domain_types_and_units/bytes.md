---
title: Byte sizes use explicit units and a single source of truth
kind: outcome
---

Represent byte counts with explicit units.
Prefer typed wrappers or clear suffixes when primitives are used.

## Acceptance criteria (checklist)

- Names include explicit units when using primitives (for example, `_bytes`, `_mib`)

## Positive examples

```go
const MaxUploadBytes int64 = 25 * 1024 * 1024 // 25 MiB
```

```python
MAX_DOWNLOAD_MBPS = 5
print(f"limit: {MAX_DOWNLOAD_MBPS} MB/s")
```

## Negative examples

Ambiguous units:

```go
const FileUploadMax int64 = 25000
```

```python
MAX_DOWNLOAD_SPEED = 5
```
