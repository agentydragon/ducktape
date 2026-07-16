# Credit System v2 — Design Document

Status: **phases 1–2 implemented** (milli-credit integer accounting via
`credit_award.py` + `credit_constants.py`, streak/daily-bonus/rest-day state in
`credit_state`); phases 3–5 (milestones, break time, richer UI) remain future
work. Implementation deviation: amounts are integer **millicredits** (×1000),
not cents (×100), and stay integers on the wire (`*_millis` fields,
`streak_bonus_percent`) — no floats anywhere. Anchored on the current
server-authoritative Postgres model: credit is computed and persisted
server-side on each `/actions/*` mutation (recorded in `ledger_events`,
surfaced read-only through `/state`); see <../README.md>. The mechanics
below are additive — a per-user `credit_state` row plus server computation
on `session.complete`. The frontend only displays derived values; it never
computes credits.

## Overview

The current credit system is simple: 1 minute studied = 1 credit (integer),
awarded on session completion. This plan introduces six interconnected
mechanics that layer on top of that base:

1. **Decimal accounting** — fractional credits to avoid rounding loss
2. **Streak across days** — daily-study multiplier that grows and resets
3. **Rest days** — streak preservation from earned rest days
4. **Time milestones** — hourly bonuses during study sessions
5. **First-5-minutes bonus** — daily kick-start credit
6. **Break time** — earned breaks that continue credit accrual

All constants live in one server-side Python module (`x/study_casino/credit_constants.py`)
so they can be tweaked without touching business logic. Derived values
(streak, multiplier, next milestone, bonuses) are surfaced to the frontend
read-only via `/state`; the frontend only displays them.

---

## Design Decisions

### Append-only streak and bonus

Streak qualifications, daily bonus awards, and rest day usage are
**append-only** — once a decision is recorded for a day, it is never
revoked or recalculated, even if sessions are later edited or deleted.
Credits themselves are still adjusted (debited) when a session is
shortened or deleted, but streak/bonus state does not change
retroactively.

**Why**: Retroactive recalculation would cascade: editing a session
shorter could invalidate that day's streak qualification, which could
change the multiplier for every subsequent day, requiring credit
adjustments on every session after it. For a single-user app this
complexity is unjustified.

### Past sessions don't qualify for streak or daily bonus

Only **live sessions** (completed in real-time via `session.complete`)
count toward streak qualification and daily bonus. Past sessions added
via `session.add_past` earn credits but do not affect streak state.

### Session edit/delete in the ledger

Session edits and deletes are server actions like any other and go into
`ledger_events` with `action_type="session_edit"` or
`action_type="session_delete"`. The row records the credit delta
(`credits_before`, `credits_after`) and `details_json` captures what
changed (old/new seconds, session ID). Streak and bonus state are not
touched.

### Millis-as-integer for all DB columns and the wire

All credit amounts are stored and transmitted as integers representing
millicredits (value × 1000). Server code uses `Decimal` internally and rounds
to whole millis on write; the frontend divides by 1000 only for display. No
`Float` columns and no floats on the wire — avoids IEEE 754 drift and float
equality traps.

### 100-day ramp to 2x

`DAILY_STREAK_INCREMENT = 0.01` and `STREAK_MULTIPLIER_CAP = 1.0`
means it takes 100 consecutive days to reach the 2x multiplier cap.
Day 50 = 1.5x, day 75 = 1.75x, etc.

---

## 1. Decimal Accounting

### Current behavior

Credits are integers. `session.complete` computes `seconds // 60` minutes
and awards that many credits. The fractional minute is lost.

### New behavior

Credits become **decimal display values backed by integer cents**. Every
credit-earning operation computes fractional amounts, stores integer cents in
Postgres, and returns decimal values through `/state`. The UI displays credits
rounded to 1 decimal place (e.g., "127.3 credits"). Internal computation uses 2
decimals to avoid accumulated floating-point drift — round to `Decimal("0.01")`
on every write.

### Server changes

- Credit helpers return `Decimal` or integer cents instead of whole-credit `int`.
- Balance writes persist integer cents and read-side serializers divide for the
  wire shape.
- All credit computations use `Decimal` internally and convert only at the
  read-side wire boundary.
- `credits_nonneg` validator checks `credits < 0` (works for float).
- DB columns (`LedgerEventRow.credits_before`, `.credits_after`, etc.) stay
  `Integer` — they store cents (value × 100). Multiply by 100 on write,
  divide on read. No Alembic migration needed for existing integer columns;
  existing rows are already in whole credits (×100 = correct cents).

### Frontend changes

- `credits` in `use_casino.js` becomes `Math.round((balance.get("credits") ?? 0) * 10) / 10`.
- Display uses `credits.toFixed(1)` instead of `credits.toLocaleString()`.
- Token amounts remain integers.

### Migration

Existing integer credits need a one-time Alembic conversion to cents if the DB
columns are changed in-place.

---

## 2. Streak Across Days

### Definition

A **daily streak** is a counter that starts at 0 and increments by 1 each
Pacific-time day on which the user accumulates at least 5 minutes of study
time (as measured by completed sessions whose `seconds >= 300` and whose
completion time falls on that Pacific day).

The streak produces a **multiplier** that applies to all credit awards:

```
streak_multiplier = min(streak_days * DAILY_STREAK_INCREMENT, STREAK_MULTIPLIER_CAP)
```

Where:

- `DAILY_STREAK_INCREMENT = 0.01` (1% per day)
- `STREAK_MULTIPLIER_CAP = 1.0` (100% bonus = 2x total)

### Persistence

Streak state lives server-side, in the per-user `credit_state` row (see
[Database Schema Changes](#database-schema-changes)): `streak_days`,
`last_qualifying_date`, `rest_days_used`. `rest_days_available` is derived,
not stored (see section 3). This state stays server-side because the server
computes it authoritatively on every session completion.

### Computation

On `session.complete` (live sessions only — **not** `session.add_past`):

1. Converts the session's `ended_at_ms` to a Pacific-time date.
2. Checks if total study seconds for that date (including this session)
   has crossed the 5-minute threshold.
3. If so, and if the date is consecutive with `last_qualifying_date`,
   increments `current_streak_days`.
4. If not consecutive (gap > 1 day), checks rest days first (see section 3).
5. Updates `last_qualifying_date`.
6. Computes `streak_multiplier` for credit award.

`session.add_past` earns credits but does **not** affect streak or daily
bonus state (see Design Decisions).

### UI

The study view shows:

- Current streak (e.g., "7-day streak")
- Current multiplier (e.g., "×1.07 bonus")
- A small progress bar or flame icon that fills toward the cap

---

## 3. Rest Days

### Definition

Every 14 days of daily streak, the user earns 1 rest day. A rest day
allows a single-day gap in the streak without resetting.

```
REST_DAY_STREAK_INTERVAL = 14  # days of streak to earn 1 rest day
```

### Behavior

When evaluating streak continuity after a session completes:

1. If the qualifying date is exactly 1 day after `last_qualifying_date`,
   streak continues normally.
2. If the gap is > 1 day, check `rest_days_available > 0`. If so, consume
   one rest day (covering a single missing day), and the streak continues.
3. If no rest days are available, reset the streak to 1 (today counts as
   day 1 of a new streak).

### Earning

Rest days are computed on the fly: `rest_days_available = (current_streak_days // REST_DAY_STREAK_INTERVAL) - rest_days_used`.

No need to "award" them explicitly — they're derived from streak length and
usage count.

### UI

- Show rest days available: "1 rest day available"
- When a rest day is consumed to preserve a streak, show a toast:
  "Rest day used — streak preserved!"

---

## 4. Time Milestones (Study Session Milestones)

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
break continuity (same as current pause behavior). Break time (section 6)
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

## 5. First-5-Minutes Bonus

### Definition

The first time a user accumulates 5 minutes of study time on a given
Pacific day, they receive a one-time bonus:

```
DAILY_FIRST_BONUS = 30  # credits
```

This bonus is multiplied by the streak multiplier (same as all credit
awards).

### Persistence

`last_first_bonus_date` is a column on the per-user `credit_state` row (see
[Database Schema Changes](#database-schema-changes)).

### Behavior

On `session.complete`, after computing the streak:

1. If the session date matches `last_first_bonus_date`, skip (already
   awarded today).
2. If total study seconds for today (including this session) >= 300:
   award `DAILY_FIRST_BONUS * streak_multiplier` credits, set
   `last_first_bonus_date` to today.

The 5-minute threshold is the same one that qualifies a day for streak
purposes. If the session pushes today over 5 minutes for the first time,
both the streak qualification and the bonus fire together.

### UI

- A toast: "Daily bonus! +30 credits (×1.07 = +32.1)"
- The study view could show a "Daily bonus: claimed ✓" indicator.

---

## 6. Break Time

### Definition

For every 3 minutes of study time, the user earns 1 minute of break time.
During a break (after stopping a session), the break timer counts down,
and the user continues earning credits at the same rate as if studying.

The purpose is to remove the fear of clicking "end session" — the user
gets rewarded whether they take a break or keep studying.

```
BREAK_ACCRUAL_RATE = 3  # minutes of study per 1 minute of break earned
BREAK_CREDIT_RATE = 1   # credits per minute of break (same as study)
```

### Mechanics

**Accrual**: On `session.complete`, the server computes:
`break_seconds_earned = floor(session_seconds / BREAK_ACCRUAL_RATE / 60) * 60`.

**Lazy settlement**: The server does **not** run a background timer. Instead
it records the break start timestamp and total break time. On every
subsequent request (sync, balance poll, new session start), it settles
the elapsed break credits lazily:

```
elapsed_s = (now_ms - break_started_at_ms) / 1000
break_used_s = min(elapsed_s, break_seconds_total)
credits_owed = (break_used_s / 60) * streak_mult_at_break_start
                - break_credits_settled
if credits_owed > 0:
    award credits_owed, increment break_credits_settled
if elapsed_s >= break_seconds_total:
    clear break state (break is over)
```

The streak multiplier is **locked** at the value it had when the session
ended (`streak_mult_at_break_start`) — break credits don't change the
multiplier retroactively.

**Interruption**: Starting a new session while on break clears the break
state immediately. Remaining unearned break time is lost (not banked).

**Milestone preservation**: Break time counts toward the time milestone
clock. If a user studies 50 minutes, stops (earning ~16 min break), the
break time continues the "session clock" for milestone purposes. The next
session starts with `50 + min(break_used, break_total) = 66` minutes
toward the next milestone.

### Persistence

Server-side in `credit_state`:

```
break_started_at_ms: int | None       # when session stopped, None = not on break
break_seconds_total: int              # total break time earned from that session
break_credits_settled: Decimal        # credits already awarded for elapsed break
streak_mult_at_break_start: Decimal   # multiplier locked at break start
```

### Frontend behavior

The frontend receives the break state from the server (via sync or a
dedicated endpoint). It computes the countdown locally:
`break_remaining = break_seconds_total - (now_ms - break_started_at_ms) / 1000`.
The UI countdown is purely cosmetic — the server settles credits on the
next request regardless. No special polling cadence needed beyond the
existing sync loop.

### Flow

```
session.complete → session credits awarded → break state written to DB
    ↓
frontend receives break state → shows countdown timer
    ↓
any server request → lazy settlement of elapsed break credits
    ↓
break_elapsed >= break_total → break state cleared
   OR
new session starts → break state cleared, remaining time lost
```

### UI

When on break:

- Replace the subject selector with a break timer panel.
- Show: "Break time: 12:34 remaining · earning 1.07 cr/min"
- Show next milestone progress.
- Button to "Skip break & study" (starts session, abandons remaining break).
- Break timer has the same visual weight as the study timer.

---

## Constants Module

`x/study_casino/credit_constants.py`:

```python
from decimal import Decimal

# Streak
DAILY_STREAK_STUDY_THRESHOLD_SECONDS = 300  # 5 minutes
DAILY_STREAK_INCREMENT = Decimal("0.01")     # 1% per day
STREAK_MULTIPLIER_CAP = Decimal("1.0")       # max 2x total
REST_DAY_STREAK_INTERVAL = 14                # streak days to earn 1 rest day

# Milestones
MILESTONE_BONUSES = [5, 10, 15, 20, 20, 20, 20, 20, 20, 20, 20, 20]
# Index 0 = after hour 1, index 1 = after hour 2, etc.

# Daily bonus
DAILY_FIRST_BONUS = Decimal("30")

# Break time
BREAK_ACCRUAL_RATE = 3  # minutes study per minute break
BREAK_CREDIT_RATE = Decimal("1")  # credits per minute of break

# Accounting
CREDIT_PRECISION = Decimal("0.01")  # round to cents
```

---

## Credit Award Formula

On session completion, the total credits awarded are:

```
base_credits = session_seconds / 60  (Decimal, not floor)

streak_mult = min(streak_days * DAILY_STREAK_INCREMENT, STREAK_MULTIPLIER_CAP)

milestone_credits = sum(MILESTONE_BONUSES[i]
                        for i in range(continuous_hours)
                        if i < len(MILESTONE_BONUSES))

first_bonus = DAILY_FIRST_BONUS if (first_time_over_5min_today) else 0

total_credits = (base_credits + milestone_credits + first_bonus) * (1 + streak_mult)
```

Break credits use the same multiplier:

```
break_credits = (break_minutes * BREAK_CREDIT_RATE) * (1 + streak_mult)
```

All results are rounded to `CREDIT_PRECISION` before writing to the
balance.

---

## Database Schema Changes

New per-user table `credit_state` — PK `user_id`, matching every other
per-user table (`balance`, `sessions`, …; see <../models.py>):

```sql
CREATE TABLE credit_state (
    user_id TEXT PRIMARY KEY,                  -- matches _USER_ID (OIDC sub)

    -- Streak
    streak_days INTEGER NOT NULL DEFAULT 0,
    last_qualifying_date TEXT,          -- ISO date, Pacific time
    rest_days_used INTEGER NOT NULL DEFAULT 0,

    -- Daily bonus
    last_first_bonus_date TEXT,         -- ISO date, Pacific time

    -- Break time (lazy settlement — no background timer)
    break_started_at_ms INTEGER,             -- ms epoch when session stopped; NULL = not on break
    break_seconds_total INTEGER NOT NULL DEFAULT 0,
    break_credits_settled REAL NOT NULL DEFAULT 0,  -- credits already awarded for elapsed break
    streak_mult_at_break_start REAL NOT NULL DEFAULT 1.0,

    -- Milestone accumulator (total continuous study+break seconds
    -- toward the next milestone, persists across session stops during breaks)
    milestone_accumulated_seconds REAL NOT NULL DEFAULT 0
);
```

Alembic migration `0003_credit_state.py` (next sequential after `0002_rng_audit`).

---

## UI Proposal

### Study View (no active session)

```
┌─────────────────────────────────────────────────┐
│  7-day streak 🔥  ×1.07 bonus   1 rest day      │
│  ▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░  30% to ×1.10      │
├─────────────────────────────────────────────────┤
│                                                 │
│           Choose your subject                   │
│       Every minute studied = 1 credit earned    │
│                                                 │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│   │ Biochem  │ │ Anatomy  │ │  Physio  │ ...   │
│   └──────────┘ └──────────┘ └──────────┘       │
│                                                 │
├─────────────────────────────────────────────────┤
│  Studied today   Credit balance   Sessions      │
│      47m            127.3            12          │
└─────────────────────────────────────────────────┘
```

### Active Session Header (sticky)

```
┌──────────────────────────────────────────────────┐
│ ● Biochemistry  00:47:23  +47.3 cr earned       │
│   Break earned: 15m  │  Next milestone: +10 in 13m│
│                          [Pause]  [Stop & Save]   │
└──────────────────────────────────────────────────┘
```

### Break Mode (replaces subject picker)

```
┌──────────────────────────────────────────────────┐
│ ☕ Break Time                                     │
│                                                   │
│        12:34                                      │
│   remaining · earning 1.07 cr/min                 │
│                                                   │
│   Next milestone: +10 in 23 min                   │
│                                                   │
│   [Skip break & study]                            │
└──────────────────────────────────────────────────┘
```

### Stats View additions

- Current streak, best streak, total rest days used
- Total milestone bonuses earned
- Total daily bonuses earned
- Total break credits earned

---

## Test Scenarios

### Normal cases

1. **Basic session**: 10-minute session earns 10 base credits, no
   milestone, no streak if first session ever.
2. **Milestone at hour 1**: 60-minute session earns 60 base + 5 milestone
   = 65 credits.
3. **Milestone at hour 2**: 120-minute session earns 120 base + 5 + 10
   = 135 credits.
4. **Multi-milestone**: 180-minute session earns 180 + 5 + 10 + 15 = 210.
5. **Streak day 1**: Complete a 5+ min session, streak becomes 1,
   multiplier 0.01.
6. **Streak day 2**: Next Pacific day, 5+ min session, streak becomes 2,
   multiplier 0.02.
7. **Streak day 100**: Multiplier capped at 1.0 (2x total).
8. **Rest day earned**: At streak 14, `rest_days_available` becomes 1.
9. **Rest day consumed**: Miss a day at streak 15, streak preserved at 16
   (next qualifying day), `rest_days_used` becomes 1.
10. **Daily bonus**: First session crossing 5 min on a new day awards +30
    credits (multiplied).
11. **No double daily bonus**: Second session on same day does not re-award
    the +30.
12. **Break accrual**: 9-minute session earns 3 minutes of break time.
13. **Break credits (lazy settlement)**: 3-minute break. User starts a new
    session 2 minutes later. Server settles 2 min of break credits (2 cr
    multiplied). 1 min of break time lost.
14. **Break + milestone**: Study 50 min, take 16 min break, then study 14
    min — total continuous time 80 min (50 + 16 break + 14), qualifies for
    hour-1 milestone.

### Corner cases

15. **Sub-5-minute session**: 4-minute session earns 4 credits (fractional),
    no daily bonus, no streak qualification, but the day's study time
    accumulator still tracks 4 minutes toward the 5-minute threshold.
16. **Session spanning midnight Pacific**: A session started at 11:50 PM
    and stopped at 12:10 AM. The session date is determined by
    `ended_at_ms` (or the midpoint?). Both days could qualify — define
    that credit accrual uses `ended_at_ms` for day assignment, but streak
    qualification checks each day independently.
17. **Multiple sessions on same day**: Three 3-minute sessions (9 min
    total). After the second session crosses 5 min, the daily bonus and
    streak qualification fire. Third session adds 3 more minutes.
18. **Past session added**: Adding a past session earns credits but does
    not affect streak or daily bonus — only live sessions count.
19. **Session edited to shorter**: Editing a session from 10 min to 2 min
    on a day that only had 5 min total. Credits are debited for the
    difference (recorded as a `session_edit` ledger event). Streak and
    daily bonus state are not changed (append-only).
20. **Session deleted**: Same as edit-to-shorter — credits debited,
    streak/bonus untouched. Recorded as a `session_delete` ledger event.
21. **Break settles naturally**: User stops with 10 min break banked.
    Next sync 10+ min later — server settles all 10 min of credits,
    clears break state. No break time remaining.
22. **Break interrupted by new session**: User has 8 min break remaining
    (10 total, 2 elapsed and settled). Starts new session. Server settles
    nothing extra — remaining 8 min lost. Milestone accumulator carries
    forward the 2 min of break that did count.
23. **Rest day with 2+ day gap**: User has 1 rest day, misses 2 days.
    Rest day covers 1 day, but the second day breaks the streak. Streak
    resets to 1 on next qualifying day.
24. **Fractional credits at boundary**: 61-second session earns 1.0167
    credits, rounded to 1.02. Next 59-second session earns 0.9833,
    rounded to 0.98. Total: 2.00 (correct, no drift).
25. **Streak at exactly the cap**: streak_days = 100, multiplier = 1.0
    (capped). Next day, multiplier stays 1.0.
26. **Milestone bonus with high multiplier**: Hour-1 milestone (5 cr)
    with streak multiplier 1.0 → 5 × 2.0 = 10 credits from milestone
    alone.
27. **Break time rounding**: 7 minutes of study earns floor(7/3) = 2 min
    break (120 seconds). The 1 extra minute of study does NOT partially
    accrue — it's whole-minute only.
28. **Zero-length session**: Session started then immediately stopped.
    Seconds = 0. Session is deleted (current behavior). No credits, no
    streak, no break time.
29. **Import existing data**: Old data with integer credits. Credits
    display correctly as floats (e.g., 150 → "150.0"). No streak state
    exists yet; starts at 0.
30. **Timezone edge case**: User is not in Pacific time. The server
    always uses Pacific time for day boundaries regardless of client
    timezone. The client shows Pacific-relative day info.

---

## Implementation Phases

### Phase 1: Decimal accounting + constants module

- Add `credit_constants.py`
- Change `_credits()` to return `float`, `_set_balance()` to write float
- Update frontend display to 1 decimal
- Update `LedgerEventRow` read/write to cents encoding (×100/÷100)
- Tests: fractional credit awards, rounding, display

### Phase 2: Streak + daily bonus + rest days

- Add `credit_state` table (Alembic migration)
- Implement streak computation on `session.complete`
- Implement daily bonus check
- Implement rest day logic
- Apply multiplier to all credit awards
- Tests: streak growth, cap, reset, rest day, daily bonus

### Phase 3: Time milestones

- Track `milestone_accumulated_seconds` in `credit_state`
- Compute milestone bonuses on session complete
- Apply multiplier to milestone bonuses
- Tests: milestone boundaries, carry-forward

### Phase 4: Break time

- Track break state in `credit_state` (`break_started_at_ms`,
  `break_seconds_total`, `break_credits_settled`,
  `streak_mult_at_break_start`)
- Implement break accrual on `session.complete`
- Implement lazy settlement: on every server request, compute elapsed
  break time, award owed credits, clear state if break is over
- Lock streak multiplier at break start
- Starting a new session clears break state (remaining time lost)
- Break credits earn the locked multiplier
- Tests: accrual, lazy settlement, interruption, milestone carry

### Phase 5: UI updates

- Streak display with progress bar
- Milestone countdown during session
- Break mode UI
- Stats view additions
- Daily bonus toast
