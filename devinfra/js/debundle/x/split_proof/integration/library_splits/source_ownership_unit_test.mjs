import assert from "node:assert/strict";
import { attribute, byteOffsets, mapIntervals } from "./source_ownership.mjs";

const code = "α😀 + b;\n// outside\nx;\n";
const names = ["node_modules/semver/a.js", "node_modules/semver/b.js"];
const map = { version: 3, sources: names, sourcesContent: ["α😀", "b"], mappings: "AAAA,G,ECAA;;" };
const inputs = Object.fromEntries(names.map((name) => [name, { bytesInOutput: 1 }]));
const meta = {
  inputs,
  outputs: { "bundle.mjs": { entryPoint: "semver.mjs", bytes: Buffer.byteLength(code), inputs } },
};
const rows = [
  { start: 0, end: 8, node: { type: "ExpressionStatement", expression: {} } },
  { start: 20, end: 22, node: { type: "ExpressionStatement", expression: {} } },
];
const { rows: actual } = attribute("semver", code, rows, map, meta);
assert.deepEqual(
  actual.map(({ classification, mappedBytes, startByte, endByte }) => [
    classification,
    mappedBytes,
    startByte,
    endByte,
  ]),
  [
    ["mixed", 9, 0, 11],
    ["unmapped", 0, 23, 25],
  ]
);
assert.deepEqual(
  actual[0].originals.map(({ file }) => file),
  ["semver/a.js", "semver/b.js"]
);
assert.throws(() => byteOffsets(code)(2), /Unicode/);
assert.throws(() => mapIntervals(code, { ...map, mappings: "g" }), /incomplete/);
assert.throws(() => attribute("semver", code, rows, map, { ...meta, inputs: {} }), /missing metafile input/);
