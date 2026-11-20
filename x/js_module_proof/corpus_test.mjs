import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

if (process.env.ACORN_RLOCATION) {
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
}
const { generate, validate } = await import("./checker.mjs");
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");

const cases = Array.from({ length: 8 }, (_, i) => ({
  cell: `cell${i}`,
  inc: `increment${i}`,
  read: `current${i}`,
  initial: [0, 7, Number.MAX_SAFE_INTEGER - 2][i % 3],
  alias: i % 2 === 1,
  snapshot: i % 4 === 3,
}));
const shard = Number(process.env.TEST_SHARD_INDEX ?? "0");
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? "1");
assert.ok(shard >= 0 && shard < shards);

for (const [i, c] of cases.entries()) {
  if (i % shards !== shard) continue;
  const dir = await mkdtemp(join(process.env.TEST_TMPDIR, "corpus-"));
  const sourcePath = join(dir, "original.mjs");
  const specPath = join(dir, "assignment.json");
  const outputDir = join(dir, "out");
  const cell = `let ${c.cell} = ${c.initial};`;
  const inc = `function ${c.inc}() { ${c.cell} += 1; }`;
  const read = `function ${c.read}() { return ${c.cell}; }`;
  const declarations = i % 2 === 0 ? [cell, inc, read] : [read, inc, cell];
  await writeFile(sourcePath, `${declarations.join("\n")}\nexport { ${c.inc}, ${c.read} };\n`);
  await writeFile(specPath, JSON.stringify({ "store.mjs": [c.cell, c.inc], "view.mjs": [c.read] }));
  await generate(sourcePath, specPath, outputDir);

  const readerPath = join(outputDir, "view.mjs");
  let reader = await readFile(readerPath, "utf8");
  const local = c.alias ? "importedCell" : c.cell;
  if (c.alias)
    reader = reader
      .replace(`{ ${c.cell} }`, `{ ${c.cell} as ${local} }`)
      .replace(`return ${c.cell};`, `return ${local};`);
  if (c.snapshot)
    reader = reader
      .replace("export function", `const saved = ${local};\nexport function`)
      .replace(`return ${local};`, "return saved;");
  await writeFile(readerPath, reader);

  function execute(path, calls) {
    const url = pathToFileURL(path).href;
    const code = `import(${JSON.stringify(url)}).then(m => { const results = []; for (const call of ${JSON.stringify(calls)}) { if (call === "increment") m[${JSON.stringify(c.inc)}](); else results.push(m[${JSON.stringify(c.read)}]()); } console.log(JSON.stringify(results)); });`;
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  }

  const result = await validate(sourcePath, specPath, outputDir);
  const entry = join(outputDir, "entry.mjs");
  if (c.snapshot) {
    assert.equal(result.result, "unsafe", JSON.stringify(result));
    assert.deepEqual(execute(sourcePath, result.calls), [result.original]);
    assert.deepEqual(execute(entry, result.calls), [result.output]);
    assert.notEqual(result.original, result.output);
  } else {
    assert.equal(result.result, "proved under assumptions", JSON.stringify(result));
    for (const calls of [["current"], ["increment", "current"], ["increment", "current", "increment", "current"]]) {
      assert.deepEqual(execute(sourcePath, calls), execute(entry, calls));
    }
  }
}
