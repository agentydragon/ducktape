/**
 * Every visual scenario: what the sweep captures, and nothing about what it renders.
 *
 * The one list -- harness.jsx checks its own scenes against it, tests/visual_runner.mjs sweeps it,
 * and BUILD names no scenario. Plain `.mjs` because both readers need it and neither compiles it:
 * the harness is bundled by esbuild, the runner is run by node.
 *
 * `element` says which of the two kinds a scenario is, and is never defaulted: `#app` for a scene
 * whose real extent is the viewport, `#shot` for one whose subject is a single component, so its
 * crop is that component's own bounding box. See util/testing/frontend_visual/README.md.
 */
import { SYNC_SETTLED } from "../scene_ready.mjs";

/** The full app at the height its longest state needs. */
const FULL_APP = { element: "#app", viewport: { width: 1200, height: 1400 }, readySelectors: [SYNC_SETTLED] };

export const SCENARIOS = {
  main_page: FULL_APP,
  streak_rest: FULL_APP,
  active_bonus_countdown: FULL_APP,
  active_bonus_unlocked: FULL_APP,
  // ChangelogModal is a real `position: fixed; inset: 0` overlay in production, so its extent is
  // genuinely the viewport -- captured via #app, not #shot, and it seeds no sync state to wait on.
  changelog: { element: "#app", viewport: { width: 1200, height: 900 } },
  // The toast alone, shrink-wrapped by its own #shot box: the default viewport is irrelevant to
  // what lands in the PNG.
  session_award: { element: "#shot" },
};
