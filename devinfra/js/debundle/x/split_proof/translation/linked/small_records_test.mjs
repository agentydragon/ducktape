import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
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

const records = Array.from({ length: 28 }, (_, index) => ({
  name: `item${index}`,
  fields: Array.from({ length: 10 }, (_, field) => [`part${field}`, `value-${index}-${field}`]),
}));
const member = (name, key) => ({
  type: "MemberExpression",
  object: id(name),
  property: id(key),
  computed: false,
  optional: false,
});
const declarations = records.map(({ name, fields }) =>
  variable("const", name, {
    type: "ObjectExpression",
    properties: fields.map(([key, value]) => property(key, literal(value))),
  })
);
declarations.push(
  variable("const", "selected", {
    type: "ArrayExpression",
    elements: [member("item0", "part0"), member("item27", "part9"), member("item1", "absent")],
  })
);
const names = [...records.map(({ name }) => name), "selected", "marker"];
const source = program([
  ...declarations,
  variable("const", "marker", literal("ready")),
  exportNamed(names.map((name) => [name, name])),
]);

const entry = "entry.mjs";
const order = ["marker.mjs", "records.mjs"];
const spec = {
  entry,
  modules: {
    "marker.mjs": [`#${declarations.length}`],
    "records.mjs": declarations.map((_, index) => `#${index}`),
    [entry]: [`#${declarations.length + 1}`],
  },
  order,
  sequential: true,
};
const dir = path.join(root, "small-records");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const manifest = writeDeployment(sourceFile, specFile, out);
assertPublicEmissionRefused(manifest, out);

await withSplitPreview({ source: sourceFile, spec: specFile, expectedExit: 1 }, async ({ directory, entryFile }) => {
  assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
  const model = probeLinkedModel({
    source: sourceFile,
    spec: specFile,
    directory,
    observe: [
      ["item0", "part0"],
      ["item27", "part9"],
      ["selected", "2"],
    ],
  });
  assert.equal(model.status, "matched");
  assert(model.fuel > 0 && model.fuel <= 74);
  assert.deepEqual(model.assumptions, []);
  assert.deepEqual(model.host_refs, []);
  assert.deepEqual(model.observed, [
    { path: ["item0", "part0"], original: '"value-0-0"', split: '"value-0-0"' },
    { path: ["item27", "part9"], original: '"value-27-9"', split: '"value-27-9"' },
    { path: ["selected", "2"], original: "undefined", split: "undefined" },
  ]);
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);

  for (const file of [sourceFile, entryFile]) {
    const namespace = await import(pathToFileURL(file).href);
    for (const { name, fields } of records) {
      const expected = Object.fromEntries(fields);
      assert.deepEqual(Object.keys(namespace[name]), Object.keys(expected));
      assert.deepEqual(Object.getOwnPropertyDescriptors(namespace[name]), Object.getOwnPropertyDescriptors(expected));
    }
    assert.deepEqual(namespace.selected, ["value-0-0", "value-27-9", undefined]);
    assert.equal(namespace.marker, "ready");
  }
});
