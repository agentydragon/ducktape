# Augur frontend — UI recommendations

Captured 2026-05-28 from a review of `app.jsx`, `forms.jsx`, and the two
screenshots under `__screenshots__/`. Ranked by likely payoff. Delete entries as
they land; tombstone the file once empty.

## Form sidebar (biggest single issue)

Every `augur-eyebrow` block (Scenario, Spending, Portfolio, Property purchase,
Taxes, Funding, Trigger below, Sell amount, Private equity tenders, Sampling…)
renders as a flat list at the same visual weight. From the screenshot the column
overruns the chart height.

1. **Sectional hierarchy + collapse.** Group into top-level cards ("Scenario",
   "Property", "Funding & exits", "Advanced") and make Advanced (Sampling, PE
   tracker, Lane floor) collapsed by default. The existing `<details>`-with-
   chevron pattern in `forms.jsx` (Sampling) is the right primitive — Mantine's
   `Accordion` is also fine. Portfolio is already collapsed (done 2026-05-28).
2. **Reset isn't reversible.** "Reset form" at the bottom destroys user edits
   with no undo. Add a confirmation, or split into "Undo last change" + "Reset
   to defaults" as separate actions.
3. **Share-this-scenario button.** State already round-trips through the URL
   (`scenarioSetToSearch`), but there's no affordance to copy the link. Add a
   "Copy URL" button next to Reset — this is the natural unit of work for a
   scenario explorer.
4. **Persisted scenarios + side-by-side.** Saving a scenario into localStorage
   and comparing two on the same chart is a major lift but is the headline
   feature for a tool like this. Even a "pin current as baseline → overlay
   future runs" mode would be useful.

## Results pane

5. **Stat cards are bare** (`app.jsx:321-336`). "Median terminal net worth
   $1,239,238" with no anchor. Two cheap upgrades: show **Δ vs. starting net
   worth** ("+$390k") and inline a tiny sparkline of the histogram inside the
   card so the user sees spread at a glance.
6. **Failed-rollouts card is dead when 0 and uninformative when not.** When
   `failedCount > 0`, make it clickable, jump to a failed seed, and surface the
   failure mode. Currently you have to know to scroll.
7. **Implied carrying cost as you tune.** The property panel collects price,
   down payment, rate, insurance %, maintenance %, HOA, taxes — but never shows
   the resulting **monthly carrying cost** while editing. That's the number the
   user is implicitly converging toward. A small "Est. monthly: $X
   (PITI+HOA+maint)" line under the Property purchase section would short-
   circuit a lot of context switching.

## Inputs

8. **More `description=` hints (same pattern as Insurance/Maintenance).** Round
   1 landed LNW floor, Management fee, Leasing fee, Avg tenancy, Closing cost.
   Reverted as noise on Fraction rented / Vacancy / Trigger below / Sell amount
   — when label + suffix already conveys it, the description just bulks the
   sidebar. Still candidate: anything in Sampling that grows beyond `Rollouts` /
   `First seed`.
9. **Shorthand entry on currency inputs.** `"1.4k"` / `"850k"` / `"1.2m"`
   parsing for monthly spend and amounts. Mantine's `NumberInput` supports a
   `parser` prop.
10. **Reasonable-range hint.** Insurance and Maintenance allow up to 10% — that's
    miles outside realistic. A tick or muted "typical 0.3–0.7%" under the field,
    or a slider with snap, would orient first-time users.

## Lifecycle events

11. **Collapse to one-line summaries when not focused.** `LifecycleEventRow`
    always renders the full grid. If there are 6+ events the column gets very
    tall. Render a row as `Month 12: change rented → 0%` and expand on click
    for edit.

## Header / nav

12. **Dark mode toggle.** `MantineProvider defaultColorScheme="auto"` is set,
    but no in-app override is visible. Useful for screenshots and for users
    whose OS doesn't match their preference.
13. **Keyboard shortcuts.** Power users tweak and re-run repeatedly: `R` rerun,
    `[` / `]` prev/next seed, `1`-`9` select metric.

## Done

- 2026-05-28: Insurance / Maintenance fields moved the "% of purchase price"
  hint into a per-field `description` (`forms.jsx:144-162`).
- 2026-05-28: Portfolio section collapsed by default with a grand-total teaser
  in the summary (`forms.jsx:555+`), and the open table regrouped into Cash /
  Public / Private sections with inline subtotals + grand Total in the footer.
- 2026-05-28: Monthly spend now sits side-by-side with its index picker (same
  responsive grid as Trigger below + Sell amount).
- 2026-05-28: PE-tenders blurb folded into the LNW floor field's `description`.
- 2026-05-28: `description` hints added to Management fee, Leasing fee, Avg
  tenancy, Closing cost (and earlier Insurance / Maintenance / LNW floor).
  Sell amount / Trigger below / Fraction rented / Vacancy tried then reverted
  as noise.
