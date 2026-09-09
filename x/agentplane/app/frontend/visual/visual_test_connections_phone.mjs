import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("connections_phone", {
  element: "#app",
  viewport: { width: 390, height: 1100 },
  readySelectors: ["[data-connection-id]"],
});
