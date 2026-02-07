---
title: Domain‑appropriate types and explicit units
kind: outcome
---

Use semantically rich, domain‑appropriate types instead of bare primitives, and make units explicit. Convert primitive inputs at boundaries; keep a single canonical internal unit/type.

## Acceptance criteria (checklist)

- Time: use rich time types; avoid raw epoch ints/floats in core logic
- Paths: use platform path libraries (`pathlib.Path` in Python; `path/filepath` in Go); no string concatenation
- URLs: build/parse with standard libraries (`urllib.parse`, `net/url`); no manual string concatenation
- Byte sizes: explicit unit suffixes (e.g., `_bytes`, `_mib`) or typed wrappers; one source of truth for limits
- Physical quantities: explicit unit suffixes (e.g., `_meters`, `_celsius`) or unit libraries; normalize to canonical internal units
- Boundary conversion: convert inbound/outbound primitives at edges

## Motivating examples

Good (single coherent example):

```python
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlunparse

cfg_path = Path(args.config)
started_at = datetime.fromtimestamp(args.started_at_unix_ms / 1000, tz=timezone.utc)
qs = urlencode({"q": query, "limit": 100})
api_url = urlunparse(("https", "api.example.com", "/items", "", qs, ""))

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
```

Bad (ambiguous primitives and string assembly):

```python
cfg_path = args.config              # str, not Path
started_at = args.started_at        # int? ms? seconds? unknown
api_url = f"https://api.example.com/items?q={query}&limit=100"  # manual concatenation
MAX_UPLOAD = 25                     # units unclear
```

## Sub‑rules

- [Time and duration](time.md)
- [URLs](urls.md)
- [Byte sizes](bytes.md)
- [Physical quantities](physical-quantities.md)
- [Paths (PathLike, Python)](../python/pathlike.md)
- [Pathlib usage (Python)](../python/pathlib.md)
