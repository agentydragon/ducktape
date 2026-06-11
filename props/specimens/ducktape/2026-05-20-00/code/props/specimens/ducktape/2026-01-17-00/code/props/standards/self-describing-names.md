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
```

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
let timeout: number = 250;             // bad
let size: number = 1024;               // bad
let license: boolean = true;           // bad (bare noun)
let admin: boolean = true;             // bad (bare noun)
let feature: boolean = true;           // bad (bare noun)
```

## Notes

- Prefer domain types where available (timedelta/Duration/Instant/etc.). When primitives are unavoidable, encode units in the name.
- Booleans: past-participle adjectives are often fine because they read as a state (enabled, accepted, archived, verified). Use is_/has_ when a noun would otherwise be ambiguous (is_admin, has_license).
- Pragmatic exception in legacy codebases: if a code path is uniformly using weak types (e.g., string paths or epoch integers) and your small change would only introduce noise by converting in/out without internal benefit, it’s acceptable to stick to the prevailing type for that narrow change. Favor module/function boundaries that convert once at input and once at output when you can extract real benefits internally.
- This property focuses on unambiguous naming for primitives. Additional properties may separately enforce: use of time/money types; currency units; angle units (deg/rad); and rate units (per_second, per_minute).
