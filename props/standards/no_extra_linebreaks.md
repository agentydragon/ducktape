---
title: No unnecessary line breaks
kind: outcome
---

Keep a short expression or call on one line when the formatter permits it and the result is easy to read.
For example, write `APIRouter(prefix="/v1", tags=["tracks"], dependencies=[Depends(auth)])` without putting
each argument or the single list item on a separate line.

Break lines when the layout communicates structure. These headers fit on one line, but each line below
corresponds to one header in the assembled string:

```python
headers = (
    "Content-Type: text/plain; charset=utf-8\n"
    "X-Env: prod\n"
    "X-Request-Id: 123\n"
)
```

Within a function, one blank line can separate logical sections such as Arrange, Act, and Assert.
Extra blank lines between the same two sections add no information.
