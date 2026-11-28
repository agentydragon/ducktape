import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import { sha256 } from "./source_ownership.mjs";

const [sourceArg] = process.argv.slice(2);
const source = path.resolve(sourceArg);
const dir = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const parse = (code) => parseModule(code, acorn.parse, eslintScope);
const put = (name, content) => {
  const file = path.join(dir, name);
  writeFileSync(file, content);
  return file;
};
const readJson = (file) => JSON.parse(readFileSync(file));
const load = (file) => import(pathToFileURL(file).href);

const code = readFileSync(source, "utf8");
const { zod: pin } = readJson(new URL("./source_pins.json", import.meta.url));
assert.equal(sha256(code), pin.sha256);
const full = parse(code);
const units = topLevelStatements(full.ast);
assert.equal(units.length, pin.units);
const unit = units[52];
assert.deepEqual([unit.start, unit.end, unit.node.id.name], [36686, 37168, "normalizeParams"]);
const exactBody = code.slice(unit.start, unit.end);
const [sourceShape] = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, {
    input:
      JSON.stringify([
        {
          kind: "singletonSourceShape",
          input: { source: dumpParsed(full, { includeOpaqueCaptures: true }), entry: "entry.mjs" },
        },
      ]) + "\n",
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  })
);
assert(sourceShape.error || sourceShape.output);
const entry = "entry.mjs";
const half = "half.mjs";
const halfSelectors = units.map((_, ordinal) => "#" + ordinal);
const cut = Math.floor(units.length / 2);
const fullSpec = put(
  "full-halves.json",
  JSON.stringify({
    entry,
    modules: { [half]: halfSelectors.slice(0, cut), [entry]: halfSelectors.slice(cut) },
    order: [half],
    sequential: true,
  })
);
const fullRequest = { source, spec: fullSpec, out: path.join(dir, "full-halves"), require: "all-outcomes" };
const fullResult = split(fullRequest);
if (fullResult.status === "certified")
  assert.deepEqual(split({ ...fullRequest, checkOutput: true }).verdict, fullResult.verdict);

const spec = put(
  "split.json",
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "predicate.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    order: ["predicate.mjs"],
    sequential: true,
  })
);
const prelude = "function enabled(value) { return value === true; }\n";
const projection = (body) => prelude + body + "\nexport { enabled, normalizeParams };\n";
const exact = projection(exactBody);
const exactRequest = {
  source: put("exact-unit.mjs", exact),
  spec,
  out: path.join(dir, "exact"),
  require: "all-outcomes",
};
const exactResult = split(exactRequest);
const exactProbe = await load(exactRequest.source);
const { z } = await load(source);
assert.equal(z.string({ error: "copied" }).safeParse(8).error.issues[0].message, "copied");
assert.equal(exactProbe.normalizeParams({ error: "copied" }).error(), "copied");

const shape = (body) => `function normalizeParams(params) { if (enabled(true)) return ${body}; }`;
const zodSpread = unit.node.body.body.findLast(
  ({ type, consequent }) =>
    type === "IfStatement" &&
    consequent.type === "ReturnStatement" &&
    consequent.argument?.type === "ObjectExpression" &&
    consequent.argument.properties[0]?.type === "SpreadElement"
).consequent.argument;
const spread = code.slice(zodSpread.start, zodSpread.end);
assert.equal(spread, "{ ...params, error: () => params.error }");
const control = projection(shape(spread));
const request = { source: put("spread.mjs", control), spec, out: path.join(dir, "spread"), require: "all-outcomes" };
const result = split(request);
console.log(
  JSON.stringify({
    source: { sha256: pin.sha256, ordinal: 52, utf16: [unit.start, unit.end], unitSha256: sha256(exactBody) },
    fullSourceShape: sourceShape.error ?? "decoded",
    fullStrict: fullResult.verdict,
    exactUnit: exactResult.verdict,
    control: result.verdict,
  })
);
assert.equal(result.status, "certified", result.log);
assert.equal(result.scope, "indexed-exported-function", result.log);
assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);

function observe({ enabled, normalizeParams }) {
  assert.equal(enabled(true), true);
  const order = [];
  const hidden = Symbol("hidden");
  const symbol = Symbol("copied");
  const prototype = {
    set __proto__(_) {
      assert.fail("used the inherited setter");
    },
    inherited: true,
  };
  const value = {
    __proto__: prototype,
    get later() {
      order.push("later");
      return "later";
    },
    get 3() {
      order.push("index");
      delete this.later;
      this.newKey = true;
      return this.marker;
    },
    marker: "receiver",
    error: "original",
    get [symbol]() {
      order.push("symbol");
      return this.marker;
    },
  };
  Object.defineProperty(value, hidden, { enumerable: false, value: "hidden" });
  Object.defineProperty(value, "__proto__", { enumerable: true, value: "own", configurable: true });
  const copy = normalizeParams(value);
  assert.deepEqual(order, ["index", "symbol"]);
  assert.deepEqual(Reflect.ownKeys(copy), ["3", "marker", "error", "__proto__", symbol]);
  assert.equal(Object.getPrototypeOf(copy), Object.prototype);
  for (const key of ["3", "marker", "__proto__", symbol]) {
    assert.deepEqual(Object.getOwnPropertyDescriptor(copy, key), {
      value: key === "__proto__" ? "own" : "receiver",
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }
  assert.equal(copy.error(), "original");
  value.error = "changed";
  assert.equal(copy.error(), "changed");

  const thrown = {};
  const abrupt = {
    error: "enter the spread branch",
    get before() {
      order.push("before");
      return 1;
    },
    get stop() {
      order.push("stop");
      throw thrown;
    },
    get after() {
      order.push("after");
      return 3;
    },
  };
  assert.throws(
    () => normalizeParams(abrupt),
    (error) => error === thrown
  );
  assert.deepEqual(order, ["index", "symbol", "before", "stop"]);
}
observe(exactProbe);
observe(await load(request.source));
observe(await load(path.join(request.out, entry)));

const changed = path.join(dir, "reversed-copy");
cpSync(request.out, changed, { recursive: true });
const changedEntry = path.join(changed, entry);
const entryText = readFileSync(changedEntry, "utf8");
const declaration = parse(entryText).ast.body.find(
  ({ type, id }) => type === "FunctionDeclaration" && id.name === "normalizeParams"
);
const copy = declaration.body.body[0].consequent.argument;
assert.equal(copy.type, "ObjectExpression");
const [sourceField, override] = copy.properties;
assert.equal(sourceField.type, "SpreadElement");
writeFileSync(
  changedEntry,
  entryText.slice(0, sourceField.start) +
    entryText.slice(override.start, override.end) +
    ", " +
    entryText.slice(sourceField.start, sourceField.end) +
    entryText.slice(override.end)
);
const changedApi = await load(changedEntry);
assert.equal(changedApi.normalizeParams({ error: "original" }).error, "original");
assert.equal(split({ ...request, out: changed, checkOutput: true }).status, "refused");
assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);

for (const [name, operand] of [
  ["accessor-literal", "{ ...params, get error() { return params.error; } }"],
  ["host-operand", "{ ...Object }"],
]) {
  const refusal = split({
    ...request,
    source: put(name + ".mjs", projection(shape(operand))),
    out: path.join(dir, name),
  });
  assert.equal(refusal.status, "refused", refusal.log);
}
writeFileSync(
  path.join(artifacts, "zod-object-spread.json"),
  JSON.stringify(
    {
      sha256: pin.sha256,
      ordinal: 52,
      start: unit.start,
      end: unit.end,
      fullSourceShape: sourceShape.error ?? "decoded",
      fullStrict: fullResult.verdict,
      exactUnit: exactResult.verdict,
      control: result.verdict,
    },
    null,
    2
  ) + "\n"
);
