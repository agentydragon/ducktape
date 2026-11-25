import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  sequentialSplit,
} from "./harness.mjs";

const runCase = caseShard();
const modules = { "worker.mjs": ["#0"], "entry.mjs": ["#1"] };

if (runCase("method-and-fields")) {
  const run = sequentialSplit(
    "private-element-order",
    `
    class Parser {
      factory = this.#factory();
      #value = 3;
      #factory() { return () => this.#value; }
      put(v) { this.#value = v; }
      method() { return this.#factory; }
    }
    export const result = (() => {
      const p = new Parser(), q = new Parser();
      if (p.method() !== q.method() || p.method().name !== "#factory") throw 1;
      p.put(7);
      if (p.factory() !== 7 || q.factory() !== 3) throw 2;
      const copy = { ...p };
      if (copy.factory !== p.factory || "#value" in p || "#factory" in Parser.prototype) throw 3;
      return p.factory();
    })();`,
    modules
  );
  assertCertifiedResult(run, 7);
  const check = () => lower(run.files.sourceFile, run.files.specFile, run.out, ["--check-output"]);
  assert.deepEqual(check().verdict, run.verdict);
  const file = path.join(run.out, "worker.mjs");
  const original = readFileSync(file, "utf8");
  const mutation = original.replace("#value = 3", "#value = 4");
  assert.notEqual(mutation, original);
  writeFileSync(file, mutation);
  assert.equal(check().verdict.status, "rejected");
}

if (runCase("fresh-brands")) {
  const run = sequentialSplit(
    "private-brand-identity",
    `
    function make() {
      return class {
        #x = 1;
        read(o) { return o.#x; }
        write(o, effects) { o.#x = (effects.v = 9); }
      };
    }
    export const result = (() => {
      const A = make(), B = make(), a = new A(), b = new B();
      const C = class extends A { #x = 2; own() { return this.#x; } };
      const c = new C(), effects = { v: 0 };
      if (c.own() !== 2 || a.read(c) !== 1) throw 1;
      let failures = 0;
      try { a.write(b, effects); } catch { failures++; }
      try { a.read({ __proto__: a }); } catch { failures++; }
      if (failures !== 2 || effects.v !== 9 || b.read(b) !== 1) throw 2;
      return failures;
    })();`,
    modules
  );
  assertCertifiedResult(run, 2);
}

if (runCase("early-and-duplicate")) {
  const run = sequentialSplit(
    "private-element-abrupt",
    `
    class Base { constructor(target) { return target; } }
    export const result = (() => {
      const trace = { v: 0 };
      const Derived = class extends Base {
        #x = (trace.v += 1);
        #m() { return 3; }
        call(o) { return o.#m(trace.v += 10); }
        assign() { this.#m = (trace.v += 100); }
      };
      const target = {}, other = new Derived(target);
      let failures = 0;
      try { new Derived(target); } catch { failures++; }
      const d = new Derived();
      try { d.call({}); } catch { failures++; }
      try { d.assign(); } catch { failures++; }
      const Early = class { x = this.#later; #later = 1; };
      try { new Early(); } catch { failures++; }
      if (other !== target || failures !== 4 || trace.v !== 102) throw 1;
      return trace.v;
    })();`,
    modules
  );
  assertCertifiedResult(run, 102);
}

if (runCase("deferred-arrow")) {
  const source = `class Reader {
    #read() { return 9; }
    later() { return async () => this.#read(); }
  }
  export const result = new Reader().later();`;
  const run = sequentialSplit(
    "private-async-closure",
    source.replace("new Reader().later()", "typeof new Reader().later()"),
    modules
  );
  assertCertifiedResult(run, "function");
  const invoked = sequentialSplit(
    "reached-private-async",
    source.replace("new Reader().later()", "new Reader().later()()"),
    modules
  );
  assertRejectedOrOrderKeptSlice(invoked, /async invocation requires Promise semantics/);
}
