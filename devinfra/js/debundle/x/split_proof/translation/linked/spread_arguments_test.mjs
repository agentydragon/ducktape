import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  prepare,
  root,
  workerSplit,
} from "./harness.mjs";

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
  assertCertifiedResult(
    workerSplit(
      `reached-spread-${name}`,
      `const worker = () => { ${body} }; const value = worker(); export { value as result };`,
      entryUnits
    ),
    expected
  );
}

if (runCase("noniterable")) {
  const replay = workerSplit(
    "noniterable-spread",
    "const worker = 42; const value = [...worker]; export {value as result};",
    entryUnits
  );
  assertRejectedOrOrderKeptSlice(replay, /original threw during linked initialization/);
  await assert.rejects(import(pathToFileURL(replay.files.sourceFile).href), TypeError);
}

// A spread drains its iterator before evaluating the argument that follows it.
if (runCase("custom")) {
  const dir = path.join(root, "custom-spread-order");
  const files = prepare(dir, {
    source: `const worker = () => {
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
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": entryUnits }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  const profile = path.join(dir, "browser.json");
  writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
  const checked = lower(files.sourceFile, files.specFile, out, ["--host-contract", profile]);
  assertCertifiedResult({ files, out, ...checked }, [11, 19, "IGNNL"]);
}
