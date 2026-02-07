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
```

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
