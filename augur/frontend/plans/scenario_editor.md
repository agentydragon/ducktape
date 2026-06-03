# Scenario editor v2 — per-knob plan

Goal: a **full-width** scenario editor (not the sidebar form) that compares a _set_ of scenarios.
Most knobs are **shared** (edit once → applies to all); any can be **overridden** per scenario.
A few stay **global-only**. Lifecycle events are kept (per-scenario). Charts: **lines** first;
**candles** later (with a month/quarter/half/year granularity picker).

## Data model (unchanged)

Each scenario keeps a full `ProductInput` (today's shape). "Shared vs overridden" is **computed**:
a knob is _shared_ when every scenario agrees (edit writes to all), _overridden_ when they differ
(or the user pinned it as a column). No base/override storage, no URL-schema change — `?scenarios=`
already carries each scenario's full input. The only new UI state is a "pinned override columns" set.

## Disposition of every current knob

### Global-only (app-shell `SharedControls`; one value for the whole set)

| Knob                  | URL      | Notes                                          |
| --------------------- | -------- | ---------------------------------------------- |
| Horizon months        | `?h`     | per your call — stays shared                   |
| Rollout count         | `?n`     | stays shared                                   |
| First seed            | `?seed`  | stays shared (shared seeds = apples-to-apples) |
| Exogenous model       | `?x`     | per your call — stays shared                   |
| Chart scale (lin/log) | `?scale` | display-only                                   |
| Currency display      | `?fmt`   | display-only                                   |

### Shared-or-override scalars (editor table: shared row if equal, column if differ; "+ Add override")

| Knob                         | Widget                                |
| ---------------------------- | ------------------------------------- |
| Monthly spend                | $ number — **overridable** (your ask) |
| Spend index (inflation/none) | select                                |
| Monthly rent (outside rent)  | $ number                              |
| Rent location                | select                                |
| Cash-buffer trigger-below    | $ number                              |
| Cash-buffer sell amount      | $ number                              |
| Cash-buffer index            | inflation/none                        |
| PE LNW floor                 | $ number                              |
| PE floor index               | inflation/none                        |

### Shared-only (one value, writes to all; not an override column for now — your call)

| Knob                   | Widget                               |
| ---------------------- | ------------------------------------ |
| Liquidity / sell order | the reorderable sell-preference list |

### Per-scenario cluster (housing is the main axis scenarios vary on; edited per scenario)

Edited in a per-scenario **Property & timeline** panel (reuses today's `PropertyPurchasePanel` +
`RentalPanel` + `LifecycleEventsEditor`), so all of these stay fully editable per scenario; setting
every scenario equal is the "global" case.

| Knob                                                                        |                                         |
| --------------------------------------------------------------------------- | --------------------------------------- |
| Property to buy                                                             | select                                  |
| Financing (cash/mortgage)                                                   | select                                  |
| Down payment %, term, annual rate                                           | mortgage-only                           |
| Insurance %, maintenance %                                                  | numbers                                 |
| Owner lives here                                                            | checkbox                                |
| Rented fraction %, vacancy %, full-property rent                            | rental                                  |
| Use management agency; fee %, leasing fee mo, avg tenancy                   | management                              |
| **Lifecycle events** (set rented %, set primary, capital improvement, sale) | **kept** — per-scenario timeline editor |

### Read-only / informational (unchanged)

| Item                               |                                       |
| ---------------------------------- | ------------------------------------- |
| Initial portfolio table            | read-only, global (deployment config) |
| Taxes blurb (federal + CA, single) | static text                           |

## Editability guarantee

Every currently-editable knob stays editable: scalars at **both** shared and per-scenario-override
levels; sell order at the shared level; the housing cluster + events at the per-scenario level.
Nothing is dropped.

## Charts

- **Lines** (first): per scenario, solid median + dashed P5/P95, one color each, **red-free palette**
  (red stays reserved for failed rollouts). No filled bands in multi-scenario mode.
- **Candles** (later): grouped per checkpoint, with a **granularity** picker (month / quarter / half /
  year). Needs the existing percentiles only (no backend change).

## Layout

Full-width: scenario chips + shared/override fields + the active scenario's Property & timeline
panel on top (full width), results below. The editor is **collapsible** (chips stay visible) so the
chart is reachable without scrolling past it. Shared scalars are **grouped** (Spending / Outside
rent / Cash buffer / Private equity) so related knobs stay together.

## Shipped vs. target: base + per-variant overrides

**Shipped** (current editor): each scenario stores a full `ProductInput`; "shared vs override" is
_computed_ — a scalar is shared when every scenario agrees (edit-once writes to all), an override
when it differs (or is pinned). Housing + lifecycle events are edited per active scenario (chips).

**Gap**: this isn't an explicit base model. When a knob has diverged there's no single place to
change it for everyone without clobbering the intentional overrides ("Make shared" forces all to one
value), and per-scenario housing reads as "every scenario edits everything".

**Target** — explicit **Base** + per-variant **overrides**:

- Data: `{ base: ProductInput, variants: [{ id, label, overrides: Partial<ProductInput> }] }`; a
  variant resolves to `{ ...base, ...overrides }`. URL: base (full) + per-variant diffs; old
  `?scenarios=` full-input arrays decode by taking the first as base and the rest as diffs (a
  1-element set → base only).
- Editing **Base** propagates to every variant that doesn't override that knob; overriders keep
  their value — the "edit a parameter in everything" the current UI lacks.
- UI = **spreadsheet for scalars + per-variant detail for housing**:
  - Scalars: one knob×scenario table (grouped rows) with a **Base** column + one column per variant;
    a variant cell shows the inherited Base value (muted) + "override", or its own value (bold) +
    "revert to base". (The mockup's table, made explicit.)
  - Housing (property / financing / rental / events): too rich for cells — Base housing edited in a
    Base "Property & timeline" panel; a variant "Override housing" gets its own panel. Rent-vs-buy
    _is_ a housing override.
- Open choices: (1) Base as a non-charted defaults config vs. designating one variant as the base;
  (2) confirm spreadsheet-for-scalars + chip-for-housing (vs. one giant spreadsheet with flattened
  housing).
