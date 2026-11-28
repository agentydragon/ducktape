import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { parse } from "acorn";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const util = "v4/core/util.js";
const core = "v4/core/core.js";
const helper = "v4/core/format.mjs";
const dir = realpathSync(process.env.TEST_TMPDIR);
const pkg = path.dirname(createRequire(import.meta.url).resolve("zod/package.json"));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const source = (name) => readFileSync(path.join(pkg, name), "utf8");
const code = source(util);
assert.equal(sha(code), "bb08133b11b478c8e715038fff26ebea204d7fd5443fba31fe138b142d202efb");
const units = (text) => topLevelStatements(parse(text, { ecmaVersion: "latest", sourceType: "module" }));
const original = (id, name) => {
  const { node, start, end } = units(code)[id];
  assert.equal(node.declaration.id.name, name);
  return code.slice(start, end);
};
const [mapper, formatter] = [original(8, "joinValues"), original(45, "stringifyPrimitive")];
const [utilImport, coreImport] = [code, source(core)].map((text) => {
  const { start, end } = units(text).find(({ node }) => node.type === "ImportDeclaration");
  return text.slice(start, end);
});
assert.equal(utilImport, 'import { globalConfig } from "./core.js";');
assert.equal(coreImport, 'import { members as installMembers } from "./util.js";');

const write = (root, route, text) => {
  const file = path.join(root, route);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, text);
  return file;
};
const originalTree = path.join(dir, "source");
write(originalTree, "package.json", '{"type":"module"}\n');
write(
  originalTree,
  util,
  [
    utilImport,
    mapper,
    formatter,
    // These witnesses replace the host-dependent implementations while
    // retaining the original import names and the actual two shipped hoists.
    "export function members() { return globalConfig; }",
    "export const snapshot = globalConfig;",
    "",
  ].join("\n")
);
write(
  originalTree,
  core,
  [coreImport, 'export const globalConfig = "ready";', "export const duringInit = installMembers();", ""].join("\n")
);
const spec = write(
  dir,
  "spec.json",
  JSON.stringify({
    entry: util,
    modules: { [helper]: ["#1", "#2"], [util]: ["#0", "#3", "#4"] },
    order: [helper],
    sequential: true,
  })
);
const manifest = write(
  dir,
  "deployment.json",
  JSON.stringify({
    entry: util,
    node_esm_root: originalTree,
    chunks: [
      { path: util, source: path.join(originalTree, util), spec },
      { path: core, source: path.join(originalTree, core) },
    ],
  })
);
const out = path.join(dir, "out");
const request = { manifest, out, require: "all-outcomes" };
const emission = split(request);
assert.equal(emission.status, "certified", emission.log);
assert.equal(emission.scope, "node-hoist-cycle", emission.log);
assert.equal(emission.guarantee, "all-outcomes", emission.log);
assert.deepEqual(emission.files, ["package.json", core, helper, util]);
assert.deepEqual(emission.verdict.checked_roots, [util, core]);
assert.equal(emission.verdict.receipt.deployment.claim, "none");
const disk = split({ ...request, checkOutput: true });
assert.equal(disk.status, "certified", disk.log);
assert.deepEqual(disk.verdict.checked_roots, [util, core]);
assert.equal(disk.verdict.receipt.operation, "checked-output");
for (const route of [core, "package.json"]) {
  assert.deepEqual(readFileSync(path.join(out, route)), readFileSync(path.join(originalTree, route)));
}

const observer = `
import * as originalNamespace from "./v4/core/util.js";
import { snapshot, members, joinValues } from "./v4/core/util.js";
import { duringInit } from "./v4/core/core.js";
const repeated = await import("./v4/core/util.js");
if (repeated !== originalNamespace || repeated.joinValues !== joinValues) throw new Error("namespace or export changed");
const receiver = { map(callback) { return { join(sep) { return callback(7) + sep; } }; } };
console.log(JSON.stringify({
  names: Object.keys(repeated), duringInit, current: members(), snapshot, mapped: joinValues(receiver),
}));
`;
const positive = {
  names: ["joinValues", "members", "snapshot", "stringifyPrimitive"],
  duringInit: "ready",
  current: "ready",
  snapshot: "ready",
  mapped: "7|",
};
for (const tree of [originalTree, out]) {
  const observed = execFileSync(process.execPath, ["--input-type=module", "-e", observer], {
    cwd: tree,
    encoding: "utf8",
  });
  assert.deepEqual(JSON.parse(observed), positive);
  // Core as the first root visits util before globalConfig initializes.
  const tdz = spawnSync(process.execPath, [path.join(tree, core)], { encoding: "utf8" });
  assert.equal(tdz.status, 1, tdz.stderr);
  assert.match(tdz.stderr, /ReferenceError: Cannot access 'globalConfig' before initialization/);
}
const edited = path.join(dir, "edited");
cpSync(out, edited, { recursive: true });
const emittedUtil = readFileSync(path.join(out, util), "utf8");
const edge = units(emittedUtil).find(
  ({ node }) => node.type === "ImportDeclaration" && node.source.value === "./core.js"
);
const [binding] = edge.node.specifiers;
assert.equal(binding.imported.name, "globalConfig");
const wrong = emittedUtil.slice(0, binding.start) + "duringInit as globalConfig" + emittedUtil.slice(binding.end);
write(edited, util, wrong);
// Both files still exist and the import still resolves, but members now
// reads the cell currently being initialized. The previously good root fails.
const badRun = spawnSync(process.execPath, ["--input-type=module", "-e", observer], {
  cwd: edited,
  encoding: "utf8",
});
assert.equal(badRun.status, 1, badRun.stderr);
assert.match(badRun.stderr, /ReferenceError/);
const tampered = split({ ...request, out: edited, checkOutput: true });
assert.equal(tampered.status, "refused", tampered.log);
assert.match(tampered.reason, /output module v4\/core\/util\.js differs from its generated ESTree/);

// Rerun the two-file request on the intact shipped files and package scopes.
const shipped = path.join(dir, "shipped");
for (const route of ["package.json", "v4/package.json", "v4/core/package.json", util, core]) {
  write(shipped, route, readFileSync(path.join(pkg, route)));
}
function checkShipped(label, root, target, dependency, owner) {
  const spec = write(
    dir,
    label + "-spec.json",
    JSON.stringify({
      entry: target,
      modules: {
        [owner]: ["#8", "#45"],
        [target]: units(code).flatMap((_, i) => (i === 8 || i === 45 ? [] : ["#" + i])),
      },
      order: [owner],
      sequential: true,
    })
  );
  const manifest = write(
    dir,
    label + "-manifest.json",
    JSON.stringify({
      entry: target,
      node_esm_root: root,
      chunks: [
        { path: target, source: path.join(root, target), spec },
        { path: dependency, source: path.join(root, dependency) },
      ],
    })
  );
  const got = split({ ...request, manifest, out: path.join(dir, label) });
  assert.equal(got.reasonCode, "no_constructed_rule", got.log);
  return got.verdict;
}
const packageRoot = checkShipped("package-root", shipped, util, core, helper);
assert.match(packageRoot.reason, /nested package scope is unsupported: v4\/core\/util\.js/);
const innerRoot = checkShipped("core-root", path.join(shipped, "v4/core"), "util.js", "core.js", "format.mjs");
assert.match(innerRoot.reason, /unsupported global/);

console.log(
  JSON.stringify({
    source: { ordinals: [8, 45], sha256: sha(code) },
    projection: { emission: emission.verdict, disk: disk.verdict },
    tampered: tampered.verdict,
    shipped: { packageRoot, innerRoot },
  })
);
