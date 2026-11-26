import assert from "node:assert/strict";
import path from "node:path";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { pathToFileURL } from "node:url";
import {
  exportNamed,
  id,
  literal,
  moduleEvaluationOrder,
  prepare,
  program,
  property,
  root,
  variable,
} from "./harness.mjs";

const fields = Array.from({ length: 705 }, (_, i) => property(`field${i}`, literal(`value${i}`)));
const record = { type: "ObjectExpression", properties: fields };
const member = (object, key, computed = false) => ({
  type: "MemberExpression",
  object: id(object),
  property: id(key),
  computed,
  optional: false,
});
const sampledFields = [0, 1, 117, 235, 352, 469, 587, 704];
const source = program([
  variable("const", "left", record),
  variable("const", "right", record),
  variable("const", "computedKey", literal("field263")),
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [
      ...sampledFields.map((i) => member(i % 2 ? "right" : "left", `field${i}`)),
      member("left", "missing"),
      member("right", "computedKey", true),
    ],
  }),
  exportNamed(["left", "right", "result"].map((name) => [name, name])),
]);
const entry = "entry.mjs";
const order = ["right.mjs", "left.mjs"];
const spec = {
  entry,
  modules: { "left.mjs": ["#0"], "right.mjs": ["#1"], [entry]: ["#2", "#3", "#4"] },
  order,
  sequential: true,
};
const dir = path.join(root, "wide-records");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const manifest = writeDeployment(sourceFile, specFile, out);
assertPublicEmissionRefused(manifest, out);
await withSplitPreview({ source: sourceFile, spec: specFile }, async ({ directory, entryFile }) => {
  assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
  const model = probeLinkedModel({
    source: sourceFile,
    spec: specFile,
    directory,
    observe: [
      ["left", "field704"],
      ["right", "field1"],
      ["result", "8"],
      ["result", "9"],
    ],
  });
  assert.equal(model.status, "matched");
  assert.equal(model.fuel, 64 + fields.length);
  assert.deepEqual(model.assumptions, []);
  assert.deepEqual(model.host_refs, []);
  assert.deepEqual(model.observed, [
    { path: ["left", "field704"], original: '"value704"', split: '"value704"' },
    { path: ["right", "field1"], original: '"value1"', split: '"value1"' },
    { path: ["result", "8"], original: "undefined", split: "undefined" },
    { path: ["result", "9"], original: '"value263"', split: '"value263"' },
  ]);
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);

  for (const file of [sourceFile, entryFile]) {
    const ns = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(ns), ["left", "result", "right"]);
    assert.deepEqual(ns.result, [...sampledFields.map((i) => `value${i}`), undefined, "value263"]);
    assert.deepEqual(
      Object.keys(ns.left),
      fields.map(({ key }) => key.name)
    );
    assert.deepEqual(ns.left, ns.right);
    assert.notEqual(ns.left, ns.right);
  }
});

// A computed key does not get a per-key equation from the quoted syntax.
// Duplicate static keys keep ordinary object-literal replacement semantics.
for (const kind of ["string", "object", "duplicate"]) {
  const objectKey = kind === "object";
  const key = objectKey ? { type: "ObjectExpression", properties: [] } : literal("field95");
  const source = program([
    variable("const", "table", {
      type: "ObjectExpression",
      properties:
        kind === "duplicate" ? [...fields.slice(0, 96), property("field2", literal("last"))] : fields.slice(0, 96),
    }),
    variable("const", "key", key),
    variable("const", "result", {
      type: "ArrayExpression",
      elements: [member("table", "key", true), ...Array.from({ length: 8 }, (_, i) => member("table", `field${i}`))],
    }),
    exportNamed([["result", "result"]]),
  ]);
  const dir = path.join(root, `computed-record-key-${kind}`);
  const { sourceFile, specFile } = prepare(dir, {
    source,
    provider: "",
    spec: { entry, modules: { "table.mjs": ["#0"], [entry]: ["#1", "#2", "#3"] }, order: ["table.mjs"] },
  });
  const out = path.join(dir, "out");
  const manifest = writeDeployment(sourceFile, specFile, out);
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ source: sourceFile, spec: specFile }, async ({ directory, entryFile }) => {
    const model = probeLinkedModel({
      source: sourceFile,
      spec: specFile,
      directory,
      observe: [
        ["result", "0"],
        ["result", "3"],
      ],
    });
    if (objectKey) {
      assert.equal(model.status, "refused");
      assert.equal(model.reason_code, "probe_unknown");
      assert.match(model.reason, /object is converted to a primitive/);
    } else {
      assert.equal(model.status, "matched");
      assert.equal(model.fuel, 64 + (kind === "duplicate" ? 97 : 96));
      assert.deepEqual(model.assumptions, []);
      assert.deepEqual(model.host_refs, []);
      assert.deepEqual(model.observed, [
        { path: ["result", "0"], original: '"value95"', split: '"value95"' },
        {
          path: ["result", "3"],
          original: kind === "duplicate" ? '"last"' : '"value2"',
          split: kind === "duplicate" ? '"last"' : '"value2"',
        },
      ]);
    }
    const expected = [
      objectKey ? undefined : "value95",
      ...Array.from({ length: 8 }, (_, i) => (kind === "duplicate" && i === 2 ? "last" : `value${i}`)),
    ];
    for (const file of [sourceFile, entryFile]) {
      const { result } = await import(pathToFileURL(file).href);
      assert.deepEqual(result, expected);
    }
  });
}
