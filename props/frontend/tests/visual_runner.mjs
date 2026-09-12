// Every scenario in tests/harness/scenarios.mjs, swept on one browser per Bazel shard. BUILD names
// a shard count, not a scenario, and `--test_filter=<scenario>` runs one.
import { runScenarios } from "../../../util/testing/frontend_visual/visual-test-lib.mjs";

import { SCENARIOS } from "./harness/scenarios.mjs";

// `#shot` for every scenario, because it is a property of this harness rather than of a scenario:
// harness.ts mounts whatever component a page names into a `#shot` div it builds itself, so the
// crop is always that component's own bounding box. A scenario needing different capture options
// turns this array into a name-to-overrides map.
const scenarios = Object.fromEntries(SCENARIOS.map((name) => [name, { element: "#shot" }]));

await runScenarios(scenarios, { title: "Props frontend" });
