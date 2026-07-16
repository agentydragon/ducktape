// Standalone ChangelogModal: unacked "what's new" entries shown on app open
// (harness scenario "changelog").

import { main } from "../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("changelog", import.meta.url, {
  viewport: { width: 1200, height: 900 },
  waitMs: 250,
});
