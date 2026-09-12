// Render-health and PR-visuals scenario "new_sandbox" of the harness in harness.tsx: the launch form
// with a preset picked through the URL, its action policy sets dropdown open, nothing on the network.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("new_sandbox", {
  element: "#app",
  viewport: { width: 1200, height: 900 },
  outputName: "new-sandbox",
  // The scene is the form with the preset's pick taken and the sets dropdown opened by the harness.
  readySelectors: [".mantine-Pill-root", '[role="listbox"]'],
});
