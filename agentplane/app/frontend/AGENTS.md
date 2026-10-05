## Verifying a visual change — see the rendered image, not just a passing test

A passing `bbr test //agentplane/app/frontend:visual` only proves every scene mounted
without an uncaught error — it says nothing about whether it looks right (there are no checked-in
pixel baselines here; see `util/testing/frontend_visual/README.md`). Before calling a visual
change (`threads/projected_session.tsx`, `threads/projected_session.css`, or any other component/stylesheet here) done, actually look
at a rendered PNG of every state you touched. Either of these satisfies that — the point is seeing
the real pixels, not a specific mechanism for getting there:

- **Wait for CI's `pr-visuals` comment** on the PR: it renders every `visual_*` scenario and posts
  before/after/diff thumbnails automatically once pushed.
- **Or run the scenario locally** — `bbr test //agentplane/app/frontend:visual
--test_filter=<scenario> --noremote_accept_cached --nocache_test_results` — and download the PNG it
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

`harness/scenarios.json` is the whole list — a row there is a target's worth of coverage, and BUILD
names no scenario at all (it carries a `shard_count`, which needs no change when the table grows).
A row needs an `element`, a `route` and a `viewport`; `outputName`, `readySelectors`,
`captureViewport` and the fixture switches are per-row overrides. The sweep reads the capture fields
(`util/testing/visual_scenarios.py`) and the harness the route and the fixture switches (`Scenario`
in `harness/scenario.ts`). JSON has no shared constants, so every phone row spells out a Pixel 6's
CSS viewport (`{ "width": 412, "height": 915, "deviceScaleFactor": 2.625 }`). `readySelectors` are
Playwright selectors (`:text("...")`, not Puppeteer's `::-p-text(...)`). Heights are deliberate: a
thread's history follows its bottom, so a row's viewport must be tall enough to keep the card under
test, and the input above it, in frame.

If a state you changed isn't exercised by any existing scenario/fixture (`harness/harness.tsx`), add
one rather than skipping the check — see `session_states` for the pattern (a fixture built
specifically to exercise states the main fixture doesn't produce). A state behind a click (an open
run, a row's evidence, the debug drawer) is a row switch on `Scenario` (`openReasoning`,
`openEvidence`, `openDebug`, …) that `harness/harness.tsx` acts on once the view mounts — check the
existing switches before adding one.
