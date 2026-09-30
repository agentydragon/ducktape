import { runScenarios } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

import { SCENARIOS } from "./scenarios.mjs";

await runScenarios(SCENARIOS, { title: "Claude session viewer" });
