// Render-health and PR-visuals scenario "sandbox_policy" of the harness in harness.tsx: the app mounted on
// canned inventory and runner events, nothing on the network.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("sandbox_policy", {
  element: "#app",
  viewport: { width: 1200, height: 1100 },
  outputName: "sandbox-policy",
});
