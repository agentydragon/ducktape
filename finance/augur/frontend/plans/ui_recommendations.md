# Augur frontend UI recommendations

Captured 2026-05-28 from a review of `app.jsx`, `forms.jsx`, and the
then-checked-in visual-test screenshots (now published per-PR via
`devinfra/pr_visuals` instead). This file is now only the active UX
backlog; delete entries as they land and tombstone the file once empty.

## Form Sidebar

1. **Sectional hierarchy and collapse.** Group the flat scenario sidebar into
   top-level sections such as Scenario, Property, Funding and exits, and
   Advanced. The existing `<details>` pattern is enough; Mantine Accordion is
   also fine. Advanced controls should default collapsed.
2. **Safer reset.** Reset currently destroys edits with no recovery. Add
   confirmation, or split it into Undo last change and Reset to defaults.
3. **Share scenario.** State already round-trips through the URL. Add a Copy URL
   affordance near reset/rerun controls.
4. **Persisted scenarios and comparison.** Save scenarios to localStorage and
   support a pinned baseline or side-by-side overlay.

## Results Pane

5. **Richer stat cards.** Show delta vs starting net worth and a compact
   distribution hint alongside each headline statistic.
6. **Actionable failed rollouts.** When `failedCount > 0`, make the card jump to
   a failing seed and show the failure mode.
7. **Implied carrying cost while editing.** The property panel collects enough
   inputs to show estimated monthly PITI plus HOA and maintenance.

## Inputs

8. **More descriptions only where they reduce ambiguity.** Use the existing
   `description=` pattern for fields that need context, but avoid adding bulk
   where label and suffix are already clear.
9. **Shorthand currency entry.** Accept `1.4k`, `850k`, and `1.2m` for spend and
   amount inputs.
10. **Reasonable-range hints.** Insurance and maintenance percentages allow
    unrealistic values; add muted typical ranges or constrained sliders.

## Lifecycle Events

11. **Collapsed row summaries.** Render lifecycle events as one-line summaries
    until focused, then expand for editing.

## Header and Nav

12. **Dark-mode override.** `MantineProvider defaultColorScheme="auto"` is set,
    but there is no visible in-app override.
13. **Keyboard shortcuts.** Add shortcuts for rerun, previous/next seed, and
    metric selection.
