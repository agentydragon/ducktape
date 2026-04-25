// Visual regression test for the Study Casino main page. Renders the
// React app inside the visual harness (mocked /state + /events), captures
// a screenshot, and pixel-diffs against
// tests/baselines/main_page-chromium-linux.png.
//
// Run:    bazel test //x/auragon_study_casino/frontend:visual_main_page
// Update: bazel run  //x/auragon_study_casino/frontend:visual_main_page -- --update

import { main } from "../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("main_page", import.meta.url, {
  viewport: { width: 1200, height: 1400 },
  // Wait for fonts and the post-mount layout to settle. The harness disables
  // CSS animations, so a short delay is enough.
  waitMs: 250,
});
