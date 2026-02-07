---
title: URLs are built and parsed with standard libraries
kind: outcome
---

Construct and parse URLs using standard libraries, not string concatenation.
Encode query parameters with library helpers and validate/normalize URLs at boundaries.

## Acceptance criteria (checklist)

- Python: use `urllib.parse` (`urlparse`, `urlunparse`, `urlencode`, `urljoin`)
- Go: use `net/url` (`url.Parse`, `url.URL`, `url.Values.Encode`)
- No manual concatenation of scheme/host/path/query strings
- Validate and normalize at boundaries; store and pass structured URL objects internally when feasible

## Positive examples

```python
from urllib.parse import urlencode, urlunparse
qs = urlencode({"q": query, "page": 2})
url = urlunparse(("https", "api.example.com", "/search", "", qs, ""))
```

```go
u, _ := url.Parse("https://api.example.com/search")
q := u.Query()
q.Set("q", query)
q.Set("page", "2")
u.RawQuery = q.Encode()
```

## Negative example

Manual concatenation (missing encoding, brittle):

```python
url = f"https://api.example.com/search?q={query}&page={page}"
```
