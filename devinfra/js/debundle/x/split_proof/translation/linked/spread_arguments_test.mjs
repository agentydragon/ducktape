import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, expectedModelObservations, prepareWorkerFixture } from "./fixture_preview.mjs";
import { caseShard, observe } from "./harness.mjs";

const entryUnits = ["#1", "#2"];
const runCase = caseShard();

for (const [name, body, expected] of [
  ["array", "return [0, ...[1, 2], 3];", [0, 1, 2, 3]],
  ["method", "const o = { x: 9, f(a,b) {return [this.x, a, b];} }; return o.f(...[1], 2);", [9, 1, 2]],
  ["new", "class A { constructor(a,b) {this.x = a+b;} } return new A(...[2,3]).x;", 5],
  [
    "super",
    "class A { constructor(x) {this.x=x;} } class B extends A {constructor() {super(...[7]);}} return new B().x;",
    7,
  ],
  [
    "throw",
    'const o = { get value() {throw "first";} }; try { return [...o.value, (() => {throw "later";})()]; } catch(e) {return e;}',
    "first",
  ],
]) {
  if (!runCase(name)) continue;
  const fixture = prepareWorkerFixture(
    `reached-spread-${name}`,
    `const worker = () => { ${body} }; const value = worker(); export { value as result };`,
    entryUnits
  );
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  const samples = Array.isArray(expected)
    ? expected.map((value, index) => [["result", String(index)], value])
    : undefined;
  await assertLinkedPreviewResult(fixture, expected, samples);
}

if (runCase("noniterable")) {
  const { manifest, out, sourceFile, specFile } = prepareWorkerFixture(
    "noniterable-spread",
    "const worker = 42; const value = [...worker]; export {value as result};",
    entryUnits
  );
  assertPublicEmissionRefused(manifest, out);
  await assert.rejects(import(pathToFileURL(sourceFile).href), TypeError);
  await withSplitPreview({ manifest }, async ({ directory, entryFile }) => {
    await assert.rejects(import(pathToFileURL(entryFile).href), TypeError);
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "probe_throw", JSON.stringify(model));
    assert.match(model.reason, /original threw during linked initialization/);

    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}

// A spread drains its iterator before evaluating the argument that follows it.
if (runCase("custom")) {
  const { manifest, out, sourceFile, specFile } = prepareWorkerFixture(
    "custom-spread-order",
    `const worker = () => {
      let trace = "";
      class Items {
        [Symbol.iterator]() {
          trace += "I";
          let used = false;
          return { get next() {
            trace += "G";
            return function() {
              trace += "N";
              if (used) return { done: true };
              used = true;
              return { value: 11 };
            };
          }};
        }
      }
      function take(a,b) {return [a,b,trace];}
      return take(...new Items(), (trace += "L", 19));
    }; const value = worker(); export { value as result };`,
    entryUnits
  );
  assertPublicEmissionRefused(manifest, out);
  const expected = { result: [11, 19, "IGNNL"] };
  assert.deepEqual(observe(sourceFile, "result"), expected);
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), expected);
    const options = {
      source: sourceFile,
      spec: specFile,
      directory,
      observe: [
        ["result", "0"],
        ["result", "1"],
        ["result", "2"],
      ],
    };
    const model = probeLinkedModel({ ...options, host: { profile: "browser-page" } });
    assert.equal(model.status, "matched", JSON.stringify(model));
    assert(model.fuel > 0);
    assert(
      model.host_refs.some(({ key }) => key === "iterator"),
      JSON.stringify(model)
    );
    assert.deepEqual(
      model.observed,
      expectedModelObservations(expected.result.map((value, index) => [["result", String(index)], value]))
    );
    const missing = probeLinkedModel(options);
    assert.equal(missing.status, "refused", JSON.stringify(missing));
    assert.match(missing.reason, /unsupported computed class key/);
  });
}
