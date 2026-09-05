// Standalone ChangelogModal: unacked "what's new" entries shown on app open
// (harness scenario "changelog").

import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

// ChangelogModal is a genuine full-viewport `position: fixed` overlay in production, so this is a
// real full-page capture, not a single-element one — see harness.jsx.
await main("changelog", {
  element: "#app",
  viewport: { width: 1200, height: 900 },
});
