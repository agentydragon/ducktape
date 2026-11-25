import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import vm from "node:vm";

const context = vm.createContext({});
const result = vm.runInContext(
  `
  let lexical = "declarative";
  const constant = "fixed";
  Object.defineProperty(globalThis, "lexical", {value: "property", writable: true, configurable: true});
  globalThis.mutable = "before";
  globalThis.presentUndefined = undefined;
  Object.defineProperty(globalThis, "accessor", {get() { return "getter"; }, configurable: true});
  const capture = (f) => { try { return String(f()); } catch (e) { return e.name; } };
  const a = capture(() => lexical);
  const b = capture(() => globalThis.lexical);
  const c = capture(() => mutable = "after");
  const d = capture(() => globalThis.mutable);
  const e = capture(() => accessor);
  const f = capture(() => typeof missing);
  const g = capture(() => typeof tdz);
  const h = capture(() => constant = "new");
  const i = capture(() => missing);
  const j = capture(() => typeof presentUndefined);
  const del = delete globalThis.mutable;
  const k = capture(() => mutable);
  // Resolve the reference before its assignment value deletes the property.
  const l = capture(() => { "use strict"; accessor = (delete globalThis.accessor, "late"); });
  let tdz;
  [a,b,c,d,e,f,g,h,i,j,String(del),k,l];
`,
  context
);
const model = execFileSync(process.env.GLOBAL_ENV_TEST, { encoding: "utf8" }).trimEnd().split("\n");
assert.deepEqual(model, Array.from(result));
