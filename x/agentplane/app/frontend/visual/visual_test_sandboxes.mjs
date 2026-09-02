// Render-health and PR-visuals scenario "sandboxes" of the harness in harness.tsx: the app mounted on
// canned inventory and runner events, nothing on the network.
import { main } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

await main("sandboxes", { element: "#app", viewport: { width: 1200, height: 900 } });
