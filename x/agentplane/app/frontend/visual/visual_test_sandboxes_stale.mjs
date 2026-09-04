// Render-health and PR-visuals scenario "sandboxes_stale" of the harness in harness.tsx: the same
// list, served by a watch that has stopped cycling, so the page says its data is not being updated.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("sandboxes_stale", { element: "#app", viewport: { width: 1200, height: 900 } });
