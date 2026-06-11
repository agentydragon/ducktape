# Repository Code Health Audit — Full Properties (This Codebase)

Goal

- Audit a codebase for code‑health issues and design/architecture smells.
- Apply every formal property defined in this repository (embedded below).
- Produce two outputs: (1) Findings with precise anchors, (2) A prioritized Plan.

How To Work

- Read‑only analysis; do not modify files. You may run read‑only tools (linters, type‑checkers, complexity/duplication scanners) if available, but your judgment must follow the written rules below.
- Apply properties strictly by their wording. Do not stretch or infer beyond what a definition actually states.
- Cite precise anchors for evidence (file:line or file:start‑end, 1‑based). For many similar cases, use one short rationale plus a compact list of anchors.

Broader Smells and Structural Anti‑Patterns

- Duplication/drift; excess complexity; least‑power violations; dynamic attribute footguns; swallowing errors; wrong‑direction dependencies; cross‑layer coupling; private reach‑through; brittle exception detection; always‑on heavy deps; protocol leakage; duplicate mechanisms; barrel imports hiding real deps; typing/contract issues; logging/diagnostics gaps; testing gaps.

Deliverables (print exactly these sections)

1. Findings (grouped by property, structural smells, other smells) with anchors.
2. Plan (prioritized phases; map to properties/smells; call out risky steps).

---

## Embedded Property Definitions (exhaustive)

### Property: consistent-naming-and-notation.md

````md
---
title: Consistent naming and notation
kind: outcome
---

Adopt one clear naming/notation convention per project (or per package) and apply it uniformly. Avoid mixing file/identifier patterns that describe the same concept with different names or layouts.

## Acceptance criteria (checklist)

- Test files follow a single, consistent convention across the project (or per top-level package), e.g., `pkg/test_foo.py` (preferred) or `pkg/foo_test.py`; do not mix patterns within the same scope.
- Choose one test location strategy and stick to it for a given project/package:
  - Co-located: `src/<pkg>/tests/test_*.py`
  - Central: `tests/<pkg>/test_*.py`
    Mixing both within the same project/package is a violation.
- Directory placement is consistent: keep related tests together under their package/module (e.g., `my_service/test_*.py`, `other_service/test_*.py`), not scattered across differently named paths.
- File names use one tokenization scheme consistently (e.g., underscores, no intermix of custom affixes/orderings like `test_pkg_run_bar.py` vs `test_pkg_baz.py`).
- Avoid parallel synonyms for the same concept in names (e.g., `interface` vs `protocol` vs `facade`) unless distinctions are intentional and documented; prefer one obvious name. See also: [Renames must pay rent](./no-random-renames.md).
- Exceptions (legacy pockets, third‑party layout) must be isolated and documented in a short comment or contributing guide; new files conform to the chosen convention.

## Positive examples

Consistent per‑package test layout and naming (central tests/ style):

```text
my_service/
  test_foo.py
  test_bar.py
  test_baz.py
other_service/
  test_xyzzy.py
```
````

Or, suffix style (keep it consistent):

```text
my_service/
  foo_test.py
  bar_test.py
```

Or, co‑located tests (stick to it project‑wide):

```text
src/
  foo/
    bar.py
    tests/
      test_bar.py
```

## Negative examples

Mixed/misaligned test file naming and placement:

```text
test_my_service_foomethod.py
test_my_service_run_bar.py
my_service/test_baz.py
other_service/test_xyxxy_endpoint.py
```

Mixed tokenization and ordering for the same scope:

```text
my_service/test_run_bar.py
my_service/test_baz.py
my_service/run_baz_test.py   # different pattern
```

Mixing co‑located and central test conventions in one project/package (do not mix):

```text
src/foo/bar.py
src/foo/tests/test_bar.py
tests/foo/test_baz.py   # ❌ mixed conventions
```

## Notes

- Pick one convention per project; consider documenting it in CONTRIBUTING.md; use linters/review to keep it consistent.
- Consistency reduces cognitive load and speeds navigation/grep.
- Related properties: [Renames must pay rent](./no-random-renames.md), [Self‑describing names](./self-describing-names.md).

````


### Property: domain-types-and-units/bytes.md

```md
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
````

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

````


### Property: domain-types-and-units/index.md

```md
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
````

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

````


### Property: domain-types-and-units/physical-quantities.md

```md
---
title: Physical quantities use explicit units or typed unit systems
kind: outcome
---

Variables representing physical quantities (distance, temperature, speed, angles, etc.) must have unambiguous units.
Prefer rich, domain-appropriate types or unit libraries, or at least encode units in names.
Maintain a single consistent internal unit per quantity to avoid drift.

## Acceptance criteria (checklist)
- Use typed units where practical
  - Python: prefer a unit library (for example, Pint) or typed wrappers; [use `datetime` types for time/duration](time.md)
  - Go: define small newtypes (for example, `type Meters float64`) or structs with methods; avoid untyped `float64` for mixed units
- If primitives are used, names explicitly include units - e.g.: `user_height_cm`, `speed_mph`, `roll_degrees`.
- Choose one canonical internal unit per quantity (for example, meters for length, m/s for speed)
  - Convert inputs to canonical units at the boundary; convert back on output
- Logging/UI/docs include units (derive labels from the same source of truth where possible)
- Validate and normalize at boundaries (reject unknown units, handle case/spacing)
- Adoption guidance: if your codebase starts accumulating multiple physical quantities and conversions, adopt a unit library (for example, Pint) to prevent errors and centralize conversions

## Positive examples

If code does not handle many physical quantities, OK to just use suffixes to mark quantities with units:

```python
from dataclasses import dataclass

@dataclass
class ThermostatReading:
    temperature_celsius: float

def adjust(target_celsius: float, current_celsius: float) -> float:
    error_celsius = target_celsius - current_celsius
    return error_celsius
````

Once writing unit-heavier code (e.g., physics), use a unit library:

```python
from pint import UnitRegistry
ureg = UnitRegistry()
Q_ = ureg.Quantity

@dataclass
class Pose:
    x: Q_  # meters
    y: Q_

MAX_STEP: Q_ = 0.25 * ureg.meter

def step(pos: Pose, dx_cm: float, dy_cm: float) -> Pose:
    # Convert at boundary; internals remain meters
    dx = (dx_cm * ureg.centimeter).to(ureg.meter)
    dy = (dy_cm * ureg.centimeter).to(ureg.meter)
    new = Pose(pos.x + dx, pos.y + dy)
    return new if (abs(dx) <= MAX_STEP and abs(dy) <= MAX_STEP) else pos
```

```go
// Go — dedicated type clarifies units
package physics

type Meters float64

type Pose struct{ X, Y Meters }

const MaxStep Meters = 0.25

func Step(p Pose, dxCM, dyCM float64) Pose {
    dx := Meters(dxCM / 100.0) // convert cm -> m at boundary
    dy := Meters(dyCM / 100.0)
    newP := Pose{p.X + dx, p.Y + dy}
    if abs(dx) <= MaxStep && abs(dy) <= MaxStep { return newP }
    return p
}
```

## Negative examples

Ambiguous units and mixed arithmetic:

```python
speed = 3.6   # km/h? m/s? mph?
step = 25     # cm? m?
position = position + step  # *boom* - nothing prevents unit logic error
```

## Exceptions

- Protocol/file format boundaries that mandate specific units (for example, Fahrenheit, centimeters) may use those units at the edge; convert immediately to canonical units internally
- Short-lived locals immediately involved in a conversion expression may omit suffixes when unit is obvious and enforced by surrounding typed context

## Guidance

- Prefer SI base units internally (meters, seconds, kilograms, celsius/kelvin) and well-known derived units where conventional (m/s)
- Centralize conversions behind helpers to avoid copy/paste and drift
- If dealing with many physical quantities, adopt a unit library (for example, Pint) to make unit errors unrepresentable

````


### Property: domain-types-and-units/time.md

```md
---
title: Time and duration use rich time types
kind: outcome
---

Represent timestamps and durations with semantically rich time types rather than raw numbers.
Convert primitive epoch values at boundaries and keep a single, consistent internal representation.

## Acceptance criteria (checklist)
- Timestamps use timezone‑aware datetime objects (Python) or `time.Time` (Go) internally; no raw epoch ints/floats in core logic
- Durations/timeouts use `datetime.timedelta` (Python) or `time.Duration` (Go); avoid float/int seconds in internals
- Boundary handling:
  - Convert inbound epochs (e.g., Unix seconds/millis) to rich types immediately at the edge
  - When primitives are unavoidable, names carry explicit unit suffixes (e.g., `created_at_unix_ms`, `deadline_unix_sec`)
- Logs/docs include units and derive labels from the same source of truth (constants)
- Do not mix different time bases in one calculation

## Positive examples

```python
# Python — progress logging using timedelta
from datetime import datetime, timedelta, timezone

PROGRESS_INTERVAL = timedelta(seconds=1)
last_print = datetime.now(timezone.utc)

now = datetime.now(timezone.utc)
if (now - last_print) >= PROGRESS_INTERVAL:
    log_progress()
    last_print = now
````

```python
# Python — convert boundary epoch to aware datetime immediately
start_unix_ms: int = args.start_epoch_ms
start_at = datetime.fromtimestamp(start_unix_ms / 1000, tz=timezone.utc)
```

```go
// Go — duration types for timeouts/intervals
var ReadyPollInterval = 500 * time.Millisecond
var OverallTimeout = 30 * time.Second

ticker := time.NewTicker(ReadyPollInterval)
defer ticker.Stop()
deadline := time.Now().Add(OverallTimeout)
for {
    if ready() { break }
    if time.Now().After(deadline) { return context.DeadlineExceeded }
    <-ticker.C
}
```

## Negative examples

Mixing float seconds and datetimes; ambiguous units:

```python
last = time.time()
if time.time() - last > 1.0:  # float seconds in core logic — avoid
    ...
```

Storing epoch ints in internals instead of converting at the edge:

```python
created_at_unix_ms: int = fetch()["created_at"]  # keep as int throughout — avoid
```

Using plain ints for timeouts in core logic — avoid:

```go
var timeoutSec = 30
if elapsedSec > timeoutSec { /* ... */ }
```

## Exceptions

- Interfacing with protocols/DBs that represent time numerically is allowed at boundaries; convert immediately to internal rich types
- Performance‑critical tight loops may use numerics when justified and documented; conversions must stay localized and lossless for the use case

````


### Property: domain-types-and-units/urls.md

```md
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
````

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

````


### Property: early-bailout.md

```md
---
title: Early bailout (guard clauses and loop guards)
kind: outcome
---

Functions and loops avoid unnecessary nesting by exiting early on failing preconditions; trivial top-level guards are expressed as early return/raise/continue/break, not as wrapping if-blocks.

## Acceptance criteria (checklist)
- Function guard: When a precondition fails and there is no corresponding else branch, use an early exit (return/raise) instead of wrapping the rest of the function in an if-block
- Multiple trivial guards: Sequential single-branch if-guards with no else are written as separate early exits (one per condition) or combined logically when clearer (e.g., `if not a or not b: return`), not as nested ifs
- Loop guard: When the first statement of a loop guards the entire body, use `continue` (or `break`) instead of wrapping the body in an if-block
- Error/flag pattern: Do not set sentinel flags and branch later; raise/return immediately at the detection site when no additional work is needed before exit
- With shared cleanup that would prevent early return, extract a helper so early exits are possible without duplicating cleanup
- Combine with "No unnecessary nesting" and walrus rules: flatten `if a: if b:` into `if a and b:` and use `:=` where it enables a single clear guard

## Positive examples
```python
# Function guard: fail fast on preconditions
def load_user(uid: str) -> User:
    if not uid:
        raise ValueError("uid required")
    if not uid.startswith("u_"):
        raise ValueError("invalid uid")
    return repo.get(uid)
````

```python
# Loop guard: continue instead of wrapping entire body
for job in jobs:
    if not job.ready:
        continue
    process(job)
```

```python
# Combine trivial guards with walrus
if (rec := get_record(key)) is None or rec.error:
    return None
return rec.value
```

```js
// JS: early returns instead of nested ifs
function handle(req) {
  if (!req.auth) return unauthorized();
  if (!req.body) return badRequest();
  return ok(process(req.body));
}
```

```python
# Shared cleanup via helper enables early bailout
conn = connect()
try:
    def _do():
        if not request.valid:
            return None
        if not has_perm(request.user):
            return None
        return serve(request)
    result = _do()
finally:
    conn.close()
```

## Negative examples

```python
# Trivial else wrapping the happy path — should early‑return
def handle(req):
    if not req.auth:
        return unauthorized()
    else:
        # Entire body is under else
        result = process(req)
        return ok(result)
```

```python
# Loop uses else to skip — should guard with continue
for task in tasks:
    if task.ready:
        run(task)
        log(task)
    else:
        continue
```

```python
# Function wrapped by a trivial if — should be early return
def load_user(uid: str) -> User | None:
    if uid:
        user = repo.get(uid)
        return user
```

```python
# Nested trivial guards — should be flattened or early-exited
def save(item):
    if item:
        if item.valid:
            if not item.error:
                return persist(item)
```

```python
# Loop body wrapped — should use continue
for task in tasks:
    if task.ready:
        run(task)
        log(task)
```

```python
# Error flag used later — should raise immediately
ok = True
for part in parts:
    if part.invalid:
        ok = False
# ... many lines later ...
if not ok:
    raise ValueError("invalid part")
```

````


### Property: least-power.md

```md
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
````

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
- Scope: this property targets patterns that increase risk (silent failures, implicit behavior, needless power) when safer alternatives exist. Pure style modernizations (e.g., using `A | B` over `Union[A, B]`) live under [Modern Python idioms](./modern-python-idioms.md) and are not “sus” on their own.
- These are heuristics; exceptions exist, but require a short inline rationale.
- Related properties: see cross‑links above for exact rules and exceptions.

````


### Property: markdown/inline-formatting.md

```md
---
title: Markdown inline formatting for code identifiers, flags, paths, and URIs
kind: outcome
---

Inline syntactic elements in Markdown are properly formatted: code spans for commands, flags, identifiers, and paths; links or autolinks for HTTP(S) URLs; code spans for non-linkable URIs/URNs.

## Acceptance criteria (checklist)
- Executables and script names use inline code: `some-script.sh`
- Flags/options and invocations use inline code: `--flag=value`, `tool --flag=value`, `foo_method(...)`
- Identifiers use inline code when referenced in prose: `FooClass`, `foo_method`, `CONSTANT_NAME`
- File/system paths use inline code: `src/some/path.py`, `/etc/hosts`
- HTTP/HTTPS URLs are linkified: `<https://example.com/path>` or `[useful link](https://example.com/path)`
- Non-HTTP URIs/URNs use inline code unless a renderer will linkify them: `gs://bucket/key`, `s3://bucket/object`, `az://container/blob`
- Do not leave these tokens as plain text in prose; do not use quotes as a substitute for code spans
- Multiline code snippets use fenced code blocks (``` … ```) with an appropriate language tag when applicable (e.g., `python`, `bash`, `markdown`). Use inline code only for single-line fragments
- Unordered lists use -, *, or + markers (GFM/CommonMark); do not use Unicode bullets like •; indent nested items consistently

## Positive examples (proper inline code and links)
```markdown
Run `some-script.sh` with `--dry-run=true`.
The `FooClass` exposes `foo_method(...)` in `src/svc/main.py`.
Logs are archived to `gs://my-bucket/logs/2025-08-27/`.
See <https://example.com/docs/tooling> for details.
````

### Positive examples (multiline code blocks)

```python
def add(a: int, b: int) -> int:
    return a + b
```

```bash
grep -R "pattern" src/ | wc -l
```

### Positive examples (lists)

```markdown
- Parent item
  - Nested item

* Alternate marker

- Another valid marker
```

## Negative examples (plaintext tokens, missing links)

```markdown
Run some-script.sh with --dry-run=true.
The FooClass exposes foo_method(...) in src/svc/main.py.
Logs are archived to gs://my-bucket/logs/2025-08-27/.
See https://example.com/docs/tooling for details.
```

### Dunder/underscore pitfalls

Using bare dunders causes emphasis; protect with code spans.

#### Negative examples (one line each)

```text
my favorite variable is __init__ and constant is __ALL__, edit src/some_module/my__file__.py
```

#### Positive examples (one line each)

```markdown
my favorite variable is `__init__` and constant is `__ALL__`, edit `src/some_module/my__file__.py`
```

````


### Property: minimize-nesting.md

```md
---
title: No unnecessary nesting (combine trivial guards)
kind: outcome
---

Trivial nested guards without else blocks are combined into a single condition; use logical conjunction (and/or) and the walrus operator to bind intermediate values when needed.

## Acceptance criteria (checklist)
- Patterns like `if a: if b:` (with no else between) are flattened to a single `if a and b:`
- Three+ level trivial nests (e.g., `if a: if b: if c:`) are flattened to a single combined condition
- When a nested guard exists only to reuse a freshly computed value, bind inline with `:=` and combine
- Deep nesting is acceptable only when branches have distinct else/elif flows or when readability clearly benefits

## Positive examples
```python
# Two-level flatten
if is_running and (code := proc.returncode) is not None:
    warn_failed(proc_id, code)
````

```python
# Three-level flatten with walrus
if user and user.active and (team := user.team) and team.enabled:
    grant_access(user, team)
```

```python
# Combine trivial guards
if item and item.ready and not item.error:
    process(item)
```

## Negative examples

```python
# Trivial else used to host the main body — should be a guard without else
if not req.auth:
    return unauthorized()
else:
    if not req.body:
        return bad_request()
    else:
        return ok(process(req.body))
```

```python
# Flattenable nested guards with elses — should combine/invert
if user:
    if user.active:
        grant_access(user)
    else:
        return
else:
    return
```

```python
# Trivial two-level nest — should be combined
if is_running:
    if proc.returncode is not None:
        warn_failed(proc_id, proc.returncode)
```

```python
# Trivial three-level nest — should be combined
if user:
    if user.active:
        if user.team:
            if user.team.enabled:
                grant_access(user, user.team)
```

````


### Property: no-dead-code.md

```md
---
title: No dead code (incl. unreachable logic and test‑only prod code)
kind: outcome
---

There should be no dead production code: no unused symbols or unreachable branches live in production directories.
Prod code exercised only in tests is either relocated to explicit test helpers (preferred), or clearly marked as test-only.
If established invariants or type reasoning make a branch impossible, delete it.
Any formally-dead "should never happen" branches only contain an immediate `assert` or `TypeError`.

## Acceptance criteria (checklist)
- Unused symbols (functions, classes, variables, constants) are removed
- Unreachable branches (by invariants/types) are removed; if a "can't happen" guard is desired, keep at most an `assert` or `TypeError`.
- Switches/if‑chains do not include arms for states that cannot occur given the function’s contract
- Mutually exclusive guards and redundant checks are collapsed (no `if a and not a`, `if a: return; ... if a: ...`)
- Code only invoked from tests is clearly marked as such (`MakeTestFooObject`, `test_helpers.py`, ...)
- Feature‑flag or compatibility shims only allowed when actually referenced; stale flags/shims are removed once disabled across environments

## Negative examples

Do not include branches ruled out by type signatures:

```python
def get_user(uid: uuid.UUID) -> User:
    if not uid:
        return None  # impossible given function contract - delete
    return db.load_user(uid)
````

No checks that will always evaluate to `true` / `false` given execution state:

```go
if basePath == "" {
    full := matchesGlob(pattern, path)
    base := matchesGlob(pattern, filepath.Base(path))
    return full || base
}
...
if !validFile || basePath == "" {  // remove basePath check - ruled out above
    return false
}
```

Speculative fallback that cannot happen given upstream checks:

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["fast", "slow"])  # upstream constraint
# ... later in code
if mode not in {"fast", "slow"}:
    # unreachable: CLI parser restricts choices; delete branch
    return default()
```

Default handling in match — wrong: provides behavior for an impossible case:

```python
match mode:
    case "fast":
        run_fast()
    case "slow":
        run_slow()
    case _:
        return default()  # unreachable; delete or turn into a hard guard
```

## Positive examples

Hard guard instead of fallback — acceptable sentinel:

```python
match mode:
    case "fast":
        run_fast()
    case "slow":
        run_slow()
    case _:
        raise AssertionError(f"unreachable mode: {mode!r}")
```

```python
# Or a simple membership assertion
assert mode in {"fast", "slow"}, f"unreachable mode: {mode!r}"
```

```go
// Test-only function living in prod code, but clearly marked as test-only - OK
func MakeTestSession() *Session { ... }
```

```python
# Unused symbol kept around "just in case" — remove
DEFAULT_TIMEOUT_SECONDS = 30  # not referenced anywhere
```

Type‑driven branches remove impossible cases:

```python
def handle(x: Bar | Baz | Quux) -> str:
    if isinstance(x, Bar):
        ...
    elif isinstance(x, Baz):
        ...
    elif isinstance(x, Quux):
        ...
    else:
        # Unreachable; keep at most a hard guard
        raise TypeError(f"Unexpected {type(x) = }")
```

## Exceptions

- Intentional extension points (plugin hooks, abstract interfaces) may appear unused locally but must be referenced by a registry, entry‑points, or configuration.
  Keep a short comment or link to the registry proving reachability:

  ```python
  def plugin_has_no_references_in_python():
      """Plugin, dynamically resolved from configuration YAML."""
      ...
  ```

  ```yaml
  # ~/.config/program/config.yaml
  active_plugins:
    - module_name:plugin_has_no_references_in_python
  ```

- Temporary compatibility shims may remain while a migration is in progress, with an owner and removal date

## Guidance

- Use local reasoning and established invariants. If a branch is obviously unreachable, delete it.
  When unsure, search references, check feature flags/config, and document the invariant you rely on.
- Prefer strengthening invariants and validations over keeping speculative fallback branches

````


### Property: no-extra-linebreaks.md

```md
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
````

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

````


### Property: no-oneoff-vars-and-trivial-wrappers.md

```md
---
title: No one-off variables or trivial pass-through wrappers
kind: outcome
---

Agent-edited code does not introduce single-use "one-off" variables that merely forward into the next call without adding non‑obvious value, and does not add pass‑through functions whose only behavior is to immediately call another function and return its result without a visible reason (e.g., boundary, adaptation, validation).

## Acceptance criteria (checklist)
- Single-use variables that simply forward into the next call are inlined, unless they convey non‑obvious meaning, are reused, or materially improve readability
- Functions that only call another function and return its result are absent, unless they add visible value (e.g., input normalization/validation, signature adaptation, dependency boundary, retries/backoff, structured logging/metrics, deprecation shim) and the reason is evident
- Test helpers/wrappers are acceptable when they encapsulate setup defaults or fixtures; public API adapters are acceptable when they adapt names/types/contracts (documented inline)
- Facade pass-throughs that stabilize an architectural boundary (e.g., App facade methods) are acceptable even if currently thin; include a brief docstring/comment stating the boundary and intent

## Negative examples (violations)

One-off iterator used only to feed collection:

```python
frames_iter = video.iter_frames()
frames = await collect_frames(frames_iter)
````

One-off error object immediately returned:

```python
error = FailureResponse(error="Not found", resource_id=rid)
return error.to_text_content()
```

Trivial pass-through wrapper with identical signature and call:

```python
def foo(a, b, c, d):
    return bar(a, b, c, d)
```

Trivial chain via one-off variables; should be one line:

```python
def probe_cache(namespace=None) -> bool:
    cfg = build_cache_config(namespace)
    client = cfg.make_client()
    return client.ready()
```

## Positive examples (acceptable)

Inline instead of one-off variable:

```python
await http.post_json({
    "type": "render_track",
    "data": [t.model_dump(exclude_none=True) for t in tracks],
})
```

Test helper encapsulates setup defaults (acceptable):

```python
def make_user(name: str = "Rai", email: str = "rai@example.com") -> User:
    return User(name=name, email=email)
```

### Negatives examples, fixed

Inline iterator usage:

```python
frames = await collect_frames(video.iter_frames())
```

Direct return of constructed value:

```python
return FailureResponse(error="Not found", resource_id=rid).to_text_content()
```

One-line chain:

```python
def probe_cache(namespace=None) -> bool:
    return build_engine_spec(snapshot_path).as_runner().ready()
```

````


### Property: no-random-renames.md

```md
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
````

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

````


### Property: no-useless-docs.md

```md
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
````

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

````


### Property: no-useless-tests.md

```md
---
title: No useless tests
kind: outcome
---

Tests must provide distinct value by exercising production behavior or documenting non‑obvious ground truth; redundant or trivially satisfied tests are removed or consolidated.

## Acceptance criteria (checklist)
- No change‑detector tests that merely assert constants, enum values, or literals (e.g., "SomeEnum.VALUE == 'value'") unless the test documents an important, non‑obvious dependency behavior we explicitly rely on (state that rationale inline).
- No tests that only re‑assert trivial, widely‑known properties of standard library or ubiquitous dependencies (e.g., Pydantic BaseModel.model_dump_json() returns str, pathlib.Path.name is a str). Exception: pinning a known upstream regression/workaround — include a clear inline rationale and link to the upstream issue.
- Parameterization or subtests cover representative classes of inputs; avoid enumerating duplicative cases that exercise the exact same behavior/path (e.g., (1,2,3) and (2,1,3) for commutative addition) unless the additional case tests a distinct property.
- Remove assertions that are implied by stronger ones (e.g., avoid `assert x is not None` when `isinstance(x, Foo)` and `x.bar == 100` already guarantee non‑None).
- Do not keep tests fully subsumed by other tests at the same abstraction level; consolidate into one parametrized test or delete the subset test.
- Each test should either:
  - Exercise production code (not just in‑module constants/types), or
  - Demonstrate a non‑obvious, important behavior of a dependency (clearly documented in the test),
  and otherwise be removed.
- Overlap across abstraction levels (e2e vs unit) is acceptable; duplication is only a violation when a test adds no new behavior coverage or rationale at its level.

## Positive examples

Parametrized, representative coverage (no duplicative cases):

```python
import pytest

@pytest.mark.parametrize(
    "a,b,expected",
    [
        (0, 0, 0),          # boundary
        (1, 2, 3),          # representative positive
        (-1, 2, 1),         # sign mix
    ],
)

def test_add_representative(a, b, expected):
    assert add(a, b) == expected
````

Non‑redundant focused assertions:

```python
x = output_of_prod_code()
assert isinstance(x, Foo)
assert x.bar == 100
```

## Negative examples

Change‑detector (no behavior exercised):

```python
def test_enum_value():
    assert SomeEnum.VALUE == "value"
```

Fully subsumed single‑case duplication:

```python
def test_add_8_8():
    assert add(8, 8) == 16  # subsumed by parametrized representative cases
```

Redundant implied assertion:

```python
x = output_of_prod_code()
assert x is not None      # useless: implied by the next two
assert isinstance(x, Foo)
assert x.bar == 100
```

Trivial property of a common dependency API:

```python
# Pydantic's API already guarantees a JSON string here; this adds no value
# Use such a test only to pin an upstream regression and include rationale+link
from hamcrest import assert_that, instance_of

def test_model_dump_json_type_is_string():
    assert_that(user.model_dump_json(), instance_of(str))  # useless
# or
# def test_model_dump_json_type_is_string():
#     assert isinstance(user.model_dump_json(), str)  # useless
```

````


### Property: python/barrel-imports-and-public-api.md

```md
---
title: Barrel imports and public API (__init__/__all__)
kind: outcome
---

Barrel imports and re‑exports exist only to shape a deliberate, versioned public API. Internal modules/packages do not use barrel files; they import from the defining module explicitly. `__init__.py` files are empty (or have a trivial docstring) unless the package is a clearly public library surface, and `__all__` appears only when intentionally declaring that public API.

## Acceptance criteria (checklist)
- Internal code (non‑released, non‑SDK, not a clearly versioned library) MUST import from the module that defines a symbol; no “convenience” barrel imports.
- `__init__.py` in internal packages is empty or contains only a brief docstring/comment; no re‑exports.
- Re‑exports in `__init__.py` are allowed only for a deliberately public, stable API (e.g., SDK, framework, PyPI package):
  - The intent is documented inline (short comment/docstring: “Public API surface for package X”).
  - Re‑exports are selective and explicit (`from .foo import Client, Error`), not wildcard.
  - Private/internal modules/classes are not exported.
- `__all__`:
  - 99%+ of modules SHOULD NOT define `__all__`.
  - Allowed only in a package `__init__.py` that is deliberately curating the public API, with visible indicators (comment and explicit re‑exports) and stable ownership/versioning expectations.
  - Do not use `__all__` in internal modules as a convenience filter.
- Names are consistent and explicit: do not create generic aggregator modules like `shared`/`common` that mask true origins; import from the real module path.

## Positive examples

Explicit import from defining module (internal code):

```python
# aaa/x.py
def foo(): ...

# bbb/y.py
from aaa.x import foo
foo()
````

Deliberate public API in a serious, versioned library:

```python
# pkg/__init__.py
"""Public API surface for pkg (stable).

Exports: Client, Error.
"""
from .client import Client
from .errors import Error

__all__ = ["Client", "Error"]
```

## Negative examples

Internal convenience barrel in `__init__.py`:

```python
# internal_pkg/__init__.py   # ❌ internal package; do not re‑export
from .foo import *
from .bar import helper
```

Ambiguous aggregator import:

```python
from shared import foo   # ❌ where does this come from?
foo()                    # prefer: from real_module.submod import foo
```

Overuse of `__all__` in a normal module:

```python
# module.py  # ❌ not a curated public entrypoint
__all__ = [name for name in globals() if not name.startswith("_")]
```

## Notes

- Public API curation belongs at the package root **init**.py of a real library/SDK with versioning; everywhere else, keep imports explicit to preserve clear dependencies and call sites.
- Re‑exports should be rare, selective, and documented; wildcard exports and convenience barrels hinder traceability and refactoring.
- Related properties: [Imports at the top](./imports-top.md), [Truthfulness](../truthfulness.md) (comments must reflect real intent), [Consistent naming and notation](../consistent-naming-and-notation.md).

````


### Property: python/forbid-dynamic-attrs.md

```md
---
title: Forbid dynamic attribute access and catching AttributeError
kind: outcome
---

Code does not use the `getattr`, `hasattr`, or `setattr` builtins, and does not catch `AttributeError`.
Code assumes variables have specific known types/type-sets (constrained by type annotations, `isinstance` checks, guarantees on return values etc.) and having attributes those types imply.
Code does not treat objects as effectively-`dict[str, Any]`.

## Acceptance criteria (checklist)
- No usage of `getattr`, `hasattr`, or `setattr`; LITERALLY FORBIDDEN whenever direct attribute access would be runtime-equivalent (i.e., the type is known or constrained).
- No `except AttributeError` (including in multi-except or bare except that later filters to AttributeError), and no code paths that swallow missing attributes and continue silently
- Attribute access is type-safe by design (static types or explicit data structures)
- Code does not "guess" attributes by trying multiple names via `getattr`/`hasattr`
- Code that legitimately branches by multiple possible input types uses `isinstance`, `match..case` or other explicit constructs - not `getattr`/`hasattr`/`setattr`.
- Trivial guards do not swallow missing attributes; they fail fast instead of continuing silently

## Positive examples
```python
# Type-driven design: explicit attributes
class User:  # could be a dataclass/attrs/TypedDict as well
    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

u = User("Rai", "rai@example.com")
send(u.email)
````

## Exceptions (rare, deliberate)

- Only when names truly arrive dynamically (e.g., plugin entrypoints specified as "package.module:function"), and only at explicit boundaries; prefer a registry/mapping over attribute probing. If used, keep scope narrow and document why direct access is impossible.
- Never use dynamic attribute probing to guess between multiple names; design types to make invalid states unrepresentable.

## Negative examples

```python
# Dynamic probing — forbidden
if hasattr(obj, "email"):
    send(getattr(obj, "email"))
```

```python
# Dynamic assignment — forbidden
setattr(config, "timeout", 10)
```

```python
# Hiding type errors behind exception catching — forbidden
try:
    return obj.value
except AttributeError:
    return None
```

```python
# Obscuring types and swallowing errors — forbidden
# house: House (pydantic) with attributes: roof: Roof, door: Door, num_windows: int
if hasattr(house, "roof"):
    # Tries to guess attributes and continues even if structure is wrong
    print("house has", getattr(getattr(house, "roof", None), "material", None))
```

```python
# Guessing multiple attribute spellings — forbidden
if hasattr(item, "id") or hasattr(item, "identifier") or hasattr(item, "ID"):
    ident = getattr(item, "id", None) or getattr(item, "identifier", None) or getattr(item, "ID", None)
    use(ident)
```

````


### Property: python/imports-top.md

```md
---
title: Imports at the top
kind: outcome
---

All imports appear at the top of the module (not inside functions/classes); the only exception is a localized import used to break an otherwise unavoidable import cycle and must be documented with an inline comment.

## Acceptance criteria (checklist)
- No `import` or `from ... import ...` statements inside functions, methods, or class bodies
- Module-level imports are grouped at the top (after optional shebang/encoding line and module docstring)
- The only permitted in-function imports are narrowly justified cases and must include an inline comment explaining the reason: breaking an import cycle; dynamic runtime import by string (plugin discovery, `module:function` resolution) or hot-reload; or truly excessive import cost that would unacceptably degrade startup time

- Dynamic imports via `__import__` and `importlib.import_module` follow the same restriction; they are not allowed inside functions unless one of the allowed exceptions applies

## Positive examples
```python
"""Module docstring."""
from __future__ import annotations

import json
from pathlib import Path

def load_config(p: Path) -> dict:
    text = p.read_text()
    return json.loads(text)
````

## Negative examples

```python
def load_config(p):
    import json  # ❌ inline import (not a cycle)
    return json.loads(p.read_text())
```

```python
# ❌ import placed after executable code
print("starting up")
import logging
```

## Exceptions (narrow, justified)

Verified presence of certain listed unusual cases may justify a local import, but only with a verifiable AND accurate inline comment explaining the reason:

- Import cycle: comment must specifically describe the cycle a module-level import would create; prefer refactoring to remove the cycle when feasible.
- Heavy import: the module must be measurably expensive at import time and the localized import must materially reduce startup cost.
- Dynamic plugin/entrypoint or hot-reload: the behavior truly requires runtime import.
  Do not apply an exception if the module is already imported at the top elsewhere, the cost is negligible, or the cycle can be eliminated with a small refactor.

### Import cycle

```python
# file: foo/bar/service.py
def handler():
    # Avoid cyclical import: foo.bar.handler imports foo.baz.model → foo.quux.util → foo.bar.service
    from foo.bar import handler as upstream_handler
    return upstream_handler()
```

### Dynamic plugin or entrypoint import by string

```python
from importlib import import_module

def load_plugin(entrypoint: str):
    module_name, func_name = entrypoint.rsplit(":", 1)
    return getattr(import_module(module_name), func_name)
```

### Hot reload during development

```python
import myapp.config as config
importlib.reload(config)
```

### Deferring a heavy import

```python
def run_gpu_job():
    # Avoid import-time slowdown from compiling kernels (~30 s)
    import gigantic_cuda_lib
    return gigantic_cuda_lib.run()
```

## Additional negative examples

```python
def run_task(name: str):
    mod = __import__(name)  # ❌ dynamic import in function with no justification
    return mod.run()
```

```python
def run_task(name: str):
    mod = import_module(name)  # ❌ no plugin architecture/justification
    return mod.run()
```

### Misleading justification (still a violation)

These examples are additionally also [truthfulness](../truthfulness.md) violations.

```python
# mod_a.py
import os
import math

def fn_a():
    return os.listdir('.'), math.sqrt(2)

# mod_b.py
def compute_now():
    # avoid heavy import at import time
    import mod_a   # ❌ mod_a.py is NOT heavy - misleading - violation
    return mod_a.fn_a()
```

```python
# foo.py
import math
import datetime

from quux import xyzzy

def bar():
    ...

# baz.py
import quux

def fn():
    # local import to avoid cycle
    from foo import bar  # ❌ foo.py does not depend on baz.py - NOT a cycle. misleading - violation
```

### Nonspecific justification (still a violation)

```python
def compute_now():
    # avoid import loop
    import datetime
    return datetime.datetime.now()
```

## Cross-references

- [Truthfulness](../truthfulness.md): misleading "avoid cycle"/"heavy import" comments are untruthful when no cycle/heaviness exists; moving imports into functions can also misrepresent real dependency structure. Keep comments and structure honest about why an exception is taken.

````


### Property: python/modern-python-idioms.md

```md
---
title: Prefer modern Python idioms (operators, types)
kind: outcome
---

Use modern Python 3.11+ idioms that improve clarity and brevity: dict merge operators, set operators, PEP 604 union types, and related conveniences. Prefer these over legacy patterns.

## Acceptance criteria (checklist)
- Dictionaries:
  - Use merge and update operators (PEP 584): `a | b` and `a |= b` (right side wins on key conflicts). Avoid `{**a, **b}` or manual loops for merging.
- Sets:
  - Use operator forms for set algebra: `|` (union), `&` (intersection), `-` (difference), `^` (symmetric difference), and their in‑place variants `|=`, `&=`, `-=`, `^=`. Avoid verbose method chains when simple operators suffice.
- Type hints:
  - Use union types with `|` (PEP 604): `A | B | C` instead of `Union[A, B, C]`.
  - Prefer `Self`/`from __future__ import annotations` patterns as needed (see [Type hints](./type-hints.md)).
- isinstance/issubclass:
  - Continue to use tuples for multiple types: `isinstance(x, (A, B, C))`.
  - Do NOT write `isinstance(x, A | B)` — union types are for annotations, not for runtime checks.
- Strings:
  - Prefer `str.removeprefix/suffix` over slicing for safety and intent (see [String affixes](./str-affixes.md)).
- Pattern matching:
  - Consider `match/case` for simple tag dispatch or structural cases when it improves readability over long `if/elif` chains.

## Positive examples

Dict merge/update (right wins):

```python
cfg = base_cfg | override_cfg
cfg |= env_cfg
````

Set algebra with operators:

```python
missing = required - present
common = a & b
all_tags = a | b | c
```

Union types in annotations:

```python
from typing import Self

def parse(val: int | str) -> int: ...

class X:
    def clone(self) -> Self: ...
```

String affixes:

```python
name = name.removesuffix(".json")
```

## Negative examples

Legacy dict merge patterns:

```python
merged = {**a, **b}              # ❌ prefer a | b
for k, v in b.items():           # ❌ manual merge
    a[k] = v
```

Verbose set API where operators are clearer:

```python
s = a.union(b, c)                # ❌ prefer a | b | c
s = a.intersection(b)            # ❌ prefer a & b
```

Union types misused at runtime:

```python
if isinstance(x, A | B):         # ❌ not supported for isinstance; use a tuple
    ...
```

Old typing style:

```python
from typing import Union

def f(x: Union[int, str]) -> int:  # ❌ prefer int | str
    ...
```

## Notes

- Readability first: prefer these idioms when they clarify intent and reduce noise; if an operator would obscure meaning in a complex expression, a named helper or method call can be acceptable.
- Related properties: [Walrus operator](./walrus.md), [String affixes](./str-affixes.md), [Type hints](./type-hints.md), [Pathlib usage](./pathlib.md).

````


### Property: python/no-mocking-plain-data.md

```md
---
title: Prefer real data/objects over mocks (do not mock plain data)
kind: outcome
---

Do not mock plain data or trivially constructible domain models. Use real objects and real resources (tmp filesystem, real Pydantic models) where practical; reserve mocks/stubs for hard boundaries (network, time, processes) or truly expensive/unavailable dependencies.

## Acceptance criteria (checklist)
- Banned: mocking trivial data containers (e.g., Pydantic models, simple dataclasses, plain dicts) by setting attributes on `Mock/MagicMock`. Construct real instances instead.
- Prefer real filesystem under `tmp_path`/`tmp_path_factory` over broad monkeypatching of `os`/`pathlib` across modules.
- Mock/stub only at external boundaries or costly/unreliable layers (HTTP, DB connections, time, randomness); keep scope narrow and specific.
- Builders/factories provide realistic defaults for domain models; validation must pass.
- Mocks never mask schema/type errors; do not “shape” mocks to look like models to placate type checks.
- Use dependency injection (pass collaborators) so tests can supply small fakes for interfaces; avoid patching internals when a constructor parameter would suffice.

## Positive examples

Construct a real Pydantic model (no MagicMock):

```python
from pydantic import BaseModel, ConfigDict

class User(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str

u = User(id="u_123", email="u@example.com")  # ✅ real, validated instance
````

Real filesystem with tmp_path:

```python
def test_writer(tmp_path: Path):
    out = tmp_path / "data.txt"
    write_data(out, "hello")
    assert out.read_text() == "hello"
```

Boundary‑level mock of HTTP only (payload realistic):

```python
def test_fetch_user(client, requests_mock):
    requests_mock.get("https://api.example.com/users/u_123", json={
        "id": "u_123", "email": "u@example.com"
    })
    assert client.fetch_user("u_123").id == "u_123"
```

## Negative examples

Mocking a Pydantic model (banned):

```python
# ❌ do not do this
user = MagicMock()
user.id = "u_123"
user.email = "x@example.com"
service.handle(user)
```

Mocking core filesystem APIs for an entire module:

```python
# ❌ prefer real tmp_path over patching pathlib/os globally
monkeypatch.setattr("mymod.pathlib.Path", FakePath)
```

Shaping a dict with wrong keys/types instead of constructing real data:

```python
# ❌ incorrect shape; bypasses validation
payload = {"userId": 1, "mail": "x"}
process(payload)
```

## Exceptions (narrow)

- Mocking is acceptable when the object cannot be constructed in tests without heavy external state (e.g., real DB connection, complex binary handles) and when the test specifically targets the interaction contract; keep mocks minimal and focused on the boundary.
- For non‑plain fields that are impractical to instantiate (e.g., embedded OS handles), provide small fakes implementing only the required interface.

## See also

- [Use pytest's standard fixtures for temp dirs and monkeypatching](./pytest-standard-fixtures.md)
- [Use yield fixtures for teardown](./pytest-yield-fixtures.md)
- [Structured data types over untyped mappings](../structured-data-over-untyped-mappings.md)
- [No useless tests](../no-useless-tests.md)

````


### Property: python/no-swallowing-errors.md

```md
---
title: Do not swallow errors (no silent except)
kind: outcome
---

Errors must not be silently ignored. Prefer letting exceptions propagate to a proper boundary; when catching is required, catch specific exceptions, take a concrete action, and/or surface the issue (UI or logs). Blanket catches with no action are banned, including in tests.

## Acceptance criteria (checklist)
- Absolute ban: `except Exception: pass` and any blanket catch that does nothing (including tests) — remove or replace with specific handling.
- Prefer no local handling at all (let it crash) unless there is a clear, domain‑specific recovery or boundary responsibility; this is the correct choice the vast majority of the time (≈90%+).
- When catching, scope the `try` narrowly and catch specific, expected exception types only; do not mask unrelated errors.
- Surfacing requirement: if you ignore an error by design, surface it appropriately — ideally to the user (UI/output) or at least log with context; document the rationale.
- No silent failures during teardown/shutdown; teardown errors must be logged (and re‑raised when appropriate for the boundary).
- Tests must not hide exceptions with broad catches; failures should fail the test unless the test is explicitly asserting the exception with `pytest.raises`.

## Positive examples

Let exceptions propagate (no swallowing):

```python
def load_config(path: Path) -> dict:
    text = path.read_text()            # may raise; OK to bubble up here
    return json.loads(text)            # may raise; OK to bubble up here
````

Catch specific error, log, then re‑raise (or return a safe default when explicitly acceptable):

```python
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    logger.error("Invalid JSON", preview=text[:100])
    raise
```

Specific no‑op with API that encodes the intent (prefer over catching exceptions):

```python
# OK: explicit no‑op on existent dirs
path.mkdir(parents=True, exist_ok=True)
```

Test asserts the expected exception (no swallowing):

```python
with pytest.raises(FileNotFoundError):
    _ = Path("/no/such").read_text()
```

Teardown with guaranteed logging:

```python
@pytest.fixture
def server(tmp_path):
    srv = start_server(tmp_path)
    try:
        yield srv
    finally:
        try:
            srv.shutdown()
        except Exception:
            logger.exception("Server shutdown failed")
            # Boundary decision: re‑raise here if failures must fail tests/jobs
```

## Negative examples

Blanket swallow (always banned):

```python
try:
    do_thing()
except Exception:
    pass  # ❌ silent swallow
```

Teardown swallowing errors silently (logs missing):

```python
try:
    cleanup()
except Exception:  # ❌ do not hide teardown errors
    return
```

Tests masking real failures:

```python
def test_something():
    try:
        run_code()
    except Exception:
        pass  # ❌ hides failures; use pytest.raises for specific exceptions
```

Catching the wrong thing (masks other issues):

```python
try:
    os.remove(p)
except Exception:  # ❌ should catch FileNotFoundError if ignoring that case only
    logger.info("ignored")
```

## Exceptions (narrow)

- Legitimate no‑op outcomes should use APIs that encode the no‑op instead of exceptions (e.g., `mkdir(exist_ok=True)`, `dict.get`, idempotent delete with specific `FileNotFoundError` catch). If you must catch, catch only the specific exception and include a short rationale.
- At true outer boundaries (HTTP handlers, main loops), a broad catch may be used to convert to an error response — must log with full context (`logger.exception`) and avoid continuing in a corrupted state.

## See also

- [Try/except is scoped around the operation it guards](./scoped-try-except.md)

````


### Property: python/pathlib.md

```md
---
title: Use pathlib for path manipulation
kind: outcome
---

Agent-edited Python uses pathlib for filesystem paths and joins; it does not use os.path.* or manual string concatenation for paths.

## Acceptance criteria (checklist)
- Paths are represented as `pathlib.Path` objects
- Path joins use `/` operator or `Path(..., ...)`, not `os.path.join`
- File I/O uses Path methods (`read_text`, `write_text`, `read_bytes`, `open`) instead of bare `open` on string paths
- No manual string concatenation for paths
- Function parameters/returns that represent filesystem paths use `pathlib.Path` (preferred) or `os.PathLike[str]` for interoperability
- CLI arguments that represent filesystem paths are parsed/typed as `pathlib.Path` via argparse (e.g., `parser.add_argument("--out", type=Path)`), not raw `str`

## Positive examples
```python
from pathlib import Path

base = Path(env_root) / "var" / "data"
config = base / "app.cfg"
text = config.read_text(encoding="utf-8")

outdir = Path(tmpdir)
(outdir / "report.json").write_text(payload, encoding="utf-8")

# Function parameters typed as Path (preferred)
import json

def read_config(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))

def write_report(out_dir: Path, name: str) -> Path:
    p = out_dir / f"{name}.json"
    p.write_text("{}", encoding="utf-8")
    return p

# argparse: parse path arguments directly as Path
from argparse import ArgumentParser

parser = ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
args = parser.parse_args([])  # example only

args.config.write_text("ok", encoding="utf-8")
````

## Negative examples

```python
import os

base = os.path.join(env_root, "var", "data")
config = os.path.join(base, "app.cfg")
with open(config, encoding="utf-8") as f:
    text = f.read()

# Manual concatenation — forbidden
logfile = env_root + "/logs/" + name + ".txt"
```

````


### Property: python/pathlike.md

```md
---
title: Pass Path objects to PathLike APIs (no str())
kind: outcome
---

Agent-edited code does not cast `pathlib.Path` (or other PathLike) to `str` when calling APIs that accept path-like objects; it passes the `Path` directly.

## Acceptance criteria (checklist)
- No `str(path)` when the target API accepts `os.PathLike`
- `Path` (or any `os.PathLike`) is passed directly to the API
- This applies across subprocess program/args, filesystem APIs, and archive/logging constructors commonly used in Python 3.8+

## Common APIs that accept PathLike (non-exhaustive)
- subprocess: `Popen`, `run`, `check_call`, `check_output` (program, arguments, and `env` mapping values)
- builtin/open: `open(path)`
- os: `stat`, `listdir`, many file ops via `os.fspath`
- shutil: `copy`, `copyfile`, `rmtree`, etc.
- zipfile/tarfile: `zipfile.ZipFile(path)`, `tarfile.open(path)`
- logging: `logging.FileHandler(path)`

## Positive examples
```python
from pathlib import Path
import subprocess
import logging

cfg = Path("/etc/tool/config.ini")
log = Path("/var/log/tool.log")
subprocess.run([Path("/usr/bin/tool"), cfg], check=True, env={"FOO": Path("/tmp/x")})
fh = logging.FileHandler(log)
````

```python
from pathlib import Path
import shutil

src = Path("data/input.bin")
dst = Path("data/out/input.bin")
shutil.copy(src, dst)
```

```python
from pathlib import Path
import zipfile

archive = Path("build/artifacts.zip")
with zipfile.ZipFile(archive, "w") as zf:
    zf.write(Path("build/report.json"))
```

## Negative examples

```python
# Casting Path to str for subprocess — forbidden
cfg = Path("/etc/tool/config.ini")
subprocess.run(["/usr/bin/tool", str(cfg)])
```

```python
# Casting Path to str for shutil — forbidden
src = Path("data/input.bin"); dst = Path("data/out/input.bin")
shutil.copy(str(src), str(dst))
```

```python
# Casting Path to str for FileHandler — forbidden
log = Path("/var/log/tool.log")
fh = logging.FileHandler(str(log))
```

```python
# Casting Path to str for subprocess env mapping — forbidden
subprocess.run(["/usr/bin/env"], env={"FOO": str(Path("/tmp/x"))})
```

```python
# Legacy API caveat: some stdlib still requires str
# Example: glob.glob requires a string pattern, not Path
import glob
pattern = Path("data") / "*.csv"
files = glob.glob(str(pattern))
```

````


### Property: python/pydantic-2.md

```md
---
title: Target Pydantic 2 (no Pydantic 1 fallback)
kind: outcome
---

Code targets Pydantic v2 APIs only. Do not write dual‑support shims or fallbacks to Pydantic v1; do not import from `pydantic.v1`. Prefer v2 idioms for validation, configuration, and serialization.

## Acceptance criteria (checklist)
- Use Pydantic v2 decorators and APIs (`field_validator`, `model_validator`, `computed_field`, `model_dump`, `model_dump_json`, `model_validate`)
- Configuration uses v2 config objects (`ConfigDict` / `SettingsConfigDict`) via `model_config = ...` (not `class Config:`)
- Settings come from `pydantic_settings.BaseSettings` (not `pydantic.BaseSettings`)
- No compatibility code or fallbacks such as `try/except ImportError` switching between v1/v2 or `from pydantic import v1 as pydantic`
- Do not use v1‑only features (`root_validator`, `validator` with classmethod semantics, `parse_obj`, `json()`/`dict()` in places where v2 equivalents exist)

## Positive examples
```python
# Pydantic v2 model with field and model validators
from pydantic import BaseModel, field_validator, model_validator, ConfigDict

class User(BaseModel):
    model_config = ConfigDict(validate_assignment=True, str_strip_whitespace=True)

    name: str
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("invalid email")
        return v

    @model_validator(mode="after")
    def check_name(self) -> "User":
        if not self.name:
            raise ValueError("name required")
        return self

u = User(name="A", email="a@example.com")
payload: dict = u.model_dump()
json_payload: str = u.model_dump_json()
````

```python
# Settings in v2 using pydantic-settings
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_")

    debug: bool = False
    data_dir: str

settings = AppSettings()
```

```python
# Programmatic validation of external data in v2
from pydantic import BaseModel

class Item(BaseModel):
    id: int
    title: str

raw = {"id": 1, "title": "hello"}
item = Item.model_validate(raw)
```

## Negative examples

```python
# Dual-support shim — forbidden
try:
    # v2 path
    from pydantic import BaseModel, field_validator
except ImportError:  # v1 fallback — DO NOT WRITE
    from pydantic import BaseModel, validator as field_validator
```

```python
# v1-only validators — forbidden when targeting v2
from pydantic import BaseModel, root_validator, validator

class User(BaseModel):
    name: str
    email: str

    @validator("email")
    def validate_email(cls, v):  # v1 style
        ...

    @root_validator
    def check_all(cls, values):  # v1 style
        ...
```

```python
# v1 Config and serialization — forbidden when v2 equivalents exist
from pydantic import BaseModel

class User(BaseModel):
    class Config:  # v1 style — use model_config = ConfigDict(...)
        validate_assignment = True

u = User(name="A")
_ = u.json()      # v1 — use model_dump_json()
_ = u.dict()      # prefer model_dump()
```

```python
# Importing the v1 compatibility module — forbidden
from pydantic import v1 as pydantic  # Do not use v1 compat layer
```

````


### Property: python/pytest-standard-fixtures.md

```md
---
title: Use pytest's standard fixtures for temp dirs and monkeypatching
kind: outcome
---

Pytest-based tests use standard built-ins for temporary paths and patching instead of hand-rolling.
Use `tmp_path` (or `tmp_path_factory` for broader scope), not raw `tempfile`/manual cleanup.
Use `monkeypatch` for environment, cwd and sys path changes.

## Acceptance criteria (checklist)
- Temporary filesystem:
  - Use `tmp_path` for per-test temporary directories; construct paths with `/` and `Path` APIs
  - Use `tmp_path_factory` for module/session-scoped directories when needed
  - Do not use raw `tempfile.mkdtemp/NamedTemporaryFile` unless code under test specifically requires it (document why `tmp_path` cannot be used)
- Process state:
  - Use `monkeypatch.chdir(tmp_path)` to set working directory (no hand-rolled cwd context managers)
  - Use `monkeypatch.setenv/monkeypatch.delenv` for environment variables (no direct `os.environ[...] = ...` in tests)
  - Use `monkeypatch.syspath_prepend` for import path tweaks
- Patching: use `monkeypatch`, `unittest.mock.patch` or other library of choice - **not** hand-rolled patching.
  - `monkeypatch.setattr` or `unittest.mock.patch` for object/function patching — no manual save/restore
  - `monkeypatch.setitem` or `unittest.mock.patch.dict` for mapping patching
- Use `tmp_path`, not legacy `tmpdir` unless testing code strictly requiring `py.path` (document why).

## Forbidden
- Hand-rolled cwd managers/context managers; use `monkeypatch.chdir(tmp_path)`
- Home-grown temp dir helpers or manual `tempfile` + cleanup; use `tmp_path`/`tmp_path_factory` or document why that would not work
- Home-rolled env mutation; use `monkeypatch.setenv` or `unittest.mock.patch.dict`

## Positive examples

```python
# tmp_path for per-test temp dirs
def test_writes_file(tmp_path: Path):
    out = tmp_path / "data.txt"
    out.write_text("hello")
    assert out.read_text() == "hello"

# tmp_path_factory for broader scope
@pytest.fixture(scope="module")
def module_tmp(module_tmp_path_factory):
    p = module_tmp_path_factory.mktemp("dataset")
    ...

# monkeypatch for cwd and env
def test_runs_in_temp_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_MODE", "test")
    run_main()
    assert (tmp_path / "output.log").exists()

# monkeypatch.setattr without manual save/restore
def test_disables_network(monkeypatch):
    monkeypatch.setattr("mymodule.http_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    with pytest.raises(RuntimeError):
        mymodule.fetch()
````

## Negative examples

Hand-rolled cwd manager — do not do this, use `monkeypatch.chdir`:

```python
class Cwd:
    def __init__(self, path):
        self.path = path
        self.prev = None
    def __enter__(self):
        self.prev = os.getcwd(); os.chdir(self.path)
    def __exit__(self, *_):
        os.chdir(self.prev)

def test_runs_in_temp_dir(...):
    with Cwd(tempfile.mkdtemp()):
        ...
```

Direct env mutation (with incorrect cleanup) where `monkeypatch` would work:

```python
def test_runs_in_temp_dir(...):
    os.environ["APP_MODE"] = "test"
    run_main()
    del os.environ["APP_MODE"]
```

Do not use hand-rolled tempfiles where `tmp_path` would work:

```python
def test_writes_file():
    root = Path(tempfile.mkdtemp())
    try:
        ...
    finally:
        shutil.rmtree(root)
```

## Exceptions

- When testing code that explicitly consumes `py.path` objects, `tmpdir` can be used.
  Prefer migrating the code under test to `pathlib.Path` and `tmp_path` when feasible.
- If third-party API requires raw `tempfile` handles (e.g., needs a real OS-level fd), document the reason and keep the scope minimal

## See also

- [PathLike (Python)](./pathlike.md)
- [Pathlib usage (Python)](./pathlib.md)

````


### Property: python/pytest-yield-fixtures.md

```md
---
title: Use yield fixtures for teardown
kind: outcome
---

Resources that require teardown should be provided via pytest yield fixtures; this is REQUIRED once the same setup/teardown appears in more than one test, otherwise recommended. Teardown must run in a `finally` block guarding the `yield`.

## Acceptance criteria (checklist)
- Yield fixtures are used for any resource needing teardown when the pattern is used in 2+ tests; single-use may inline cleanup, but prefer a yield fixture for clarity/reuse.
- Teardown lives in a `finally:` after the `yield`, ensuring cleanup on errors/failures and partial setups.
- No duplicated setup/teardown code across tests; factor into a fixture instead of copy/paste try/finally blocks.
- Prefer yield fixtures over `request.addfinalizer` for readability; use `addfinalizer` only when teardown must be registered conditionally or multiple independent cleanups are required.
- Fixture scope is chosen intentionally (function/module/session) and matches the resource lifetime; teardown corresponds to that scope.

## Positive examples

Basic resource with guaranteed cleanup:

```python
import pytest

@pytest.fixture
def temp_db(tmp_path):
    db = start_db(tmp_path)
    try:
        yield db
    finally:
        db.stop()
````

Parametrized fixture with per-case cleanup:

```python
@pytest.fixture(params=["v1", "v2"])
def api_server(tmp_path, request):
    srv = start_server(version=request.param, root=tmp_path)
    try:
        yield srv
    finally:
        srv.shutdown()
```

Using yield instead of duplicating cleanup in tests:

```python
# Good: fixture owns lifecycle

def test_reads(api_server):
    assert api_server.health() == "ok"

# (instead of repeating start/stop in each test)
```

## Negative examples

Duplicated try/finally across tests (factor into a fixture):

```python
# ❌ repeated in multiple tests
root = mktemp(); srv = start_server(root)
try:
    ...
finally:
    srv.shutdown(); rmtree(root)
```

Missing finally (cleanup skipped on failure):

```python
# ❌ teardown would be skipped if assertions fail
@pytest.fixture
def srv(tmp_path):
    srv = start_server(tmp_path)
    yield srv
    srv.shutdown()  # not guarded; prefer try/finally
```

## See also

- [Use pytest's standard fixtures for temp dirs and monkeypatching](./pytest-standard-fixtures.md)

````


### Property: python/scoped-try-except.md

```md
---
title: Try/except is scoped around the operation it guards
kind: outcome
---

Try/except blocks are short and localized: the try encloses only the minimal risky operation, with the except immediately following it. Treat exceptions as normal control flow guards (like `if`), not as wrappers for large bodies. Only top‑level error boundaries may use broad, larger try/except blocks with clear justification and logging.

## Acceptance criteria (checklist)
- The `try` block encloses the minimal risky expression(s) (typically 1–3 lines); avoid wrapping long blocks of unrelated work
- Prefer `try/except/else` to keep the main logic outside the `try` when helpful
- Use specific, expected exception types (e.g., `json.JSONDecodeError`), not blanket catches, except at top‑level boundaries
- Separate independent risky operations into separate `try/except` blocks rather than one large wrapper
- Exception (allowed): At true outer boundaries (HTTP handler, main loop, task runner), a broader `try/except` may be used with a comment explaining the boundary and with full logging

## Positive examples

`try` encloses only the risky JSON parse:

```python
async def _process_buffer_lines(self, buffer: str) -> None:
    lines = buffer.split("\n")
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(
                "Malformed record",
                preview=line[:100],
                error=str(e),
            )
            await self._attempt_recovery(line)
            continue  # Early bailout
        # Normal path after successful parse
        if (event_id := payload.get("id")) in self._callbacks:
            callback = self._callbacks[event_id]
            callback(payload)
        else:
            logger.debug(
                "Received event",
                kind=payload.get("type"),
            )
````

Distinct `try`/`except` blocks for separate risky operations:

```python
from pathlib import Path

def read_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Config is not valid JSON", path=str(path), error=str(e))
        raise
```

### Error-boundary example (allowed)

#### HTTP request handler

```python
def handle_request(req) -> Response:
    try:
        data = get_bytes()
        obj = json.loads(data)
        return make_response(obj)
    except Exception:
        logger.exception("Unhandled error in request handler")
        return Response(status=500)
```

#### Top-level CLI command handler

```python
def cmd_sync(args: argparse.Namespace) -> int:
    try:
        repo = Repo.open(args.path)
        run_sync(repo, force=args.force)
        return 0
    except Exception:
        logger.exception("sync failed")
        return 2
```

These broad catches are acceptable only at the handler/command boundary, not in helpers.

## Negative examples

Too-wide try/except wraps long body — should scope around `json.loads` and early-bailout:

```python
async def _process_records(self, text: str) -> None:
    lines = text.split("\n")
    for record in lines[:-1]:
        record = record.strip()
        if not record:
            continue

        try:
            # Risky operation mixed with unrelated logic (too large try)
            obj = json.loads(record)
            logger.debug("Parsed object", id=obj.get("id"))
            if (eid := obj.get("id")) in self._callbacks:
                cb = self._callbacks[eid]
                cb(obj)
            else:
                logger.debug("Unrouted event", kind=obj.get("type"))
        except json.JSONDecodeError as e:
            logger.warning("Malformed record", preview=record[:100], error=str(e))
            await self._attempt_recovery(record)
```

One big `try` for unrelated risks in non-boundary code — should split into separate localized `try`/`except` blocks:

```python
def internal_business_logic():
    try:
        blob = get_bytes()      # may raise TimeoutError
        obj = json.loads(blob)  # may raise JSONDecodeError
        write(obj)              # may raise OSError
    except Exception:           # wrong here: not at a boundary and hides errors
        on_error()
```

Top-level else wrapping long body — exceptions used as a giant wrapper:

```python
try:
    verify()
except InvalidState:
    return failure()
else:
    # 20+ lines of main logic here — should move the main logic out, keep try minimal
    return build_result()
```

````


### Property: python/str-affixes.md

```md
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
````

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

````


### Property: python/strenum.md

```md
---
title: Use StrEnum for string‑valued enums (Python)
kind: outcome
---

Python enums with string values are declared with `enum.StrEnum` (Python 3.11+) rather than `class X(str, Enum)` or plain `Enum` with string literals.

Rationale: StrEnum members are both strings and enums, so they interoperate with APIs/serialization/JSON/DB that expect `str` without leaking `.value` into calling code, while still enforcing a closed set of allowed values.

## Acceptance criteria (checklist)
- String‑valued enums subclass `enum.StrEnum`
- Do not declare string enums as `class X(str, Enum)`
- Do not use plain `Enum` with string literal members when a string enum is intended
- Targeting older Python (<3.11) is an acceptable exception only when positively identified as the target

## Positive examples
```python
from enum import StrEnum

class ErrorCode(StrEnum):
    PROCESS_DIED = "process_died"
    COMMUNICATION_FAILURE = "communication_failure"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    STARTUP_FAILURE = "startup_failure"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    UNKNOWN = "unknown"
````

## Negative examples

```python
# Old style — forbidden when targeting 3.11+
from enum import Enum

class ErrorCode(str, Enum):
    PROCESS_DIED = "process_died"
    COMMUNICATION_FAILURE = "communication_failure"
```

```python
# Plain Enum with string values — ambiguous intent
from enum import Enum

class ErrorCode(Enum):
    PROCESS_DIED = "process_died"
    COMMUNICATION_FAILURE = "communication_failure"
```

````


### Property: python/type-hints.md

```md
---
title: Modern type hints (PEP 604 unions, builtin generics)
kind: outcome
---

Agent-edited Python uses modern typing: builtin generics (e.g., `list[int]`) and PEP 604 unions (`A | B`), not legacy `typing.List`, `typing.Dict`, `typing.Union`, or `typing.Optional`.

## Acceptance criteria (checklist)
- Builtin generics are used: `list[T]`, `dict[K, V]`, `set[T]`, `tuple[T, ...]`
- Unions use `A | B` and optional uses `T | None`
- No `typing.Union`, `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Set`, `typing.Tuple` in edited hunks
- Forward references do not use string type names when `from __future__ import annotations` can be used
- It is acceptable to target an older Python that lacks these features only when positively identified as the target

## Positive examples
```python
from __future__ import annotations
from collections.abc import Iterable

def names(items: Iterable[str] | None) -> list[str]:
    return [x for x in (items or [])]
````

```python
UserRecord = dict[str, str | int]
ids: set[int] = {1, 2, 3}
```

## Negative examples

```python
from typing import List, Dict, Optional, Union

def names(items: Optional[List[str]]) -> List[str]:
    return [x for x in (items or [])]
```

```python
from __future__ import annotations

class Node:
    def child(self) -> "Node":  # ❌ string forward ref; use future annotations instead of strings
        ...
```

````


### Property: python/walrus.md

```md
---
title: Use walrus for trivial immediate conditions
kind: outcome
---


When a simple condition depends on a value computed immediately before, the value is bound inline with the walrus operator (:=) inside the condition.

## Acceptance criteria (checklist)
- Patterns like `if x`, `if not x`, `if x is None`, `if x is not None`, or `if x == <literal>` that depend on a freshly computed value use `:=` to bind inline
- The bound expression is the immediately evaluated value (e.g., a function call or awaitable)
- Do not create a separate one‑off variable assignment solely to feed the next `if` when `:=` would be equivalent and readable
- Do not force a walrus when the condition can be written directly without a temporary and the value is not reused (e.g., prefer `if server_process.poll() is not None:` over `if (_ := server_process.poll()) is not None:`)
- Only enforce when the walrus form remains a single, readable line after formatting

## Positive examples
```python
# DB lookup: bind inline for a trivial guard
if not (user := db.get(User, user_id)):
    return FailureResponse(error="User not found").to_text_content()

detail = DetailResponse(
    user_id=user_id,
    name=user.name,
    groups=[g.name for g in user.groups],
)
return detail.to_text_content()
````

```python
# Synchronous
if (item := cache.get(key)) is not None:
    return item
```

```python
# Equality to simple literal
if (code := compute_status()) == 1:
    handle_ok()
```

## Negative examples

```python
# One-off assignment only to feed the next if — should use walrus
user = db.get(User, user_id)
if not user:
    return FailureResponse(error="User not found").to_text_content()
```

```python
# Redundant two-step when a single `if (x := ...) is not None:` suffices
result = maybe_get()
if result is not None:
    use(result)
```

## Clarifications

- Apply this rule only to collapse a redundant two-step "assign, then immediately check" into a single `if` with `:=` when it improves clarity.
- Do not introduce throwaway bindings (e.g., `_ := ...`) just to satisfy the rule; either bind to a meaningful name you reuse, or write the condition directly.

## Dict error checks

### Positive examples

```python
# Use walrus to bind dict error payload inline
if error := resp.get("error"):
    raise ApiError(f"Server error: {error.get('message', 'unknown')}")
```

### Negative examples

```python
# Two-step then check — should use walrus
if "error" in resp:
    error = resp["error"]
    raise ApiError(f"Server error: {error.get('message', 'unknown')}")
```

## While reader loops

### Positive examples

```python
# File-like object
while chunk := f.read(8192):
    process(chunk)

# Async stream
while (line := await stream.readline()):
    handle(line)
```

### Negative examples

```python
# Two-step read loop instead of walrus
chunk = f.read(8192)
while chunk:
    process(chunk)
    chunk = f.read(8192)

# Async version
line = await stream.readline()
while line:
    handle(line)
    line = await stream.readline()
```

````


### Property: self-describing-names.md

```md
---
title: Self‑describing names for primitives (units and meaning)
kind: outcome
---

Primitive‑typed identifiers (int/float/str/bool/bytes/number) are named so their exact meaning and units are unambiguous from name + type + immediate context; when an appropriate domain type exists (e.g., duration/time), use it instead of an ambiguous primitive.

## Acceptance criteria (checklist)
- Durations: use a duration type (e.g., Python datetime.timedelta, Go time.Duration, Java java.time.Duration) OR suffix the unit on primitives (e.g., timeout_ms, poll_interval_secs)
- Timestamps: use time types (datetime/Instant) instead of numeric epochs; if a primitive is required, suffix unit explicitly (created_at_epoch_ms or created_at_epoch_s)
- Sizes: suffix byte‑based units on primitives (payload_bytes, chunk_size_kb) rather than ambiguous names (chunk_size)
- Ratios/percentages: name includes the scale (progress_percent, error_ratio)
- Booleans: clear state/predicate names. Past-participle adjectives are fine (enabled, accepted, archived, verified) and read as state; use is_/has_ when a bare noun would be ambiguous (is_admin, has_license). Avoid bare nouns like license/admin/feature.
- IDs: include entity in name when type is a generic string/number (user_id, order_id) rather than id in ambiguous scopes
- Do not introduce bare primitives whose meaning/units are unclear from name; rename to make meaning obvious or use a richer type

## Positive examples
```python
# Python
from datetime import timedelta, datetime
TIMEOUT: timedelta = timedelta(milliseconds=250)           # best
retry_delay_ms: int = 250                                  # clear primitive
chunk_size_bytes: int = 65536
created_at: datetime = datetime.now()
progress_percent: int = 85
is_enabled: bool = True
accepted: bool = True
archived: bool = False
verified: bool = True
licensed: bool = True
is_admin: bool = False
has_license: bool = True
user_id: str = "u_123"
````

```ts
// TypeScript
const timeoutMs: number = 250;
const payloadBytes: number = 1024;
const successRatio: number = 0.97; // ratio in [0,1]
const isActive: boolean = true;
```

```go
// Go
var Timeout time.Duration = 250 * time.Millisecond
var ChunkSizeBytes int = 64 * 1024
var IsAdmin bool = false
```

```java
// Java
Duration timeout = Duration.ofMillis(250);
int payloadBytes = 1024;
boolean isEnabled = true;
```

## Negative examples

```python
# Ambiguous units / meaning
TIMEOUT: int = 250                     # bad: unit unknown
retry_delay: int = 250                 # bad: unit unknown
chunk_size: int = 65536                # bad: items? bytes?
timestamp: int = 1712345678            # bad: epoch? seconds? ms?
progress: float = 0.85                 # bad: ratio or percent?
id: str = "123"                        # bad: which entity?
```

```ts
// TypeScript
let timeout: number = 250; // bad
let size: number = 1024; // bad
let license: boolean = true; // bad (bare noun)
let admin: boolean = true; // bad (bare noun)
let feature: boolean = true; // bad (bare noun)
```

## Notes

- Prefer domain types where available (timedelta/Duration/Instant/etc.). When primitives are unavoidable, encode units in the name.
- Booleans: past-participle adjectives are often fine because they read as a state (enabled, accepted, archived, verified). Use is*/has* when a noun would otherwise be ambiguous (is_admin, has_license).
- Pragmatic exception in legacy codebases: if a code path is uniformly using weak types (e.g., string paths or epoch integers) and your small change would only introduce noise by converting in/out without internal benefit, it’s acceptable to stick to the prevailing type for that narrow change. Favor module/function boundaries that convert once at input and once at output when you can extract real benefits internally.
- This property focuses on unambiguous naming for primitives. Additional properties may separately enforce: use of time/money types; currency units; angle units (deg/rad); and rate units (per_second, per_minute).

````


### Property: structured-data-over-untyped-mappings.md

```md
---
title: Structured data types over untyped mappings
kind: outcome
---

Code uses structured, typed data models for domain payloads and API surfaces rather than ad‑hoc "bag‑of‑whatever" maps.
Avoid `dict[str, Any]`/`Mapping[str, Any]`, `Record<string, unknown>`, `map[string]any`, etc. for application data; prefer
Pydantic models, dataclasses + TypedDicts, TS interfaces/types, Go structs, Java records/POJOs, with proper (de)serialization.

## Acceptance criteria (checklist)
- No new function parameters/returns are untyped or loosely typed maps for domain data (e.g., `dict[str, Any]`, `Mapping[str, Any]`, `Record<string, unknown>`, `map[string]any`)
- Enumerations: when a field has one of N possible options, use a proper enum — not a bare primitive
  - Python: `enum.StrEnum` (3.11+) for string‑valued enums; plain `Enum` for non‑string values. See [Use StrEnum for string‑valued enums](python/strenum.md)
  - TypeScript: string literal unions (preferred) or `enum` with a runtime schema (e.g., zod) for external input
  - Go: define a named type with `const ( ... iota )` values; add `MarshalJSON/UnmarshalJSON` when serializing
  - Java: `enum` for closed sets
- Define concrete schemas for domain payloads:
  - Python: `pydantic.BaseModel` (preferred) or `TypedDict` for simple shapes; dataclasses when value semantics are desired (add explicit serde at boundaries)
  - TypeScript: `interface`/`type` with a runtime schema (zod/io‑ts) when validating external input
  - Go: `struct` with `json` tags
  - Java: records/POJOs with Jackson/Moshi/Gson annotations as needed
- Validation happens at boundaries: parse external JSON into the structured type (e.g., `Model.model_validate(data)`, `z.parse(data)`, `json.Unmarshal(...)`, `ObjectMapper.readValue(...)`)
- Serialization uses library methods (`model_dump(_json)`, `JSON.stringify(value)`, `json.Marshal`, `ObjectMapper.writeValueAsString`) — do not hand‑assemble nested maps
- Temporary map‑like collections are acceptable for inherently map‑shaped data (e.g., HTTP headers/query params, logging contexts); document invariants and normalize to a model ASAP if they cross module boundaries
- Prefer precise fields over opaque blobs; avoid passing through arbitrary `extra` unless explicitly modeled and justified
- Related: keep types precise and explicit; see [type correctness and specificity](./type-correctness-and-specificity.md) and [forbid dynamic attribute access](python/forbid-dynamic-attrs.md); Python should also [target Pydantic 2](python/pydantic-2.md)

## Positive examples

Python (Pydantic v2 model + StrEnum):
```python
from enum import StrEnum
from pydantic import BaseModel, ConfigDict

class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"

class User(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    email: str
    role: Role = Role.USER

def parse_user(raw: dict) -> User:
    return User.model_validate(raw)

# Serialize for transport
payload: dict = User(id="u1", email="u@example.com", role=Role.ADMIN).model_dump()
````

Python (TypedDict for small, static shapes):

```python
from typing import TypedDict

class Health(TypedDict):
    status: str
    uptime_secs: int

def get_health() -> Health:
    return {"status": "ok", "uptime_secs": 12}
```

TypeScript (interface + literal union + runtime check):

```ts
import { z } from "zod";

type Role = "admin" | "user"; // closed set
export interface User {
  id: string;
  email: string;
  role: Role;
}
export const UserSchema = z.object({
  id: z.string(),
  email: z.string().email(),
  role: z.enum(["admin", "user"]),
});

const user: User = UserSchema.parse(JSON.parse(input));
```

Go (struct + typed enum‑like):

```go
type Role string
const (
    RoleAdmin Role = "admin"
    RoleUser  Role = "user"
)

type User struct {
    ID    string `json:"id"`
    Email string `json:"email"`
    Role  Role   `json:"role"`
}

var u User
_ = json.Unmarshal(data, &u)
```

Java (record + enum):

```java
public enum Role { ADMIN, USER }
public record User(String id, String email, Role role) {}
```

## Negative examples (violations)

Opaque dict returned from core logic:

```python
def load_user() -> dict[str, Any]:  # too loose
    return {"id": uid, "mail": email}  # inconsistent, unvalidated keys
```

Ad‑hoc nested map assembly for transport:

```python
payload = {
    "user": {"id": user.id, "email": user.email},
    "meta": extras,  # bag‑of‑whatever
}
# prefer: payload = Envelope(user=user).model_dump()
```

Using primitives for a closed set (should be an enum):

```python
role: str = "admin"  # should be Role (StrEnum)
```

TypeScript domain shape as Record (no schema):

```ts
function makeUser(): Record<string, unknown> {
  // too loose
  return { id: "u1", email: "u@example.com", role: "admin" };
}
```

Go passing dynamic bags through modules:

```go
func Handle(m map[string]any) error {  // too loose
    // callers and callees disagree on keys/types
    return nil
}
```

Notes

- Use map‑like types only for inherently key/value domains (headers, labels), short‑lived and close to their origin
- When introducing a model on an existing loose interface, convert once at the boundary; avoid churn by bouncing between loose and strict forms inside the same flow

````


### Property: truthfulness.md

```md
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
````

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

````


### Property: type-correctness-and-specificity.md

```md
---
title: Type correctness and specificity
kind: behavior
---

Code is correctly and precisely typed.
Declared types reflect the actual values passed, returned, and assigned.
Types are neither too narrow nor too wide.
Unions reflect real control‑flow possibilities.
Prefer concrete types over `Any`.
Factor complex type expressions into named aliases.

## Acceptance criteria
- All assigned, returned, and passed values are members of their declared types. Static checks pass under chosen type checker where applicable.
- Types are not too narrow: no value violates its declared type at runtime or under static analysis.
- Types are not too wide: if a declaration uses a union (e.g., `A | B`), there must be a live, reachable code path producing/accepting each variant. Remove dead variants.
- Avoid `Any` when a reasonably precise type is feasible (e.g., `Callable[[X], Y] | Z`, concrete container element types, `Protocol`, `TypedDict` or generics).
- When interfaces use complex unions/algebraic types, use a type alias to keep signatures readable and DRY.
- Optionality is explicit: use `T | None` if and only if `None` is a real, reachable case.
- Avoid silencing type errors with blanket casts/ignores; fix the underlying types or add precise, documented narrows.

## Positive examples

Python (progressive typing):

```python
from collections.abc import Callable, Generator
from typing import TypedDict, Protocol, TypeAlias, Any

class Fetcher(Protocol):
    def __call__(self, url: str, timeout: float) -> bytes: ...

class Item(TypedDict):
    id: str
    size_bytes: int

def parse_items(raw: bytes) -> list[Item]:  # precise element type
    ...

# Concrete callable type instead of Any
def get_data(fetch: Fetcher) -> bytes:
    return fetch("/api", 2.5)

# Union with real paths for both variants
def load(kind: str) -> bytes | str:
    if kind == "bin":
        return b"\x00\x01"
    return "text"
````

Type alias removes repetition and clarifies meaning:

```python
ComplexInterfaceType: TypeAlias = (
    Callable[[int, str], bytes]
    | Generator[int, None, str]
)

def handle(handler: ComplexInterfaceType) -> str:
    if result := next(handler(1, "x"), None):
        return str(result)
    return "done"
```

## Negative examples

Too wide union (declares `int | str` but never returns `str` - should declare `-> int`):

```python
def f(flag: bool) -> int | str:
    return 1
```

Overuse of `Any` / loose type - should be `Callable[[int], str]` or similar:

```python
def run(cb: Any) -> Any:
    return cb(123)

def maybe() -> int | None:  # not actually optional
    return 3
```

Too narrow type (mismatched element types):

```python
users: list[str] = ["u1", 2]  # 2 is not str
```

Unnamed complex complex repeated in multiple places:

```python
from collections.abc import Callable, Generator

def g(h: Callable[[int, str], bytes] | Generator[int, None, str]) -> str:
    ...  # prefer to TypeAlias and DRY
```

```

```
