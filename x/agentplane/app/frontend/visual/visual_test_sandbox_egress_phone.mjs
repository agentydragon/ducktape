// The "sandbox" scenario of harness.tsx at a Pixel 6's CSS viewport: the app is used from a phone, so
// every page has to fit its width.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("sandbox_egress", {
  element: "#app",
  viewport: { width: 412, height: 915, deviceScaleFactor: 2.625 },
  outputName: "sandbox-egress-phone",
});
