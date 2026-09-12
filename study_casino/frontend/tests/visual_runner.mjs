// Every scenario in tests/harness/scenarios.mjs, swept on one browser per Bazel shard. BUILD names
// a shard count, not a scenario, and `--test_filter=<scenario>` runs one.
import { runScenarios } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

import { SCENARIOS } from "./harness/scenarios.mjs";

await runScenarios(SCENARIOS, { title: "Study Casino" });
