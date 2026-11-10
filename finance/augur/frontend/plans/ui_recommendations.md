# Augur frontend UI recommendations

Open UX follow-ups for the current scenario editor and results views.

## Scenario editor

- Consider default-collapsing advanced controls.
- **Safer reset.** Add confirmation or undo for resetting the active scenario.
- **Share scenario.** Add a Copy URL affordance near the scenario controls.
- **Persisted scenarios.** Save scenarios to localStorage.

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
