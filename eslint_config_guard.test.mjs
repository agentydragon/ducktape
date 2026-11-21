import assert from "node:assert/strict";
import path from "node:path";
import process from "node:process";

import { ESLint } from "eslint";

const runfilesRoot = process.env.TEST_SRCDIR;
const workspaceName = process.env.TEST_WORKSPACE;
assert.ok(runfilesRoot, "Bazel provides the test runfiles root");
assert.ok(workspaceName, "Bazel provides the main workspace name");

const workspaceRoot = path.join(runfilesRoot, workspaceName);
const eslint = new ESLint({
  cwd: workspaceRoot,
  overrideConfigFile: path.join(workspaceRoot, "eslint.config.js"),
});
const source = 'export const lintProbe: string = "ok";';

for (const extension of ["ts", "tsx"]) {
  const [result] = await eslint.lintText(source, {
    filePath: path.join(workspaceRoot, `future-project/src/probe.${extension}`),
  });
  assert.ok(result, `ESLint returns a result for an unmatched .${extension} path`);
  assert.deepEqual(
    result.messages.map(({ ruleId }) => ruleId),
    ["no-restricted-syntax"]
  );
}

const [registeredResult] = await eslint.lintText(source, {
  filePath: path.join(workspaceRoot, "finance/augur/frontend/probe.tsx"),
});
assert.ok(registeredResult, "ESLint returns a result for a registered frontend path");
assert.equal(registeredResult.errorCount, 0);

const [sharedResult] = await eslint.lintText(source, {
  filePath: path.join(workspaceRoot, "aiquota/shared/probe.ts"),
});
assert.ok(sharedResult, "ESLint returns a result for a registered shared TypeScript path");
assert.equal(sharedResult.errorCount, 0);
