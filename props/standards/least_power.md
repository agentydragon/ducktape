---
title: Principle of least power ("no sus code")
kind: behavior
---

Choose the least powerful construct that achieves the goal safely. “Sus” patterns are those that carry avoidable risk (silent failure, reflection, low‑level primitives) when a simpler, safer alternative exists. Escalate only when required, and document why.

## Rules

- Least power first: when two approaches are functionally equivalent, choose the safer, higher‑level, more explicit one.
- Escalate only with rationale: if you bypass the safer option, add a short inline comment explaining the constraint (cycle, performance, boundary contract).
- Keep the risky thing local: scope the “bigger gun” to the smallest block; never wrap whole modules in low‑level mechanisms; fail fast and surface errors.

## Escalation ladders (prefer left; move right only when required)

- Imports: module‑top `from x import y` → inline `from x import y` (cycle, documented) → `importlib.import_module(...)` → `__import__(...)` (almost never)
- Data modeling: typed model attributes → structured `TypedDict` (simple shapes) → boundary dicts/serde only → ad‑hoc dict wrangling (avoid)
- Output/IO: `print()`/logger → logging APIs → `sys.stdout.write` (rare) → `os.write` (avoid for ordinary output)
- Concurrency: `asyncio.gather`/Tasks → `run_in_executor`/`ThreadPoolExecutor` (scoped) → raw `Thread`/Pool (avoid in async code)
- Dynamic access: concrete types/pattern matching → registry mapping → `getattr`/`hasattr`/`setattr` (forbidden when direct access is equivalent)
- Processes/OS: `Path`/`pathlib`/`shutil`/`subprocess.run(check=True)` → `os.*` syscalls (only with hard requirement)
- Errors: specific, narrow try/except with surfacing → broad boundary catch with logging → blanket catch that hides failure (forbidden)
- Public API: explicit imports from defining module → curated `__init__`+`__all__` (real library only) → convenience barrels in internal code (forbidden)
- Tests: real resources/models (`tmp_path`, real Pydantic) → narrow stubs at boundaries → mocking plain data/patching whole modules (forbidden)

## Positive examples

Explicit import + typed model:

```python
from mypkg.models import User

u = User(id="u_123", email="u@example.com")
print(u.email)
```

Async concurrency with gather:

```python
async def load_all(ids: list[str]) -> list[Item]:
    return await asyncio.gather(*(fetch_one(i) for i in ids))
```

High‑level IO/logging:

```python
print("done")                 # tests/scripts
logger.info("started", extra={"count": n})  # production
```

## Negative examples (sus code)

Dynamic import without need:

```python
obj = __import__("mypkg.plugin").run()  # ❌ use explicit imports; importlib only with rationale
```

Ad‑hoc dict wrangling for domain data:

```python
user = {}
user["id"] = 1             # ❌ use a model; avoid opaque dicts
```

Low‑level stdio for ordinary output:

```python
sys.stdout.write("ok\n")     # ❌ prefer print() or logger
```

Manual threads in async code:

```python
# ❌ suspicious in an async codebase; prefer gather/executor
threads = [Thread(target=fetch, args=(i,)) for i in ids]
[t.start() for t in threads]
[t.join() for t in threads]
```

Low‑level OS process/syscalls when high‑level exists:

```python
os.system("cmd")  # ❌ prefer subprocess.run(["cmd"], check=True)
fd = os.open("out.txt", os.O_WRONLY | os.O_CREAT)  # ❌ prefer Path("out.txt").write_text(data)
```

## Notes

- Principle of least power: pick the simplest construct that expresses intent safely; only escalate when there’s a clear, documented need.
- Scope: this property targets patterns that increase risk (silent failures, implicit behavior, needless power) when safer alternatives exist. Pure style modernizations (e.g., using `A | B` over `Union[A, B]`) live under [Modern Python idioms](./python/modern-python-idioms.md) and are not "sus" on their own.
- These are heuristics; exceptions exist, but require a short inline rationale.
- Related properties: see cross‑links above for exact rules and exceptions.
