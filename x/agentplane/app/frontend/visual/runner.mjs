// Every scenario in visual/scenarios.ts, swept on one browser per Bazel shard. The table is the
// only list: BUILD names a shard count, not a scenario, and `--test_filter=<scenario>` runs one.
import { runScenarios } from "../../../../../util/testing/frontend_visual/visual-test-lib.mjs";

import { SCENARIOS } from "./scenarios.js";

await runScenarios(SCENARIOS, { title: "Agentplane" });
