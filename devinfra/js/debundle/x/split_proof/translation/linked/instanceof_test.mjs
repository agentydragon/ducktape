import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, caseShard, lower, root, threeUnitFixture as fixture } from "./harness.mjs";

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const options = ["--host-contract", profile];
const runCase = caseShard();

if (runCase("ordinary prototype chain")) {
  const f = fixture(
    "ordinary-instanceof",
    `class Parent {}
     const result = (() => {
       class Child extends Parent {}
       const child = new Child();
       if (!(child instanceof Parent) || !(child instanceof Child) || new Parent() instanceof Child) throw 1;
       if (({ __proto__: null }) instanceof Parent || 0 instanceof Child || Child.prototype instanceof Child) throw 2;
       function C() {}
       const previous = new C();
       C.prototype = {};
       if (previous instanceof C || !(new C() instanceof C)) throw 4;
       C.prototype = 3;
       if (1 instanceof C) throw 3;
       let errors = 0;
       try { child instanceof C; } catch { errors = errors + 1; }
       try { child instanceof null; } catch { errors = errors + 1; }
       try { child instanceof {}; } catch { errors = errors + 1; }
       return errors;
     })();
     export { result };`
  );
  const run = lower(f.sourceFile, f.specFile, f.out, options);
  assertCertifiedResult({ ...run, files: f, out: f.out }, 3);
  assert.deepEqual(f.run(...options, "--check-output"), run.verdict);
  const out = path.join(f.out, "entry.mjs");
  const original = readFileSync(out, "utf8");
  const changed = original.replace("instanceof Parent", "instanceof Child");
  assert.notEqual(changed, original);
  writeFileSync(out, changed);
  assert.match(f.run(...options, "--check-output").reason, /differs from its generated ESTree/);
}

if (runCase("custom hasInstance protocol")) {
  const f = fixture(
    "instanceof-protocol",
    `class Predicate {
       static get [Symbol.hasInstance]() {
         this.trace += "G";
         return function (value) { this.trace += "C"; return this.expected === value ? "yes" : ""; };
       }
     }
     const result = (() => {
       Predicate.trace = "";
       Predicate.expected = 42;
       const left = () => { Predicate.trace += "L"; return 42; };
       const right = () => { Predicate.trace += "R"; return Predicate; };
       if (!(left() instanceof right()) || 2 instanceof Predicate) throw 1;
       const inherited = { __proto__: Predicate };
       inherited.expected = 7;
       if (!(7 instanceof inherited) || inherited.trace !== "LRGCGCGC") throw 2;
       let errors = 0;
       const primitiveHook = { [Symbol.hasInstance]: 7 };
       try { 2 instanceof primitiveHook; } catch { errors = errors + 1; }
       class Throws { static get [Symbol.hasInstance]() { throw "getter"; } }
       try { 1 instanceof Throws; } catch (e) { if (e === "getter") errors = errors + 1; }
       if (errors !== 2) throw 3;
       return Predicate.trace;
     })();
     export { result };`
  );
  const run = lower(f.sourceFile, f.specFile, f.out, options);
  assertCertifiedResult({ ...run, files: f, out: f.out }, "LRGCGC");
  assert.deepEqual(f.run(...options, "--check-output"), run.verdict);
}
