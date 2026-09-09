import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("consent_reconnect_phone", {
  element: "#app",
  viewport: { width: 390, height: 1600 },
  readySelectors: ["[data-reconnect-review]"],
});
