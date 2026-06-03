# Scenario editor — per-knob plan

A **full-width** comparison editor over a **Base** scenario and per-variant **overrides**. Every
per-scenario knob is a **table row** (columns = Base + variants), edited in place: a Base cell edits
it everywhere, a variant cell inherits Base (rendered greyed) until overridden (then white, with an
inline ↩ to revert). The one list-shaped knob — the lifecycle **timeline** — is the active
scenario's editor below the table. A few knobs stay **global** (app shell). Charts: **lines** first;
**candles** later (month/quarter/half/year granularity picker).

## Data model (base + overrides)

`{ base: { label, input: ProductInput }, variants: [{ id, label, overrides: Partial<ProductInput> }] }`.
A variant resolves to `{ ...base.input, ...overrides }` — it inherits every knob it doesn't
override. **Base is a charted series** (series 0, blue); variants follow. Editing a Base knob updates
every inheriting variant for free; a variant only diverges on the keys in its `overrides`. The active
selection (Base or a variant) is ephemeral UI state — it scopes the timeline editor, the rollout
histogram, the selected-rollout overlay, events, and the detailed terminal table; it is not
persisted.

### URL

- The whole set serializes to a single `?scenarios=` JSON param (`v: 2`): the Base full input plus
  per-variant **override diffs**. A lone Base (no variants) uses the same form. Lifecycle `_id`s are
  stripped on encode / reminted on decode; variants are capped at `MAX_VARIANTS`. A
  malformed/unrecognized blob falls back to a default Base-only set.

## The table (everything per-scenario)

Rows = knobs, grouped (Property / Spending / Outside rent / Cash buffer / Private equity); columns =
Base + each variant. Each cell is the right widget for its knob: `$` number, `%` number, `mo`
number, inflation/none select, Yes/No select, Cash/Mortgage select, 30/15-yr select, property
select, location select. Owning rows only appear when some scenario makes them relevant (the same
"union" trick the metric list uses):

| `needs`    | Row shows when some scenario…         | Rows                                                            |
| ---------- | ------------------------------------- | --------------------------------------------------------------- |
| (none)     | always                                | Property to buy, all Spending / Outside-rent / Cash-buffer / PE |
| `owns`     | has a property                        | Financing, Insurance, Maintenance, Owner-lives-here, Rented %   |
| `mortgage` | buys with a mortgage                  | Down payment, Term, Annual rate                                 |
| `rented`   | rents the property out (fraction > 0) | Vacancy %, Full-property rent, Use management                   |
| `managed`  | uses a management agency              | Mgmt fee, Leasing fee, Avg tenancy                              |

A variant cell renders **inherited** (greyed/`filled`, showing Base's value) or **overridden** (white,
with an inline ↩ to revert). Editing an inherited cell creates the override; ↩ drops the key so it
re-inherits Base. There is no housing "unit" — every knob (property, financing, rental, …) overrides
individually, exactly like the scalars.

## Below the table

- **Timeline** (lifecycle events) — the active scenario's editor (a variable-length event list can't
  live in a cell). Shown when the active scenario owns a property; for a variant it carries its own
  override with a "Revert to base" affordance.
- **Sell order** — Base-only (the reorderable liquidity list, inherited by all variants).
- **Initial portfolio** — read-only, global (deployment config).

## Global (app-shell `SharedControls`)

Horizon (`?h`), rollout count (`?n`), first seed (`?seed`), exogenous model (`?x`), chart scale
(`?scale`), currency display (`?fmt`) — one value for the whole set.

## Active selection

The scenario chips manage the set (add / rename / delete → the table columns) and pick the **active**
scenario, which drives the timeline editor + the rollout results below (histogram, selected-rollout
overlay, events, terminal table). The fan chart overlays Base + every variant at once regardless.

## Charts

- **Lines** (shipped): Base + each variant draw a solid median + dashed P5/P95, one color each,
  red-free palette (red is reserved for failed rollouts). A lone Base renders byte-identically to the
  pre-comparison chart (filled bands).
- **Candles** (later): grouped per checkpoint with a granularity picker (month / quarter / half /
  year). Needs the existing percentiles only (no backend change).
