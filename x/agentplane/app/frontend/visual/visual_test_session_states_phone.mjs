// The "session_states" scenario of harness.tsx at a Pixel 6's CSS viewport: the app is used from a
// phone, so every page has to fit its width. The existing nav/header chrome (its own decluttering
// is separately tracked) leaves little vertical room here, so the queued-input dot at the bottom
// falls off the page -- same as `session_raw_phone`, the desktop capture is where every state in
// this fixture is actually visible.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("session_states", {
  element: "#app",
  viewport: { width: 412, height: 915, deviceScaleFactor: 2.625 },
  outputName: "session-states-phone",
  captureViewport: true,
});
