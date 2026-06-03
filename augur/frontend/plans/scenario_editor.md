# Scenario editor — per-knob plan

A **full-width** scenario editor (not the sidebar form) that compares a **Base** scenario against
per-variant **overrides**. Most knobs are edited once on Base (edit → propagates to every variant
that doesn't override it); any scalar can be **overridden** per variant, and the housing cluster is
overridden per variant **as a unit**. A few knobs stay **global** (app shell). Lifecycle events are
kept (Base or per-variant). Charts: **lines** first; **candles** later (month/quarter/half/year
granularity picker).

## Data model (base + overrides)

`{ base: { label, input: ProductInput }, variants: [{ id, label, overrides: Partial<ProductInput> }] }`.
A variant resolves to `{ ...base.input, ...overrides }` — it inherits every knob it doesn't
override. **Base is a charted series** (series 0, blue); variants follow in the remaining colors.
Editing a Base knob updates every inheriting variant for free; a variant only diverges on the keys
in its `overrides`. The active selection (Base or a variant) is ephemeral UI state — it scopes the
histogram, selected-rollout overlay, events, and the detailed terminal table, and is not persisted.

### URL

- Base, no variants → compact, shareable, backward-compatible `?s=`/`?lc=` (a single scenario reads
  exactly like the pre-comparison product link).
- Base + variants → `?scenarios=` JSON (`v: 2`): the Base full input plus per-variant **override
  diffs** (not full inputs). Lifecycle `_id`s are stripped on encode and reminted on decode so
  siblings never share React keys. Variants are capped at `MAX_VARIANTS` on decode.
- A legacy/unrecognized `?scenarios=` blob (or malformed JSON) falls back to Base-only, reading any
  `?s=` present — same version gate as the single-scenario codec.

## Disposition of every current knob

### Global (app-shell `SharedControls`; one value for the whole set)

| Knob                  | URL      | Notes                                          |
| --------------------- | -------- | ---------------------------------------------- |
| Horizon months        | `?h`     | stays global                                   |
| Rollout count         | `?n`     | stays global                                   |
| First seed            | `?seed`  | stays global (shared seeds = apples-to-apples) |
| Exogenous model       | `?x`     | stays global                                   |
| Chart scale (lin/log) | `?scale` | display-only                                   |
| Currency display      | `?fmt`   | display-only                                   |

### Base-or-override scalars (editor spreadsheet: Base column + one column per variant)

A variant cell shows the inherited Base value (muted) until edited; editing it creates the override
(bold) and a ↩ revert appears to drop it back to inheriting Base. Grouped so related knobs stay
together (Spending / Outside rent / Cash buffer / Private equity).

| Knob                         | Widget         |
| ---------------------------- | -------------- |
| Monthly spend                | $ number       |
| Spend index (inflation/none) | select         |
| Monthly rent (outside rent)  | $ number       |
| Rent location                | select         |
| Cash-buffer trigger-below    | $ number       |
| Cash-buffer sell amount      | $ number       |
| Cash-buffer index            | inflation/none |
| PE LNW floor                 | $ number       |
| PE floor index               | inflation/none |

### Base-only (one value; inherited by all variants, not an override column)

| Knob                   | Widget                               |
| ---------------------- | ------------------------------------ |
| Liquidity / sell order | the reorderable sell-preference list |

### Per-entity housing cluster (overridden per variant as a unit)

Edited in the **Property & timeline** panel for the active entity (`PropertyPurchasePanel` +
`RentalPanel` + `LifecycleEventsEditor`). Base housing edits propagate to inheriting variants. A
variant either **inherits** Base housing (one-line summary + "Override housing", which copies the
whole cluster from Base with fresh lifecycle keys) or **overrides** it (full panel + "Revert to base
housing"). Rent-vs-buy _is_ a housing override.

| Knob                                                                        |                            |
| --------------------------------------------------------------------------- | -------------------------- |
| Property to buy                                                             | select                     |
| Financing (cash/mortgage)                                                   | select                     |
| Down payment %, term, annual rate                                           | mortgage-only              |
| Insurance %, maintenance %                                                  | numbers                    |
| Owner lives here                                                            | checkbox                   |
| Rented fraction %, vacancy %, full-property rent                            | rental                     |
| Use management agency; fee %, leasing fee mo, avg tenancy                   | management                 |
| **Lifecycle events** (set rented %, set primary, capital improvement, sale) | per-entity timeline editor |

### Read-only / informational (unchanged)

| Item                               |                                       |
| ---------------------------------- | ------------------------------------- |
| Initial portfolio table            | read-only, global (deployment config) |
| Taxes blurb (federal + CA, single) | static text                           |

## Editability guarantee

Every currently-editable knob stays editable: scalars on **Base** and as a **per-variant override**;
sell order on Base; the housing cluster + events on Base and per-variant. Nothing is dropped.

## Charts

- **Lines** (shipped): Base + each variant draw a solid median + dashed P5/P95, one color each,
  **red-free palette** (red stays reserved for failed rollouts). No filled bands in multi-scenario
  mode. A lone Base renders exactly like the pre-comparison chart (filled bands, blue).
- **Candles** (later): grouped per checkpoint, with a **granularity** picker (month / quarter / half
  / year). Needs the existing percentiles only (no backend change).

## Layout

Full-width: scenario chips (Base + variants, the active one ringed) + the scalar spreadsheet + the
active entity's Property & timeline panel, results below. The editor is **collapsible** (chips stay
visible) so the chart is reachable without scrolling past it.
