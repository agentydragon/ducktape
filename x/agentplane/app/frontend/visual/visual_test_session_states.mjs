// Render-health and PR-visuals scenario "session_states" of the harness in harness.tsx: every
// status the badge-to-dot restyle touches that the main "session" scenario doesn't produce on its
// own -- a failed tool call, a streaming run, and a message queued mid-turn.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("session_states", {
  element: "#app",
  viewport: { width: 1200, height: 900 },
  outputName: "session-states",
  captureViewport: true,
});
