# claude-quota: panel UX design

## Problem

Two AI coding assistants (Claude, Codex) each enforce two rate-limit windows:

| provider | short window | long window    |
| -------- | ------------ | -------------- |
| Claude   | 5h burst     | 7d             |
| Codex    | ~1h primary  | ~24h secondary |

The widget should answer, at a glance:

1. **Throttle now?** Am I about to hit a short-window limit. Rare, but urgent — if I'm at 90% of the 5h window the next burst will get 429s.
2. **Will I run dry early?** Am I burning the long window faster than it refreshes — i.e. will I hit 100% with N days still on the clock and have to stop heavy work?
3. **Am I leaving quota on the table?** The long window is paid for whether I use it or not. If I'm at 30% used with 1 day left in a 7d window, I should send work _now_ before the bucket resets.

A naïve "% used + time to reset" display doesn't separate (2) from "on pace, finishing exactly at reset" — both look like high usage near a deadline. The signal we actually want is **pace deviation**: how far above/below the constant-rate line we are.

## Prior art

The closest analogues I can think of, in rough order of relevance:

| Domain                                                          | What they show                                                                                             | Take-away                                                                                                             |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Cellular data caps** (T-Mobile/Verizon apps, iOS data widget) | Bar of % used + a vertical "expected" tick at `time_elapsed/cycle_length`; color goes red when fill > tick | This is _literally_ our problem. Two-marker progress bar is the established idiom.                                    |
| **ISP monthly data caps** (Comcast etc.)                        | Same pattern — fill + pace marker + days remaining                                                         | Confirms the idiom.                                                                                                   |
| **AWS/GCP/Datadog budget burn-rate dashboards**                 | Forecast of end-of-period spend at current burn; "you will be $X over budget at this rate"                 | Forecast text ("will exhaust in Yd Zh") is more actionable than raw %.                                                |
| **GitHub Actions minutes / Cloudflare bandwidth**               | Plain % bar + reset date                                                                                   | Minimum viable; doesn't address pace.                                                                                 |
| **Sprint burn-down charts**                                     | Line chart: ideal slope vs actual cumulative; gap = ahead/behind schedule                                  | Same math (cumulative-vs-ideal) but as time series. Strong "are we ahead/behind" signal but doesn't fit a 24px panel. |
| **EV/Tesla range vs route**                                     | "You have 213mi, route needs 180mi → 33mi surplus on arrival" with green/red coloring                      | Reframes as _surplus at deadline_ rather than _% used now_ — arguably the most useful framing for our case.           |
| **Hybrid car eco/pace gauge**                                   | Needle centered on "ideal throttle"; deviates left (under) / right (over)                                  | Pace-as-needle is a good compact widget.                                                                              |
| **Strava/Garmin pacing**                                        | Current pace vs target; ahead/behind in seconds                                                            | Confirms the signed-deviation framing.                                                                                |
| **Battery icons**                                               | Single fill, optional %, color thresholds                                                                  | Familiar but no pace concept. Good fallback when pace data isn't available (early in window).                         |
| **401k / retirement projection**                                | Projected end-state given current contribution rate                                                        | Same forecast pattern as cloud burn-rate.                                                                             |

The clear winner across the references is **bar with two markers (fill + pace tick)** plus a **forecast** ("will run out 2d before reset" / "will leave 18% on the table"). Cell carriers landed on this for the same problem; we should steal it.

## Design options for the panel widget

The panel widget has roughly 16–20px of vertical space and we want to show two providers, each with two windows. Options, from simplest to most elaborate:

### A. Battery-style fill, no pace

```
A▮▮▮▯▯  O▮▮▮▮▯
```

One mini-bar per provider, showing the binding (long) window's % used. Optional numeric suffix.

- **Pros**: dead simple, instantly readable.
- **Cons**: ignores pace, ignores the short window. Falls into the "looks low when on track" trap.

### B. Battery fill + pace tick

```
A▮▮▮│▯▯  O▮▮▮▮│▯
       ↑              ↑
       time-elapsed marker
```

Same bar, plus a 1px tick at `time_elapsed_fraction`. Distance between fill edge and tick = pace deviation. Color the bar: green if fill < tick (surplus), red if fill > tick (burning hot).

- **Pros**: encodes pace in the same widget. Established idiom (cell carriers).
- **Cons**: subtle on a 16px-wide bar; fine print is hard to read at a glance.

### C. Brand logo, tinted by pace state

Just the logo, recolored by pace status:

| Color                | Meaning                                                                                |
| -------------------- | -------------------------------------------------------------------------------------- |
| blue                 | Surplus available, > 10% behind pace ("use it or lose it" if window is mostly elapsed) |
| default (white/grey) | On pace ±5%                                                                            |
| yellow               | Burning hot, 5–15% ahead of pace                                                       |
| red                  | Burning hot, ≥15% ahead of pace **OR** short window > 85%                              |

- **Pros**: most compact, brand-recognizable, color is preattentive.
- **Cons**: throws away magnitude. "How bad" requires opening the popup.

### D. Brand logo + small pace numeral

```
[A] -3%   [O] +8%
```

Sign convention: `+` = ahead of pace (bad / burning hot), `-` = behind (surplus). Color the numeral.

- **Pros**: dense, gives magnitude, no chart-reading needed.
- **Cons**: requires the user to internalize the sign convention. `+8%` looking bad is unintuitive at first.

### E. Brand logo as pie/water-fill

The logo shape itself is partially filled (water rising in a glass) to encode % used. The fill color encodes pace.

- **Pros**: very compact, visually distinctive, brand-y.
- **Cons**: hard to render precisely at 16px on irregular logo shapes (the Anthropic mark and OpenAI swirl aren't simple convex shapes); fill levels under ~25% or over ~75% become illegible. Implementation is Cairo-heavy.

### F. Two-row stacked mini-bars per provider

```
A short ▮▮▯▯▯
A long  ▮▮▮▮│▯
```

Both windows visible simultaneously, with pace tick on the long one.

- **Pros**: shows everything.
- **Cons**: 2-row layout doesn't fit cleanly into a single-line panel; effectively requires its own panel slot per provider.

## Recommended design

**Default panel widget**: option C + D combined.

```
[A] +12%   [O] -8%
 ▲           ▲
 │           └── color: blue (surplus, behind pace)
 └── color: yellow (burning hot, 12% ahead of pace)
```

- One brand logo per provider as `St.Icon` (16px, monochrome SVG with `style_class` for tint).
- Logo color encodes the **binding** window's pace state (long window normally; short window if it crosses the urgency threshold — short wins because it's the immediate-pain one).
- Optional pace numeral suffix (default on for the long window).
- Optional `▮` short-window mini-bar on the right edge (default off; turn on if you find yourself frequently hitting the short window).

**Click → popup** (existing `PanelMenu.Button` popup):

```
Claude
  burst (5h)   13% used     ↻ 4h 12m   pace: -28% (cool)
  weekly (7d)  82% used     ↻ 1d 3h    pace: +12% (will exhaust ~17h before reset)
Codex
  primary (1h)  3% used     ↻ 47m      pace: -54% (cool)
  secondary (24h) 41% used  ↻ 14h      pace: -7%  (on pace)
```

For each window: total length, used %, time-to-reset, pace deviation, **and a forecast in plain English** — that's the most actionable line ("will exhaust 17h before reset" / "will leave 22% unused at reset").

### Why this design

- **At-a-glance answers Q1 and Q2** (throttle now / running early) via icon color. Red/yellow icon = open popup for details.
- **Q3 (use-it-or-lose-it) is signalled** by blue tint + late-in-window: if the icon is blue and `reset_after_seconds < 0.2 * total_window_seconds`, you've got unused quota worth burning.
- **Brand recognition** is preserved — no "C" / "O" letter prefixes that look like debug output (current TODO.md item).
- **Failure modes degrade gracefully**: if the API is down or the credential is missing, fall back to a greyed icon with `?` suffix.

### Pace math

For a window with `used_percent ∈ [0,100]` and known total length `W` and time-to-reset `R`:

```
time_elapsed_fraction = (W - R) / W
expected_used = 100 * time_elapsed_fraction
pace_deviation = used_percent - expected_used         # signed, in percentage points
```

Forecast at current pace:

```
if used_percent > 0 and time_elapsed_fraction > 0:
    burn_rate_per_sec = used_percent / (W - R)
    seconds_to_exhaustion = (100 - used_percent) / burn_rate_per_sec
    surplus_or_deficit_seconds = R - seconds_to_exhaustion
    # positive surplus  → "will leave X% unused at reset"
    # negative surplus  → "will exhaust Yh Zm before reset"
```

Edge cases:

- **First 5% of window** (`time_elapsed_fraction < 0.05`): pace is too noisy. Suppress pace output, fall back to plain "% used".
- **Last 5% of window**: same — pace becomes hyper-sensitive. Just show "% used" and time-to-reset.
- **`used_percent == 0`**: forecast is "infinite surplus" — display `-` rather than a number.

### Cumulative pace vs. instantaneous burn rate

The pace-deviation metric above is **cumulative**: it compares total-used to ideal-rate-from-window-start. This is the right default — it's what tells you "will I make it to reset?".

A separate **instantaneous** metric ("burn rate over last 30 min") is useful for "am I overspending _right now_", which is the question worth asking when you're about to start a heavy task. The API only returns cumulative %, so this requires the extension to retain a short rolling history of `(timestamp, used_percent)` samples and finite-difference them.

Defer to v2 (see `TODO.md`). When implemented, surface as a third popup field: "burn rate (last 30m): 4%/h → would exhaust in 5h 12m at this rate".

## Render knobs (proposed gschema settings)

| Key                         | Type     | Default | Purpose                                                                                  |
| --------------------------- | -------- | ------- | ---------------------------------------------------------------------------------------- |
| `show-pace-numeral`         | bool     | true    | The `+12%` / `-8%` text after each logo                                                  |
| `show-percent`              | bool     | false   | Replace the pace numeral with raw `% used`. Mutually exclusive with `show-pace-numeral`. |
| `show-short-window-bar`     | bool     | false   | Tiny `▮▮▯▯▯` short-window bar to the right of the logo                                   |
| `pace-yellow-threshold`     | int (pp) | 5       | Pace deviation at which logo turns yellow                                                |
| `pace-red-threshold`        | int (pp) | 15      | Pace deviation at which logo turns red                                                   |
| `short-window-warn-percent` | int      | 85      | Short-window % at which the icon flips to red regardless of long-window pace             |
| `surplus-blue-threshold`    | int (pp) | 10      | Pace deviation below which logo tints blue ("use it or lose it")                         |
| `poll-interval-seconds`     | int      | 120     | Currently a const; promote to a setting                                                  |

A second tier of cosmetic knobs (`show-claude` / `show-codex` to hide a provider; `compact-mode` to drop suffixes entirely) can come later — start with the table above.

## Implementation notes (carry-overs to TODO.md)

- Brand SVG marks: ship monochrome single-path SVGs in `gnome-extensions/claude-quota/icons/` (Anthropic press kit + OpenAI brand page; both publish a black/white mark suitable for monochrome use). Load via `Gio.FileIcon` from `extension.path`. Note: SVGs are owned by Anthropic/OpenAI and are not covered by the repo's AGPL license — they're vendored under fair use for personal use only.
- `St.Icon` supports CSS-based recoloring via `-st-icon-style: symbolic` + `color:` on the style class. Use `style_class` swaps (`quota-ok` / `quota-warn` / `quota-hot` / `quota-cool`) rather than inline color.
- Window total lengths: hard-code the four (5h, 7d, ~1h, ~24h) initially. The Codex API returns `limit_window_seconds` so prefer that when present; the Claude API doesn't, so derive from `resets_at - now` floor-rounded to the next 5h or 7d boundary.
- Move `POLL_INTERVAL_SECONDS` and the thresholds from `extension.js` constants to a `Gio.Settings` schema.
- The `_updateLabel`/`_fetchClaude`/`_fetchCodex` methods currently compose the panel string inline. Refactor: each provider returns a `ProviderState` (used %, reset_seconds, window_seconds, pace_deviation, status: ok|warn|hot|cool|stale|unknown), and a single `_renderPanel(states)` builds the panel widget from those. Makes pace-state coloring a one-liner and the popup a separate render path.

## Open questions

- **Which window drives the icon color?** Proposed: long window in steady state, but short window overrides when it crosses `short-window-warn-percent`. Alternative: always whichever has the worse pace. The "always worst" rule is more honest but makes the icon noisier (short-window pace fluctuates a lot during active sessions).
- **Should the popup show seven-day-opus / seven-day-sonnet?** Claude's API breaks the 7d window down by model. For users who care about Opus headroom specifically, a third popup row would help; for everyone else it's clutter. Probably gate on a `show-model-breakdown` setting, default off.
- **Does anyone actually want option E (logo as pie)?** It's the cutest but the most expensive to build. Skip unless someone asks.
