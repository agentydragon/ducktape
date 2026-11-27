import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";

const placement = {
  entry: "entry.mjs",
  order: ["part.mjs"],
  sequential: true,
  modules: { "part.mjs": ["#0"], "entry.mjs": ["#1"] },
};

function observe(file, expected) {
  const child = spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      `import assert from "node:assert/strict";
     const ns = await import(process.argv[1]);
     assert.deepEqual(Object.keys(ns), ["y"]);
     assert.equal(ns.y, ${expected});`,
      pathToFileURL(file).href,
    ],
    { encoding: "utf8" }
  );
  assert.ifError(child.error);
  assert.equal(child.status, 0, child.stdout + child.stderr);
}

for (const [name, text, expected] of [
  ["literal", "var x=1; export const y=x;", "1"],
  ["hoisted-self-read", "var x=x; export const y=x;", "undefined"],
]) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  mkdirSync(dir);
  const source = path.join(dir, "source.mjs");
  const spec = path.join(dir, "placement.json");
  const out = path.join(dir, "out");
  writeFileSync(source, text);
  writeFileSync(spec, JSON.stringify(placement));
  const request = { source, spec, out, require: "all-outcomes" };
  const result = split(request);
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.verdict.strategy, "compiled-constructor");
  assert.equal(result.verdict.guarantee, "all-outcomes");
  assert.equal(result.verdict.every_ending_matches, true);
  const partFile = path.join(out, "part.mjs");
  const entryFile = path.join(out, "entry.mjs");
  const part = readFileSync(partFile, "utf8");
  const entry = readFileSync(entryFile, "utf8");
  assert.match(part, name === "literal" ? /\bvar x = 1;/ : /\bvar x = x;/);
  assert.match(entry, /import \{\s*x\s*\} from "\.\/part\.mjs"/);
  assert.match(entry, /const y = x;/);
  assert.equal(split({ ...request, checkOutput: true }).status, "certified");
  observe(source, expected);
  observe(entryFile, expected);

  // The imported cell has the same value as y in both cases but is not the exported cell.
  const wrong = entry.replace(/export \{[^}]*\}/, "export { x as y }");
  assert.notEqual(wrong, entry);
  writeFileSync(entryFile, wrong);
  observe(entryFile, expected);
  assert.equal(split({ ...request, checkOutput: true }).status, "refused");
  assert.equal(readFileSync(entryFile, "utf8"), wrong);

  if (name === "hoisted-self-read") {
    const lexical = path.join(dir, "lexical.mjs");
    writeFileSync(lexical, text.replace(/^var /, "const "));
    const tdz = spawnSync(process.execPath, [lexical], { encoding: "utf8" });
    assert.ifError(tdz.error);
    assert.equal(tdz.status, 1);
    assert.match(tdz.stderr, /ReferenceError/);
  } else {
    const wrongOwner = path.join(dir, "wrong-owner.json");
    writeFileSync(wrongOwner, JSON.stringify({ ...placement, modules: { "part.mjs": ["#1"], "entry.mjs": ["#0"] } }));
    const rejected = path.join(dir, "wrong-owner");
    assert.equal(split({ ...request, out: rejected, spec: wrongOwner }).status, "refused");
    assert.ok(!existsSync(rejected));
  }
}
