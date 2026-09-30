# Credit System follow-ups

Open work is limited to time milestones, break-time accrual and settlement, and
the remaining UI for those features. The current credit contract and
streak/daily-bonus behavior live in <../README.md> and the server code.

## Time milestones

### Definition

During a continuous study+break period, the user earns bonus credits at
each hour boundary:

| Hour boundary | Bonus credits |
| ------------- | ------------- |
| 1st hour      | +5            |
| 2nd hour      | +10           |
| 3rd hour      | +15           |
| 4th hour      | +20           |
| 5th+ hour     | +20 each      |

```
MILESTONE_BONUSES = [5, 10, 15, 20, 20, 20, ...]
```

The bonus is awarded at the **end** of each hour, not the beginning.
"Continuous" means the session hasn't been stopped — pausing doesn't
break continuity (same as current pause behavior). The break-time follow-up below
also counts toward the milestone clock.

### When awarded

On `session.complete`, the server computes the total continuous
study+break seconds and awards all milestone bonuses that fall within
that duration. For a 2.5-hour session:

- Hour 1 completed: +5 credits
- Hour 2 completed: +10 credits
- Hour 3 not completed (only 30 min in): no bonus

Each milestone bonus is also multiplied by the streak multiplier.

### Frontend

During an active session, show the next milestone:
"Next milestone: +10 cr in 23 min"

The live timer already ticks; add a secondary countdown.

---

## Constants to add

`study_casino/credit_constants.py`:

```python
from decimal import Decimal

# Milestones: index 0 is the bonus after hour 1, index 1 after hour 2, etc.
MILESTONE_BONUSES = [5, 10, 15, 20, 20, 20, 20, 20, 20, 20, 20, 20]

# Break time
BREAK_ACCRUAL_RATE = 3  # minutes studied per minute of break
BREAK_CREDIT_RATE = Decimal("1")  # credits per minute of break
```

## Award behavior to implement

- Award each elapsed study milestone once, preserving progress across sessions
  during an earned break and applying the existing streak multiplier.
- Accrue break time at the configured study-to-break rate. Settle elapsed break
  credits lazily on server requests, using the multiplier captured when the break
  began. Starting a new session clears any remaining break time.

## Database changes

Extend the existing per-user `credit_state` row with only the fields needed for
these open features:

- `milestone_accumulated_seconds` for progress toward the next milestone.
- Break start time, accrued duration, settled credits, and the multiplier captured
  at break start.

The current streak and daily-bonus fields already belong to the schema documented
in <../README.md>.

## UI follow-ups

- Show progress to the next milestone during a study session.
- Provide a break view with remaining time, break-credit rate, and a way to skip
  the break and start studying.
- Add milestone and break-credit totals to the stats view.

## Acceptance checks

- Milestones award at exact boundaries and carry progress across eligible
  sessions; each milestone is awarded once and uses the existing streak multiplier.
- Break accrual rounds according to the configured rate; lazy settlement is
  idempotent across requests and stops when the break ends.
- Starting another session interrupts a break and drops unused time.
- UI progress, break state, and totals match the server-provided values.

## Implementation order

1. **Time milestones:** add accumulated-time state, calculate and award milestone
   bonuses on session completion, and cover boundaries and carry-forward.
2. **Break time:** add break state, accrue time on session completion, settle earned
   credits lazily, lock the streak multiplier at break start, and test interruption.
3. **UI:** add milestone progress, break mode, and milestone/break totals.
