## Verifying a visual change — see the rendered image, not just a passing test

A passing `bbr test //x/agentplane/app/frontend:visual` only proves every scene mounted
without an uncaught error — it says nothing about whether it looks right (there are no checked-in
pixel baselines here; see `util/testing/frontend_visual/README.md`). Before calling a visual
change (`session.tsx`, `session.css`, or any other component/stylesheet here) done, actually look
at a rendered PNG of every state you touched. Either of these satisfies that — the point is seeing
the real pixels, not a specific mechanism for getting there:

- **Wait for CI's `pr-visuals` comment** on the PR: it renders every `visual_*` scenario and posts
  before/after/diff thumbnails automatically once pushed.
- **Or run the scenario locally** — `bbr test //x/agentplane/app/frontend:visual
--test_filter=<scenario> --noremote_accept_cached --nocache_test_results` — and download the PNG it
  writes to the test's
  undeclared outputs (`buildbuddy_api` skill: `bbapi artifact download <invocation-id>
"<scenario>-actual.png"`), then view it.

A local interactive browser session (running the app, clicking through it by hand) is neither
required nor the goal here — it's extra machinery for the same answer a screenshot already gives.
Reach for it only when a _static_ screenshot genuinely can't show what changed (verifying motion
itself, not an animation's start/end frames — which visual tests disable anyway).

## Adding a scenario

`visual/scenarios.ts` is the whole list — a row there is a target's worth of coverage, and BUILD
names no scenario at all (it carries a `shard_count`, which needs no change when the table grows).
A row needs a route and a viewport; `outputName`, `readySelectors`, `captureViewport` and the
fixture switches are per-row overrides.

If a state you changed isn't exercised by any existing scenario/fixture (`visual/harness.tsx`), add
one rather than skipping the check — see `session_states` for the pattern (a fixture built
specifically to exercise states the main fixture doesn't produce). Many states are reachable via a
URL param in the row's `route` (`?raw=1`, `?reasoning=<item-id>`) rather than a simulated click —
check `scenarios.ts` before assuming a new state needs click-simulation infrastructure this app's
`visual-test-lib.mjs`-based tests don't have (unlike `haku/console`'s own `render.mjs`).
