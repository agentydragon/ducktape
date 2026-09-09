import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("consent_reconnect", {
  element: "#app",
  viewport: { width: 1200, height: 1300 },
  readySelectors: ["[data-reconnect-review]"],
});
