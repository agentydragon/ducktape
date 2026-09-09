import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("actions_phone", { element: "#app", viewport: { width: 390, height: 1100 }, readySelectors: ["details"] });
