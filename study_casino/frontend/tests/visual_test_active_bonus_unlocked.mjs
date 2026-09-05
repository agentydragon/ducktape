// Active session past the daily-bonus threshold: strip flips to "unlocked"
// and the live estimate includes the bonus at the post-qualification
// multiplier (harness scenario "active_bonus_unlocked").

import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";
import { SYNC_SETTLED } from "./scene_ready.mjs";

await main("active_bonus_unlocked", {
  element: "#app",
  viewport: { width: 1200, height: 1400 },
  readySelectors: [SYNC_SETTLED],
});
