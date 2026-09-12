import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("actions_history_phone", {
  element: "#app",
  viewport: { width: 390, height: 1400 },
  readySelectors: ["details"],
});
