// Active session past the daily-bonus threshold: strip flips to "unlocked"
// and the live estimate includes the bonus at the post-qualification
// multiplier (harness scenario "active_bonus_unlocked").

import { main } from "../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("active_bonus_unlocked", import.meta.url, {
  viewport: { width: 1200, height: 1400 },
  waitMs: 250,
});
