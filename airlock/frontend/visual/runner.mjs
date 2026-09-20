// Every scenario in visual/scenarios.mjs, swept on one browser per Bazel shard.
import { runScenarios } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

import { SCENARIOS } from "./scenarios.mjs";

await runScenarios(SCENARIOS, { title: "Airlock" });
