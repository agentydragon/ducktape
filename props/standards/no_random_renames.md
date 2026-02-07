---
title: Renames must pay rent (no random renames)
kind: outcome
---

Do not introduce aliases or new names unless they add clear value (disambiguation, collision avoidance, or stronger semantics). Prefer one obvious name per concept and reuse it consistently.

## Acceptance criteria (checklist)

- No import aliasing without a concrete reason:
  - Disallowed: `import json as j` used only to shorten `json`.
  - Allowed with rationale: name collision (`from httpx import Response as HttpxResponse`), contextual disambiguation (`from foo.api import Response as FooApiResponse`), or to avoid overshadowing a local symbol.
- No pass‑through aliases (one‑off renames) that add no semantics:
  - Disallowed: `x2 = x; process(x2)` when `process(x)` suffices.
  - Prefer inlining trivial values: `process(make_value())` when readability is unchanged. See also: [No one‑off vars](./no-oneoff-vars-and-trivial-wrappers.md).
- Consistent terminology: do not refer to the same thing by multiple different names in the same scope/module (e.g., calling a `MyServer()` instance `http_server` in one place and `processor` elsewhere) unless the roles truly differ and are documented.
- Contextual renames must strengthen meaning and then be used consistently:
  - Good: renaming a generic value to a domain‑specific one at the point its meaning becomes clear; drop the old name and continue with the precise one.
  - If the reason is non‑obvious, include a short inline comment (e.g., “avoid import cycle”, “disambiguate two Response types”). Misleading justifications violate [Truthfulness](./truthfulness.md).
- Avoid introducing parallel synonyms for the same concept (e.g., `interface` vs `protocol` vs `facade`) unless they represent distinct, well‑defined abstractions.

## Positive examples

Context adds semantics; new name replaces the old one:

```python
unsanitized_input = url_query.get("i")
if our_command_mode == Command.BUY:
    phone_number = unsanitized_input  # domain meaning becomes clear here
    # ... use phone_number from here on; do not keep using unsanitized_input
```

Disambiguate two Response types:

```python
from httpx import Response as HttpxResponse
from my_sdk.types import Response as MySdkResponse

def handle_http(r: HttpxResponse) -> MySdkResponse: ...
```

Avoid pass‑through alias; inline when simple:

```python
# instead of: tmp = make_payload(); send(tmp)
send(make_payload())
```

## Negative examples

Import alias without value:

```python
import json as j  # ❌ pointless alias

data = j.loads(text)
# prefer: import json; data = json.loads(text)
```

One‑off alias adds no meaning:

```python
x = foo()
x2 = x          # ❌ useless alias
process(x2)     # prefer: process(x)
```

Terminology drift for the same object:

```python
server = MyServer()
http_server = server     # ❌ duplicate name for same instance
processor = server       # ❌ misleading name; not a processor
```

## Notes

- Renames should “pay rent”: resolve a collision, remove ambiguity, or increase semantic precision. Otherwise, keep the original name.
- When you must rename for semantics, migrate fully to the new name in that scope; do not keep both alive.
- Cross‑refs: [No one‑off vars](./no-oneoff-vars-and-trivial-wrappers.md), [Self‑describing names](./self-describing-names.md), and [Truthfulness](./truthfulness.md).
