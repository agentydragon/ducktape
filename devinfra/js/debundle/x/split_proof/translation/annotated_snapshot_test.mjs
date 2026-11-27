import { strict as assert } from "node:assert";
import { annotateProgram, parseModule, resolveIdentifiers } from "./scope_metadata.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { acorn, dump, eslintScope, loadSnapshot } from "./parser.mjs";

function legacyDump(parsed) {
  const meaning = resolveIdentifiers(parsed, () => true);
  return JSON.stringify(annotateProgram(parsed, meaning, { legacy: true }));
}
// The transported Program keeps its declaration list, import and export. Lean splits the list later.
const syntax = 'import {x as y} from "./dep.js"; const a = y, b = 1; export {a};';
const syntaxProgram = dump(syntax);
assert.equal(syntaxProgram.sourceText, syntax);
assert.equal(syntaxProgram.sourceType, "module");
assert.deepEqual(
  syntaxProgram.body.map((s) => syntax.slice(s.start, s.end)),
  ['import {x as y} from "./dep.js";', "const a = y, b = 1;", "export {a};"]
);
assert.deepEqual(
  syntaxProgram.body[1].declarations.map((d) => syntax.slice(d.start, d.end)),
  ["a = y", "b = 1"]
);
assert.equal(syntaxProgram.body[1].declarations[0].init.k, "V");
assert.equal(syntaxProgram.body[2].specifiers[0].local.k, "T");
const structure = (value) =>
  JSON.parse(
    JSON.stringify(value, function (key, v) {
      if (["start", "end", "sourceText", "annotationVersion"].includes(key)) return undefined;
      if (this?.type === "Identifier" && ["k", "b", "d"].includes(key)) return undefined;
      if (this?.type === "Literal" && ["lit", "value", "raw"].includes(key)) return undefined;
      return v;
    })
  );
assert.deepEqual(
  structure(syntaxProgram),
  structure(acorn.parse(syntax, { sourceType: "module", ecmaVersion: "latest" }))
);
// Printer fields cross the JSON boundary, including values JSON cannot represent directly.
const printSource = `/*🌱*/ const literals = [/a+/gi, 0xffn, 1e400, -0];
const holder = { get /* keep */ item() { return 1; }, async *items() {} };
export class Thing { static run() {} }
const closures = [x => x, function () {}, class {}];`;
const printParsed = parseModule(printSource, acorn.parse, eslintScope);
const printProgram = JSON.parse(JSON.stringify(dumpParsed(printParsed)));
assert.equal(printProgram.annotationVersion, 2);
const [literalDecl, objectDecl, exportedClass, closuresDecl] = printProgram.body;
assert.deepEqual(literalDecl.declarations[0].init.elements, [
  { type: "Literal", lit: "r:a+/gi", raw: "/a+/gi", value: null, regex: { pattern: "a+", flags: "gi" } },
  { type: "Literal", lit: "bi:255", raw: "0xffn", value: "255", bigint: "255" },
  { type: "Literal", lit: "n:Infinity", raw: "1e400", value: "Infinity" },
  {
    type: "UnaryExpression",
    operator: "-",
    prefix: true,
    argument: { type: "Literal", lit: "n:0", raw: "0", value: 0 },
  },
]);
const methods = objectDecl.declarations[0].init.properties;
const classDecl = exportedClass.declaration;
const closures = closuresDecl.declarations[0].init.elements;
assert.deepEqual(
  [...methods, classDecl, ...classDecl.body.body, ...closures].map(({ start, end }) => printSource.slice(start, end)),
  [
    "get /* keep */ item() { return 1; }",
    "async *items() {}",
    "class Thing { static run() {} }",
    "static run() {}",
    "x => x",
    "function () {}",
    "class {}",
  ]
);
const legacy = JSON.parse(legacyDump(printParsed));
assert.deepEqual(loadSnapshot(legacy), printProgram);
legacy.body[0].declarations[0].init.elements[0].lit = "r:b+/gi";
assert.throws(() => loadSnapshot(legacy), /disagrees with a fresh parse/);
const scopes = dump("const a = (x) => x, b = (x) => x;");
const callbacks = scopes.body[0].declarations.map((d) => d.init);
assert.notEqual(callbacks[0].params[0].b, callbacks[1].params[0].b);
for (const callback of callbacks) assert.equal(callback.params[0].b, callback.body.b);
