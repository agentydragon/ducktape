// Active session pre-threshold: strip shows the daily-bonus countdown
// (harness scenario "active_bonus_countdown").

import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";
import { SYNC_SETTLED } from "./scene_ready.mjs";

await main("active_bonus_countdown", {
  element: "#app",
  viewport: { width: 1200, height: 1400 },
  readySelectors: [SYNC_SETTLED],
});
