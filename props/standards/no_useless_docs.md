---
title: No useless documentation or comments
kind: outcome
---

There are no comments/docstrings that merely restate what is obvious from the immediate context (nearby lines, function signature, class/module names).

## Acceptance criteria (checklist)

- No docstrings/comments that merely restate what is obvious from the immediate context (± a few lines, function signature, class/module names)
- Argument/return sections appear only when semantics/constraints are non‑obvious
- Evaluation scope: Only agent‑added or agent‑edited hunks are considered; redundant comments elsewhere in the file do not violate this property
- Keep module/class/function docs that capture contracts, invariants, side‑effects, or non‑obvious decisions
- Remove template boilerplate and generated stubs that provide no additional signal

## Positive examples (no boilerplate; not restating immediate context)

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Music Editor")

class Track(BaseModel):
    title: str
    bpm: int

@app.post("/tracks")
def create_track(t: Track) -> Track:
    return t
```

## Negative examples (boilerplate restating immediate context)

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Music Editor")

class Track(BaseModel):
    title: str
    bpm: int

@app.post("/tracks")
def create_track(t: Track) -> Track:
    """Create a track and return it.

    Args:
        t: The Track to create

    Returns:
        The created Track
    """
    # Build a Track instance
    return Track(title=t.title.strip(), bpm=min(max(t.bpm, 40), 220))
```
