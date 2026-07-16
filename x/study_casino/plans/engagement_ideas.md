# Engagement Ideas — Making Studying More Addictive (Ethically)

Status: idea backlog. The retention core (streaks, daily bonus, milestones,
break time) is designed in <credit_system_v2.md> — implement that first.
These ideas layer on top of it, grouped by the psychological lever each
one pulls. Delete entries as they ship or get rejected.

## Priority order

1. <credit_system_v2.md> phases 1–2 (decimal accounting + streak/daily
   bonus) — the proven retention core.
2. Casino gating on today's study time — cheap; directly converts the
   gambling urge into study sessions.
3. Lucky-session variable rewards — small; reuses the RNG audit stack.
4. Quests and jackpot — need new schema and UI; later.

## Variable-ratio rewards on the study side

All the slot-machine psychology (random, intermittent reward) currently
lives in the casino; studying pays a flat, predictable 1 credit/minute.
Variable-ratio is the most addictive reinforcement schedule — put some of
it on the virtuous action:

- Each completed session has a small chance (e.g. 15%) of a surprise
  bonus: "Lucky session! ×2 credits" or a rare "golden chip" drop.
- Server-resolved via the existing deterministic-RNG stack
  (`rng_action_audits` / `rng_call_audits`), so it's audit-logged and
  provably fair like the casino games.

## Casino gating on today's studying

Make studying the only fuel for gambling and keep the fuel gauge visible.
In increasing strength:

- A free daily spin that unlocks only after 25 minutes studied today.
- Wager caps proportional to today's study minutes (studied 60 min →
  max 60 tokens per bet today).
- The casino door is closed until N minutes studied today; the locked
  view shows "study 12 more minutes to open the casino". Converts "I
  want to gamble" directly into a session start.

## Goal-gradient toward prizes

People accelerate as they approach a goal. The prizes view already has a
catalog and token balance — add a progress bar per prize ("73% of the way,
≈ 2 study days at your current pace"). Same treatment for the next streak
milestone and next rest day.

## Quests

A small rotating set of weekly challenges paying credit bonuses:
"3 sessions in Anatomy this week", "one 2-hour block", "study before
10am twice". Adds novelty (the thing flat time-for-credits lacks most)
and steers behavior worth steering: subject balance, deep-work blocks,
morning starts.

## Jackpot fed by losses

A fraction of every losing wager feeds a visible progressive jackpot.
Only study milestones grant eligibility (e.g. each hour studied this
week = one jackpot ticket, drawn Sunday). Losses stop feeling purely
negative, and eligibility is another study hook.

## Cosmetic unlocks

Card backs, table felt colors, slot themes, a title next to the username —
unlocked by lifetime study hours or streak records, **not** purchasable
with tokens. Rewards long-term accumulation without inflating the prize
economy. The frontend already has theming primitives (`COLORS`, fonts).

## Start-friction reduction

Addiction is mostly about how easy the next hit is; the hardest part of
studying is minute zero:

- One-tap "resume last subject" button.
- A "just 5 minutes" quick-start pairing with the first-5-minutes bonus
  from credit_system_v2.

## Guardrails (what keeps this "in a good way")

- Streaks stay forgiving — rest days (credit_system_v2 §3) prevent the
  broken-60-day-streak rage-quit.
- The one-way credits → tokens economy stays inviolate: winnings can
  never be re-gambled, so the casino can't become the point. Any new
  mechanic must preserve this.
- No punishment mechanics beyond streak reset; never debit balances for
  not studying.
