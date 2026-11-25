import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertCertifiedResult, caseShard, lower, prepare, root, workerSplit } from "./harness.mjs";

const runCase = caseShard();
if (runCase("stable-sites")) {
  const test = workerSplit(
    "stable-template-sites",
    "function render(tag) { return [tag`same`, tag`same`]; }\n" +
      `const identity = value => value;
       export const result = (() => {
         const a = render(identity), b = render(identity);
         if (a[0] !== b[0] || a[1].raw !== b[1].raw || a[0] === a[1] || a[0].raw === a[1].raw)
           throw "template site identity";
         let failures = 0;
         try { a[0][0] = "changed"; } catch { failures++; }
         try { a[0].raw.extra = 1; } catch { failures++; }
         try { delete a[0].raw; } catch { failures++; }
         if (failures !== 3) throw "unfrozen template";
         return a[0];
       })();`,
    ["#1", "#2"]
  );
  assertCertifiedResult(test, ["same"]);
  for (const f of [test.files.sourceFile, path.join(test.out, "entry.mjs")]) {
    const { result } = await import(pathToFileURL(f).href);
    assert(Object.isFrozen(result) && Object.isFrozen(result.raw));
    assert.deepEqual(Object.getOwnPropertyDescriptor(result, "raw"), {
      value: result.raw,
      enumerable: false,
      configurable: false,
      writable: false,
    });
  }
}

if (runCase("imported-tag")) {
  const dir = path.join(root, "imported-template-tag");
  const files = prepare(dir, {
    source: 'import { tag } from "./provider.mjs"; const value = tag`a\\x62${7}c`; export const result = value;',
    provider: `export function tag(strings) {
      const copy = Array.from(typeof strings === "string" ? [strings] : strings);
      if (copy === strings || copy.length !== 2 || copy[0] !== "ab" ||
          strings.raw[0] !== "a\\\\x62" || arguments.length !== 2 || arguments[1] !== 7) throw "tag";
      return copy[0] + arguments[1] + copy[1];
    }`,
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2"] }, order: [], sequential: true },
  });
  const out = path.join(dir, "out");
  const contract = path.join(dir, "browser.json");
  writeFileSync(contract, JSON.stringify({ profile: "browser-page" }));
  const options = ["--host-contract", contract];
  const checked = lower(files.sourceFile, files.specFile, out, options);
  assertCertifiedResult({ ...checked, files, out }, "ab7c");
  assert(checked.verdict.premises.some((a) => a.includes("intrinsic Array constructor")));
  assert(checked.verdict.premises.some((a) => a.includes("intrinsic Array.from function")));
  const verify = () => lower(files.sourceFile, files.specFile, out, [...options, "--check-output"]);
  assert.equal(verify().verdict.status, "certified");
  const entry = path.join(out, "entry.mjs");
  const printed = readFileSync(entry, "utf8");
  assert(printed.includes("a\\x62"));
  writeFileSync(entry, printed.replace("a\\x62", "ab"));
  assert.equal(verify().verdict.status, "rejected");
}

if (runCase("order-and-invalid-escape")) {
  const source = [
    "const run = () => {",
    '  const trace = { log: "" };',
    '  const receiver = { get tag() { trace.log += "g"; return function (s, v) {',
    '    if (this !== receiver || s[0] !== undefined || s.raw[0] !== "\\\\xZ") throw "receiver or cooked";',
    '    trace.log += "t"; return v; }; } };',
    '  const step = () => { trace.log += "s"; return 4; };',
    "  const value = receiver.tag`\\xZ${step()}`;",
    '  try { receiver.tag`a${(() => { throw 1; })()}`; } catch { trace.log += "c"; }',
    '  if (trace.log !== "gstgc") throw "order";',
    "  return [value, trace.log]; };",
    "export const result = run();",
  ].join("\n");
  assertCertifiedResult(workerSplit("tag-order", source), [4, "gstgc"]);
}
