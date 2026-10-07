## Verifying a visual change — see the rendered image, not just a passing test

A passing `bbr test //agentplane/app/frontend:visual` only proves every scene mounted
without an uncaught error — it says nothing about whether it looks right (there are no checked-in
pixel baselines here; see `util/testing/frontend_visual/README.md`). Before calling a visual
change (`threads/projected_session.tsx`, `threads/projected_session.css`, or any other component/stylesheet here) done, actually look
at a rendered PNG of every state you touched. Either of these satisfies that — the point is seeing
the real pixels, not a specific mechanism for getting there:

- **Wait for CI's `pr-visuals` comment** on the PR: it renders every visual scenario and posts
  before/after/diff thumbnails automatically once pushed.
- **Or run the scenario locally** — `bbr test //agentplane/app/frontend:visual
--test_filter=<scenario-or-test-name> --noremote_accept_cached --nocache_test_results` — and download the PNG it
  writes to the test's
  undeclared outputs (`buildbuddy_api` skill: `bbapi artifact list <invocation-id>` for the exact
  name, which is the scenario's `outputName` or its key, then `bbapi artifact download <invocation-id>
"<name>-actual.png"`), then view it. A failure in another shard: `bbapi target log <invocation-id>
visual --failed`.

A local interactive browser session (running the app, clicking through it by hand) is neither
required nor the goal here — it's extra machinery for the same answer a screenshot already gives.
Reach for it only when a _static_ screenshot genuinely can't show what changed (verifying motion
itself, not an animation's start/end frames — which visual tests disable anyway).

## Adding a scenario

The fixture tables are `harness/scenarios.json` for ordinary mounts and
`harness/interaction_scenarios.json` for states driven by named tests in
`test_visual_interactions.py`. The TypeScript harness reads both to choose the route and canned data;
the Python tests own clicks, scrolls, assertions, and capture. BUILD names no individual scenario.
Filter ordinary scenes by JSON scenario name, and interaction scenes by Python test function name
(plus parametrized case ID when needed), for example `--test_filter=test_grant_selector_hides_picked_option`.
A row needs an `element`, a `route` and a `viewport`; `outputName`, `readySelectors`,
`captureViewport` and the fixture switches are per-row overrides. The sweep reads the capture fields
(`util/testing/visual_scenarios.py`) and the harness reads the route and fixture switches (`Scenario`
in `harness/scenario.ts`). JSON has no shared constants, so every phone row spells out a Pixel 6's
CSS viewport (`{ "width": 412, "height": 915, "deviceScaleFactor": 2.625 }`). `readySelectors` are
Playwright selectors (`:text("...")`). Heights are deliberate: a
thread's history follows its bottom, so a row's viewport must be tall enough to keep the card under
test, and the input above it, in frame.

If a state you changed isn't exercised by any existing scenario/fixture (`harness/harness.tsx`), add
one rather than skipping the check — see `session_states` for the pattern (a fixture built
specifically to exercise states the main fixture doesn't produce). Put interaction behavior in
`test_visual_interactions.py`; the TypeScript harness owns fixture data and mounting. Some older
scenes still use harness switches for clicks (`openReasoning`, `openEvidence`, `openDebug`, …);
do not add another switch for an interaction that Playwright can perform.
