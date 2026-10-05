import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// `runScenarios` ends the process, so the sweep runs in a child, as a Bazel target would run it.
// The page mounts, then throws from a timer, and the scene waits for a selector the page will
// never produce: only the page error can explain the failure, and nothing else will end the wait
// before WAIT_TIMEOUT_MS. The timer outlasts the page load, so the wait is already pending when the
// error lands; on a runner slow enough to load later, the sweep fails at its check before the wait
// instead, and the result is the same.
const root = mkdtempSync(join(process.env.TEST_TMPDIR ?? tmpdir(), "visual-test-lib-"));
const harnessDir = join(root, "harness");
mkdirSync(harnessDir);
writeFileSync(join(harnessDir, "index.html"), '<div id="app"></div><script src="./harness.js"></script>');
writeFileSync(
  join(harnessDir, "harness.js"),
  `document.getElementById("app").innerHTML = '<div id="shot"></div>';
setTimeout(() => { throw new Error("scene exploded"); }, 3000);`
);

const libUrl = new URL("./visual-test-lib.mjs", import.meta.url).href;
const sweep = `import { runScenarios } from ${JSON.stringify(libUrl)};
await runScenarios({ throws: { element: "#shot", readySelectors: [".never-arrives"] } }, { title: "Page error" });`;
const run = spawnSync(process.execPath, ["--input-type=module", "-e", sweep], {
  encoding: "utf8",
  env: {
    ...process.env,
    HARNESS_PATH: join(harnessDir, "harness.js"),
    TEST_UNDECLARED_OUTPUTS_DIR: join(root, "out"),
    // A `--test_filter` aimed at this test would otherwise filter the child's one scene away.
    TESTBRIDGE_TEST_ONLY: "",
  },
});

assert.equal(run.status, 1, run.stderr);
assert.match(run.stderr, /throws: uncaught page errors:\n\s+Error: scene exploded/);
assert.doesNotMatch(run.stderr, /Waiting for selector/);

console.log("visual-test-lib.test.mjs passed");
