- Construct `VisualPage` before navigation so it records page-load errors.
- Captures check render and network health; tests own app readiness and interactions.
- `.capture.json` sidecars are diagnostics, not visual-review assets or baselines.
- `VisualPage.capture()` review assets go in the root `TEST_UNDECLARED_OUTPUTS_DIR`
  with unique names: the publisher reads only the root `visual-review.json`.
- Nontrivial injected browser scripts live in linted `.js` files declared as
  runfiles, not embedded in Python.
- **Gotcha:** Playwright passes route callbacks `(route, request)` positionally,
  trimmed to the callback's signature, so a loop lambda `lambda route, gate=gate`
  receives `request` as `gate`. Use a factory returning a one-argument
  `handler(route)` closing over the gate.
