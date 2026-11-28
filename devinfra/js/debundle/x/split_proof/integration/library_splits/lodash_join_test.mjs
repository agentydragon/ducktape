import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import { sha256 } from "./source_ownership.mjs";

const nodeOnly = process.argv.includes("--node-only");
const require = createRequire(import.meta.url);
const publish = "join.js";
const helper = "join-implementation.mjs";
const caller = new URL("./lodash_join/caller.mjs", import.meta.url);
const oracle = fileURLToPath(new URL("./lodash_join/oracle.mjs", import.meta.url));
const spec = fileURLToPath(new URL("./lodash_join/split.json", import.meta.url));
const manifest = realpathSync(require.resolve("lodash-es/package.json"));
const source = realpathSync(require.resolve("lodash-es/" + publish));
assert.equal(path.dirname(source), path.dirname(manifest));
const packageBytes = readFileSync(manifest);
const bytes = readFileSync(source);
assert.equal(bytes.length, 691);
assert.equal(sha256(bytes), "2f791dd3d33d7033f23af1bcb523df3910f26ed52a4eda7a098e0e30ca55496d");
assert.equal(packageBytes.length, 724);
assert.equal(sha256(packageBytes), "83d054ea84cf0efe4db931cf24c4ca9dde533a0e49779d26176be9d43061c903");
const { name, version, type } = JSON.parse(packageBytes);
assert.deepEqual([name, version, type], ["lodash-es", "4.18.1", "module"]);

const code = bytes.toString("utf8");
const parse = (text) => parseModule(text, acorn.parse, eslintScope);
const parsed = parse(code);
const rows = topLevelStatements(parsed.ast);
assert.deepEqual(
  rows.map(({ node }) => node.type),
  ["VariableDeclaration", "VariableDeclaration", "FunctionDeclaration", "ExportDefaultDeclaration"]
);
assert.deepEqual(
  parsed.scopes.globalScope.through.map(({ identifier }) => identifier.name),
  ["Array"]
);
const [prototype, save, fn, snapshot] = rows.map(({ node }) => node);
assert.equal(prototype.declarations[0].init.property.name, "prototype");
assert.equal(save.declarations[0].init.property.name, "join");
const dispatch = fn.body.body[0].argument.alternate;
assert.equal(dispatch.callee.object.name, "nativeJoin");
assert.equal(dispatch.callee.property.name, "call");
assert.equal(snapshot.declaration.name, fn.id.name);

function probeSource() {
  const [sourceShape] = JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      input:
        JSON.stringify([
          {
            kind: "sourceClassification",
            input: {
              source: dumpParsed(parsed, { includeOpaqueCaptures: true }),
              sourceText: code,
              entry: publish,
            },
          },
        ]) + "\n",
      encoding: "utf8",
    })
  );
  assert(sourceShape.output || sourceShape.error);
  return sourceShape.error ?? "decoded";
}
const dir = process.env.TEST_TMPDIR;
const makeRoot = (name) => {
  const root = path.join(dir, name);
  mkdirSync(root);
  copyFileSync(manifest, path.join(root, "package.json"));
  copyFileSync(caller, path.join(root, "caller.mjs"));
  return root;
};
const observe = (root) => JSON.parse(execFileSync(process.execPath, [oracle, root], { encoding: "utf8" }));
const original = makeRoot("original");
copyFileSync(source, path.join(original, publish));
const before = observe(original);
const returned = (value) => ({ ending: "returned", value });
assert.deepEqual(before, {
  exports: ["default"],
  publicIdentity: true,
  name: "join",
  arity: 2,
  normal: returned("a~b~c"),
  defaultSeparator: returned("a,b"),
  nullish: [returned(""), returned("")],
  ending: "threw",
  result: null,
  identicalThrown: true,
  writes: 11,
  events: [
    "call:get",
    "call:invoke",
    "length:this",
    "first-slot:this",
    "first:this",
    "second-slot:this",
    "second:this",
  ],
  replacements: 0,
  receiver: true,
  functionReceiver: true,
  separatorPassed: true,
});

const request = {
  source: path.join(original, publish),
  spec,
  out: path.join(dir, "strict"),
  require: "all-outcomes",
};
const result = nodeOnly ? null : split(request);
let disk = null;
let emittedNode = null;
function checkLayout(root) {
  const read = (file) => readFileSync(path.join(root, file), "utf8");
  const ownerCode = read(helper);
  const entryCode = read(publish);
  const actual = topLevelStatements(parse(ownerCode).ast);
  assert.deepEqual(
    actual.slice(0, 3).map(({ start, end }) => ownerCode.slice(start, end)),
    rows.slice(0, 3).map(({ start, end }) => code.slice(start, end))
  );
  const publishRow = parse(entryCode).ast.body.find(({ type }) => type === "ExportDefaultDeclaration");
  assert(publishRow);
  assert.equal(entryCode.slice(publishRow.start, publishRow.end), code.slice(snapshot.start, snapshot.end));
}
if (result?.status === "certified") {
  assert.equal(result.guarantee, "all-outcomes");
  disk = split({ ...request, checkOutput: true });
  assert.equal(disk.status, "certified", disk.log);
  assert.equal(disk.guarantee, result.guarantee);
  assert.equal(disk.scope, result.scope);
  checkLayout(request.out);
  const deployed = makeRoot("cli-with-original-package");
  for (const file of result.files) {
    if (file === "package.json") assert.deepEqual(readFileSync(path.join(request.out, file)), packageBytes);
    copyFileSync(path.join(request.out, file), path.join(deployed, file));
  }
  emittedNode = observe(deployed);
  assert.deepEqual(emittedNode, before);
} else if (result) {
  assert.equal(result.reasonCode, "no_constructed_rule", result.log);
}

// A concrete reference for the exact placement, used only as a Node oracle.
// It keeps the complete original prefix (including comments) and the
// executable default in its original public route.
const reference = makeRoot("reference");
const body = code.slice(0, snapshot.start);
const entry = `import { join } from "./${helper}";\n` + code.slice(snapshot.start);
writeFileSync(path.join(reference, helper), body + "export { join };\n");
writeFileSync(path.join(reference, publish), entry);
checkLayout(reference);
const referenceNode = observe(reference);
assert.deepEqual(referenceNode, before);

const wrongFormat = path.join(dir, "wrong-format");
cpSync(reference, wrongFormat, { recursive: true });
writeFileSync(path.join(wrongFormat, "package.json"), '{"type":"commonjs"}\n');
const formatCheck = spawnSync(process.execPath, ["--check", path.join(wrongFormat, publish)], { encoding: "utf8" });
assert.equal(formatCheck.status, 1, formatCheck.stderr);
assert.match(formatCheck.stderr, /Cannot use import statement outside a module/);

const edits = [
  [
    "reread",
    dispatch.callee.object,
    "arrayProto.join",
    ({ replacements, result }) => replacements === 1 && result === "replacement",
  ],
  ["receiver", dispatch.arguments[0], "[]", ({ receiver, result }) => !receiver && result === ""],
];
for (const [name, node, insert, distinguish] of edits) {
  const tree = path.join(dir, name);
  cpSync(reference, tree, { recursive: true });
  writeFileSync(
    path.join(tree, helper),
    body.slice(0, node.start) + insert + body.slice(node.end) + "export { join };\n"
  );
  const observed = observe(tree);
  assert.equal(observed.identicalThrown, false, name);
  assert.equal(observed.writes, 0, name);
  assert(distinguish(observed), JSON.stringify({ name, observed }));
  if (disk) {
    const changed = path.join(dir, `tampered-${name}`);
    cpSync(request.out, changed, { recursive: true });
    copyFileSync(path.join(tree, helper), path.join(changed, helper));
    assert.equal(split({ ...request, out: changed, checkOutput: true }).status, "refused");
  }
}

const evidence = {
  original: { sha256: sha256(bytes), units: rows.length, imports: 0 },
  placement: JSON.parse(readFileSync(spec)),
  projection: null,
  reachedCallProof: null, // The caller relation is a separate proof interface.
  cli: result && { verdict: result.verdict, files: result.files, sourceShape: probeSource() },
  disk: disk?.verdict ?? null,
  node: { original: before, reference: referenceNode, cli: emittedNode },
  deployment: {
    format: "node-esm",
    manifestSha256: sha256(packageBytes),
    referenceOnly: emittedNode === null,
    commonjsRejected: true,
  },
};
console.log(JSON.stringify(evidence));
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "lodash-join.json"),
  JSON.stringify(evidence, null, 2) + "\n"
);
