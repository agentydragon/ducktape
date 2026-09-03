// Render-health and PR-visuals scenario "session_reasoning" of the harness in harness.tsx: the
// session view with the reasoning blocks expanded, which the "session" scenario only ever shows
// folded.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("session_reasoning", {
  element: "#app",
  viewport: { width: 1200, height: 900 },
  outputName: "session-reasoning",
});
