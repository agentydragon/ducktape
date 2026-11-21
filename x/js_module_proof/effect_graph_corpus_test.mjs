import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

if (process.env.ACORN_RLOCATION) {
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
}
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const { generateEffects, validateEffects } = await import("./effects.mjs");
const modelDir = dirname(fileURLToPath(import.meta.url));
const model =
  (await readFile(join(modelDir, "Semantics.lean"), "utf8")) +
  "\n" +
  (await readFile(join(modelDir, "Effects.lean"), "utf8"));
const shard = Number(process.env.TEST_SHARD_INDEX ?? "0");
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? "1");
const names = ["a.mjs", "b.mjs", "c.mjs", "entry.mjs"];
const graphs = [
  [[], [0], [0], [1, 2]],
  [[], [0], [0], [2, 1]],
  [[], [0], [1], [2]],
  [[], [0, 0], [0], [1, 2]],
];
const list = (items) => `[${items.join(", ")}]`;
const strings = (items) => list(items.map((item) => JSON.stringify(item)));

function run(entry, throwAt) {
  const code = `const events=[]; globalThis.record=value=>{events.push(value);if(events.length===${throwAt})throw "boom"}; import(${JSON.stringify(pathToFileURL(entry).href)}).then(()=>console.log(JSON.stringify({events,error:null})),error=>console.log(JSON.stringify({events,error})));`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

for (let index = 0; index < 12; index++) {
  if (index % shards !== shard) continue;
  const dir = await mkdtemp(join(process.env.TEST_TMPDIR, "effect-graph-"));
  const source = join(dir, "source.mjs");
  const specPath = join(dir, "spec.json");
  const output = join(dir, "out");
  const labels = Array.from({ length: 4 }, (_, i) => `case${index}event${i}`);
  const text = labels
    .map((event, i) => (i % 2 ? `globalThis.record('${event}'); // comment` : `globalThis . record ("${event}");`))
    .join("\n");
  await writeFile(source, `${text}\n`);
  const graph = graphs[index % graphs.length];
  const modules = Object.fromEntries(
    names.map((name, i) => [
      name,
      {
        imports: graph[i].map((dependency) => names[dependency]),
        statements: [i],
      },
    ])
  );
  const spec = { kind: "effects", entry: "entry.mjs", modules };
  await writeFile(specPath, JSON.stringify(spec));
  await generateEffects(source, specPath, output);

  // Imports are declarations: moving them below a call does not delay the
  // dependency. Preserve their relative order while changing their position.
  if (index % 2 === 0) {
    const path = join(output, "b.mjs");
    const lines = (await readFile(path, "utf8")).trim().split("\n");
    const imports = lines.filter((line) => line.startsWith("import "));
    const effects = lines.filter((line) => !line.startsWith("import "));
    await writeFile(path, `${[...effects, ...imports].join("\n")}\n`);
  }
  // This case is intentionally outside the assignment, but its module graph
  // still has a trace that can be checked against Node independently.
  if (index % 4 === 2) {
    const path = join(output, "b.mjs");
    await writeFile(path, (await readFile(path, "utf8")).replace(labels[1], `changed${index}`));
  }

  const modulesFromFiles = [];
  for (const name of names) {
    const body = parse(await readFile(join(output, name), "utf8"), {
      ecmaVersion: "latest",
      sourceType: "module",
    }).body;
    const imports = body
      .filter((node) => node.type === "ImportDeclaration")
      .map((node) => names.indexOf(node.source.value.slice(2)));
    const events = body
      .filter((node) => node.type !== "ImportDeclaration")
      .map((node) => node.expression.arguments[0].value);
    assert.ok(imports.every((n) => n >= 0));
    modulesFromFiles.push(
      `({ imports := ${list(imports)}, expectedImports := [], assignment := [], effects := ${strings(events)} } : EffectModule)`
    );
  }

  const nodeResults = [];
  for (let throwAt = 0; throwAt <= 4; throwAt++) nodeResults.push(run(join(output, "entry.mjs"), throwAt));
  let proof = `\ndef corpusModules : List EffectModule := ${list(modulesFromFiles)}\n`;
  proof += `theorem node_trace : effectTrace corpusModules 3 = some ${strings(nodeResults[0].events)} := by decide\n#print axioms node_trace\n`;
  for (let throwAt = 0; throwAt <= 4; throwAt++) {
    const replies = throwAt === 0 ? [] : [...Array(throwAt - 1).fill("none"), 'some "boom"'];
    const result = nodeResults[throwAt];
    const error = result.error === null ? "none" : `some ${JSON.stringify(result.error)}`;
    proof += `theorem node_throw_${throwAt} : outputBehavior corpusModules 3 (${list(replies)} : List (Option String)) = some (${strings(result.events)}, ${error}) := by decide\n#print axioms node_throw_${throwAt}\n`;
  }
  const lean = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: model + proof,
  });
  assert.equal(lean.status, 0, lean.stderr || lean.stdout);
  for (const theorem of ["node_trace", ...Array.from({ length: 5 }, (_, n) => `node_throw_${n}`)]) {
    assert.ok(lean.stdout.includes(`'${theorem}' does not depend on any axioms`), lean.stdout);
  }

  const result = await validateEffects(source, specPath, output);
  const expected = ["proved under assumptions", "unsafe", "unknown", "proved under assumptions"][index % 4];
  assert.equal(result.result, expected, JSON.stringify(result));
  assert.deepEqual(result.modeledOutput, nodeResults[0].events);
  if (result.result === "unsafe") {
    assert.deepEqual(result.output, nodeResults[0].events);
    assert.deepEqual(result.original, run(source, 0).events);
  }
  if (result.result === "unknown") assert.match(result.reason, /graph and assignment/);
}
