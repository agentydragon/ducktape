# Excalidraw browser smoke follow-ups

The public corpus, accepted spec and self-hosted `test_smoke` target are implemented. See
<../excalidraw/README.md> for their current inputs and behavior.

## Remaining coverage

- Exercise post-startup `import()` chunks for locales, font subsetting and file-system
  features. The current smoke covers startup and drawing only.
- Capture a representative editor state through `VisualPage.capture()` so the PR can
  publish a screenshot.

## Current corpus boundary

The current Excalidraw spec has no `wrapper_shape` entries for `named_from_default`,
`named_from_module_default` or `named_from_json_default`. The engine supports these
shapes, but this smoke does not exercise them. If a future bundle pin introduces one,
add a corresponding case to the accepted spec and smoke.

## Regression routing

When a private-corpus debundler issue is tractable on Excalidraw too, prefer a regression
test here or a smaller e2e under `devinfra/js/debundle/e2e/` so public CI covers it
without private source.
