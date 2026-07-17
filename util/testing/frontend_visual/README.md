# frontend_visual

Shared Puppeteer/Playwright infrastructure for per-scenario visual render-health
tests (see `visual-test-lib.mjs` for the JS/Puppeteer path used by
`x/study_casino/frontend` and `props/frontend`, and `frontend_visual.py` for the
Python/Playwright path used by `x/study_casino/tests` and `finance/augur`).

There are no checked-in pixel baselines: these tests gate render health (the
harness loads, the scenario mounts, zero uncaught page errors) and publish the
rendered PNG for PR visual review instead — see
`devinfra/pr_visuals/plans/goldens_to_pr_visuals.md`.

## Screenshot target: element, not viewport

`visual-test-lib.mjs`'s `main()` takes a required `element` CSS selector — there
is no default, so every scenario states explicitly which of the two cases it is:

- **`element: "#app"`** — the scenario is a genuine full page or full app (nav,
  header, the works). A full-page/viewport-shaped screenshot is the correct
  capture here.
- **`element: "#shot"`** (by convention) — the scenario's real subject is a
  single component (a toast, a modal, a chart, a card) pulled out of its normal
  page for isolated testing. The harness must mount it inside a wrapper with
  that id, sized to the component's own content, and the test screenshots only
  that wrapper.

Getting this wrong looks like: wrapping a small component in an artificial
`minHeight: "100vh"` box and hand-picking a viewport size to roughly match it,
then screenshotting the whole page. That produces a PNG that's mostly empty
background, and the "roughly match it" viewport is a guess that goes stale the
first time the component's real size changes. See
<https://github.com/agentydragon/ducktape/pull/3343> for the original instance
of this bug and fix.

If the single component has a real production container that owns its width
(a dashboard grid cell, a fixed-width panel column), render it inside that
actual container/class in the harness — not a synthetic hardcoded width — so
the screenshot tracks the true CSS instead of a number that can drift from it.
