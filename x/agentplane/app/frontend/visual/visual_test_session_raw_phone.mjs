// The "session_raw" scenario of harness.tsx at a Pixel 6's CSS viewport: the frames are wide, so
// the phone is where a raw reading falls off the page if it is going to.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("session_raw", {
  element: "#app",
  viewport: { width: 412, height: 915, deviceScaleFactor: 2.625 },
  outputName: "session-raw-phone",
});
