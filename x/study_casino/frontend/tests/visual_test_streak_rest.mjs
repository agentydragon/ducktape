// Streak strip with a banked rest day + unclaimed daily bonus (harness
// scenario "streak_rest"). See visual_test_main_page.mjs for run/update flow.

import { main } from "../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("streak_rest", {
  element: "#app",
  viewport: { width: 1200, height: 1400 },
  waitMs: 250,
});
