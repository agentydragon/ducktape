// Visual regression test for the Study Casino main page. Renders the
// React app inside the visual harness (mocked /state + /events), captures
// a screenshot, and publishes it for PR visual review (no checked-in baseline).
//
// Run:    bazel test //study_casino/frontend:visual_main_page

import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";
import { SYNC_SETTLED } from "./scene_ready.mjs";

await main("main_page", {
  element: "#app",
  viewport: { width: 1200, height: 1400 },
  readySelectors: [SYNC_SETTLED],
});
