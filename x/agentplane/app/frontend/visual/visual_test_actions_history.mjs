// Render-health and PR visual for the operator Action history surface.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("actions_history", {
  element: "#app",
  viewport: { width: 1200, height: 1400 },
  readySelectors: ["details"],
});
