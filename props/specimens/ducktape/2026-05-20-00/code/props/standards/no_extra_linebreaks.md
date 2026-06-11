---
title: No unnecessary line breaks
kind: outcome
---

The parse tree is laid out in the minimum number of lines allowed by the configured linter, except where newlines are deliberately added to improve readability.
If code can fit on one line without harming readability and the linter would preserve it, it does.

## Acceptance criteria (checklist)

- Calls/constructors with short argument lists are on one line when the linter would not split them
- Expressions that can be a single line without reducing readability are written on one line
- It is acceptable to add at most one blank line to separate logical sections (e.g., Arrange/Act/Assert in tests)
- It is acceptable to break lines deliberately for readability (e.g., multi‑line string assembly), even if a single line would be valid
- Do not introduce two or more consecutive blank lines for spacing

## Positive examples

```python
# One-line constructor call (readable; linter keeps it on one line)
img = MediaContent(type="image", data=sample_png, mimeType="image/png")

# Intentional section spacing (at most one blank line)
# Arrange
foo = make_foo()
foo.prepare()

# Act
foo.activate()

# Multi-line string assembly for readability
headers = (
    "Content-Type: text/plain; charset=utf-8\n"
    "X-Env: prod\n"
    "X-Request-Id: 123\n"
)
```

## Negative examples

```python
# Unnecessarily split call with identical parse tree; should be single line
img = MediaContent(
    type="image",
    data=sample_png,
    mimeType="image/png",
)

# Excessive blank spacing (more than one empty line between sections)
# Arrange
foo = make_foo()


# Act
foo.activate()

# Gratuitous line breaks that neither improve readability nor are required by the linter
value = (
    compute_value()
)
```

### FastAPI configuration examples

#### Negative examples (identical parse tree, unnecessary breaks)

```python
from fastapi import APIRouter, Depends

def create_router() -> APIRouter:
    return APIRouter(
        prefix="/v1",
        tags=["tracks"],
        dependencies=[
            Depends(auth),
        ],
    )
```

#### Positive examples (same parse tree, compact layout)

```python
from fastapi import APIRouter, Depends

# One-line call with the same arguments
router = APIRouter(prefix="/v1", tags=["tracks"], dependencies=[Depends(auth)])
```
