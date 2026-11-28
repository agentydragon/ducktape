import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { walk } from "../../translation/syntax_inventory.mjs";
import { split } from "../../translation/testing/split.mjs";
import { smoke } from "./smoke.mjs";

const [source] = process.argv.slice(2);
const bytes = readFileSync(source);
const sha256 = createHash("sha256").update(bytes).digest("hex");
assert.equal(sha256, "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44");
const { ast, scopes, code } = parseModule(bytes.toString("utf8"), acorn.parse, eslintScope);
const units = topLevelStatements(ast);
assert.equal(units.length, 59);
const moduleScope = scopes.scopes.find((scope) => scope.type === "module");
const declaration = (id) => units[id].node.declarations[0];
const factory = declaration(6);
const helper = declaration(3);
assert.equal(factory.id.name, "__commonJS");
assert.equal(helper.id.name, "__getOwnPropNames");
const loaderScope = scopes.acquire(factory.init.body);
const helperCell = moduleScope.set.get(helper.id.name);
assert(
  loaderScope.through.some(({ resolved, identifier }) => resolved === helperCell && identifier.name === helper.id.name)
);

const callbacks = new Map();
const loaderRows = new Map();
for (const [id, { node }] of units.entries()) {
  const d = node.declarations?.[0];
  if (d?.init?.callee?.name !== factory.id.name) continue;
  const binding = moduleScope.set.get(factory.id.name);
  assert(binding.references.some(({ identifier }) => identifier === d.init.callee));
  const [callback] = d.init.arguments[0].properties;
  assert.equal(d.init.arguments[0].properties.length, 1);
  assert(callback.method && typeof callback.key.value === "string");
  assert(!callbacks.has(callback.key.value));
  const row = { id, binding: d.id.name, callback: callback.key.value };
  callbacks.set(callback.key.value, row);
  loaderRows.set(d.id.name, row);
}
assert.equal(loaderRows.get("require_constants").id, 9);
assert.equal(loaderRows.get("require_semver2").id, 55);
assert.equal(declaration(56).init.arguments[0].callee.name, "require_semver2");
assert.equal(declaration(56).init.callee.name, "__toESM");

const directCalls = [];
for (const [owner, { node }] of units.entries()) {
  walk(node, (expr) => {
    const { callee } = expr;
    if (expr.type !== "CallExpression" || callee.type !== "Identifier") return;
    const row = loaderRows.get(callee.name);
    const ref = row && moduleScope.set.get(row.binding).references.find(({ identifier }) => identifier === callee);
    if (ref) directCalls.push({ from: owner, to: row.id, topLevel: ref.from.variableScope === moduleScope });
  });
}
assert.deepEqual(
  directCalls.filter((call) => call.topLevel),
  [{ from: 56, to: 55, topLevel: true }]
);

// Run the shipped entry twice, once untouched and once in a realm where the
// captured helper delegates to the original and logs callback selection.
const tmp = process.env.TEST_TMPDIR;
const output = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const baseline = await smoke("semver", source);
assert(baseline.ok, JSON.stringify(baseline));
const traced = path.join(tmp, "traced.mjs");
copyFileSync(source, traced);
// Install the observer only until the entry has captured it and initialized.
// Each run still uses the same pinned source bytes.
async function observedImport(url, onLookup) {
  const descriptor = Object.getOwnPropertyDescriptor(Object, "getOwnPropertyNames");
  Object.defineProperty(Object, "getOwnPropertyNames", {
    ...descriptor,
    value(value) {
      const keys = Reflect.apply(descriptor.value, this, [value]);
      const row = keys.length === 1 ? callbacks.get(keys[0]) : undefined;
      if (row) onLookup(row.id);
      return keys;
    },
  });
  try {
    return await import(url);
  } finally {
    Object.defineProperty(Object, "getOwnPropertyNames", descriptor);
  }
}

const tracedUrl = pathToFileURL(traced).href;
const callbackLookups = [];
const actual = await observedImport(tracedUrl, (id) => callbackLookups.push(id));
assert.deepEqual(Object.keys(actual), ["semver"]);
const duringImport = callbackLookups.slice();
assert.equal(duringImport[0], 55);
assert.deepEqual(duringImport.slice(0, 3), [55, 11, 9]);
assert.deepEqual(
  [...duringImport].sort((a, b) => a - b),
  [...loaderRows.values()].map(({ id }) => id)
);
assert.equal((await import(tracedUrl)).semver, actual.semver);
assert.equal(actual.semver.valid("1.2.3"), "1.2.3");
assert.equal(actual.semver.satisfies("1.2.3", "^1.0.0"), true);
assert.deepEqual(await smoke("semver", traced), baseline);
const afterImport = callbackLookups.slice(duringImport.length);
assert.deepEqual(afterImport, []);
console.log(
  JSON.stringify({ loaderCount: loaderRows.size, directCalls: directCalls.length, duringImport, afterImport })
);

// Throw from the captured lookup at the first constants load in the actual
// import chain. The failed ESM evaluation remains observable on a second
// import of the same URL, even though CommonJS resets its private cache.
const eagerFailure = new Error("captured lookup threw while loading constants");
const abruptLookups = [];
const failedUrl = tracedUrl + "?throw";
await assert.rejects(
  observedImport(failedUrl, (id) => {
    abruptLookups.push(id);
    if (id === 9) throw eagerFailure;
  }),
  (error) => error === eagerFailure
);
assert.deepEqual(abruptLookups, [55, 11, 9]);
await assert.rejects(import(failedUrl), (error) => error === eagerFailure);
assert.deepEqual(abruptLookups, [55, 11, 9]);
const fresh = await import(tracedUrl + "?fresh");
assert.equal(fresh.semver.valid("1.2.3"), "1.2.3");

// Project the original capture, factory and first loader into a test driver.
// Reparse emitted bytes before running the private controls.
const projectionIds = [3, 6, 9];
const projectionDir = path.join(output, "projection");
mkdirSync(projectionDir);
const driver = parseModule(
  'import { observe } from "./semver_wrapper_controls.mjs"; observe(__commonJS, require_constants);',
  acorn.parse
).ast.body;
const projection = "semver-projection.mjs";
const manifest = path.join(tmp, "projection.json");
writeFileSync(
  manifest,
  JSON.stringify({
    [projection]: {
      type: "Program",
      sourceType: "module",
      body: [...projectionIds.map((i) => units[i].node), ...driver],
    },
  })
);
execFileSync(process.env.MODULE_IO, ["--print", manifest, projectionDir, source]);
const projectionPath = path.join(projectionDir, projection);
const parsedProjection = parseModule(readFileSync(projectionPath, "utf8"), acorn.parse);
const printedUnits = topLevelStatements(parsedProjection.ast);
const syntax = (node) =>
  JSON.stringify(node, (key, value) => (["start", "end", "range", "loc"].includes(key) ? undefined : value));
for (const [index, ordinal] of projectionIds.entries()) {
  const original = declaration(ordinal).init;
  const readback = printedUnits[index].node.declarations[0].init;
  assert.equal(syntax(readback), syntax(original));
  if (ordinal === 6) {
    assert.equal(parsedProjection.code.slice(readback.start, readback.end), code.slice(original.start, original.end));
  }
  assert.equal(printedUnits[index].node.declarations[0].id.name, declaration(ordinal).id.name);
}
copyFileSync(
  new URL("./semver_wrapper_controls.mjs", import.meta.url),
  path.join(projectionDir, "semver_wrapper_controls.mjs")
);
const controlRun = JSON.parse(execFileSync(process.execPath, [projectionPath], { encoding: "utf8" }));

// Keep the entire original initializer chain before its first eager call.
const entry = "entry.mjs";
const prefix = "prefix.mjs";
const spec = path.join(output, "semver-order-kept-spec.json");
const ids = (start, end) => Array.from({ length: end - start }, (_, i) => "#" + (i + start));
writeFileSync(
  spec,
  JSON.stringify({
    entry,
    modules: { [entry]: ids(56, 59), [prefix]: ids(0, 56) },
    sequential: true,
    order: [prefix],
  })
);
const request = { source, spec, out: path.join(tmp, "published"), require: "all-outcomes" };
const result = split(request);
const rechecked = result.status === "certified";
if (rechecked) {
  assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
  assert.deepEqual(await smoke("semver", path.join(request.out, entry)), baseline);
} else {
  assert.equal(result.reasonCode, "no_constructed_rule");
}
writeFileSync(
  path.join(output, "semver-wrapper-node.json"),
  JSON.stringify(
    {
      original: { sha256, units: units.length, directCalls, loaderRows: [...loaderRows.values()] },
      reached: {
        duringImport,
        afterImport,
        abruptImport: { lookups: abruptLookups, sameErrorOnRepeatImport: true, freshImport: "1.2.3" },
      },
      projection: {
        units: projectionIds,
        controls: controlRun,
        readback: "matching initializers and exact factory source",
      },
      node: { api: Object.keys(actual), baseline },
      deployment: { prefix: [0, 55], entry: [56, 58], verdict: result.verdict, files: result.files, rechecked },
    },
    null,
    2
  )
);
