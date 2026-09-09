import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("connections", {
  element: "#app",
  viewport: { width: 1200, height: 1000 },
  readySelectors: ["[data-connection-id]"],
});
