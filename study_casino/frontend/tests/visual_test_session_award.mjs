// Standalone SessionAwardToast: server-computed award breakdown shown after
// completing a session (harness scenario "session_award").

import { main } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("session_award", {
  element: "#shot",
});
