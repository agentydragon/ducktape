import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  observe,
  root,
  threeUnitFixture as fixture,
} from "./harness.mjs";

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

const predicate = `class Predicate {
  static get [Symbol.hasInstance]() {
    this.trace += "G";
    return function (value) { this.trace += "C"; return this.expected === value ? "yes" : ""; };
  }
}`;

function checkProtocol(name, declaration, body, expected) {
  if (!runCase(name)) return;
  const f = fixture(
    name,
    `${declaration}
const result = (() => { ${body} })();
export { result };`
  );
  const run = lower(f.sourceFile, f.specFile, f.out, options);
  assertCertifiedResult({ ...run, files: f, out: f.out }, expected);
}

checkProtocol(
  "instanceof-evaluation-order",
  predicate,
  `Predicate.trace = "";
   Predicate.expected = 42;
   const left = () => { Predicate.trace += "L"; return 42; };
   const right = () => { Predicate.trace += "R"; return Predicate; };
   if (!(left() instanceof right())) throw 1;
   return Predicate.trace;`,
  "LRGC"
);

checkProtocol(
  "instanceof-inherited-hook",
  predicate,
  `Predicate.trace = "";
   const inherited = { __proto__: Predicate, expected: 7 };
   if (!(7 instanceof inherited) || 2 instanceof inherited) throw 1;
   if (Predicate.trace !== "") throw 2;
   return inherited.trace;`,
  "GCGC"
);

if (runCase("instanceof-non-callable-hook")) {
  const f = fixture(
    "instanceof-non-callable-hook",
    `const primitiveHook = { [Symbol.hasInstance]: 7 };
     const result = 2 instanceof primitiveHook;
     export { result };`
  );
  const run = lower(f.sourceFile, f.specFile, f.out, options);
  assertRejectedOrOrderKeptSlice({ ...run, out: f.out }, /original threw during linked initialization/);
  assert.deepEqual(observe(f.sourceFile, "result"), { error: "TypeError" });
}

checkProtocol(
  "instanceof-throwing-getter",
  `class Throws { static get [Symbol.hasInstance]() { throw "getter"; } }`,
  `try { 1 instanceof Throws; } catch (e) { return e; }
   return "did not throw";`,
  "getter"
);
