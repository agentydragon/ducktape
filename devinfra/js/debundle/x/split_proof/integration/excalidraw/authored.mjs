import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";
import { assertSelectedSourceUnits } from "../../translation/testing/ast_assertions.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { splitInputs } from "../../translation/split_dump.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
for (const [chunk, spec] of [
  ["editor", process.env.EDITOR_LEAN_SPEC],
  ["main", process.env.MAIN_LEAN_SPEC],
]) {
  const dir = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, chunk);
  mkdirSync(dir, { recursive: true });
  const reports = path.join(process.env.SPEC_GRAPHS, "reports/tree", chunk);
  const graph = JSON.parse(gunzipSync(readFileSync(path.join(reports, "dependency_graph.json.gz"))));
  assert.equal(graph.chunk_id, chunk);
  const validation = JSON.parse(readFileSync(path.join(reports, "translation_validation.json"), "utf8"));
  assert(validation.gate_accepts && validation.accepts);
  const source = readFileSync(path.join(chunks, chunk + ".js"), "utf8");
  const { dump, core, units } = splitInputs(source, { external: true, census: true });
  const nodes = graph.nodes.sort((a, b) => a.statement_ordinal - b.statement_ordinal);
  assert.equal(nodes.length, units.length);
  const entry = "entry.mjs";
  const owners = new Map(graph.module_graph.nodes.map((m) => [m.key, m.kind === "entry" ? entry : m.path + ".mjs"]));
  for (const [i, node] of nodes.entries()) {
    assert.equal(node.statement_ordinal, i);
    const ast = units[i].node;
    const binding = ast.declarations?.[0]?.id ?? ast.id;
    if (binding?.type === "Identifier")
      assert(
        node.declared_bindings.some((b) => b.binding === binding.name),
        chunk + " unit " + i
      );
  }
  // Rust's priority orders imports. The emitter's depth-first traversal determines execution order.
  const effects = JSON.parse(readFileSync(path.join(reports, "effect_translation_validation.json"), "utf8"));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const schedule = effects.emitted_order.map((id) => byId.get("unit:" + id).statement_ordinal);
  assert.deepEqual(
    schedule.toSorted((a, b) => a - b),
    nodes.map((node) => node.statement_ordinal)
  );
  const write = (name, value) => {
    const file = path.join(dir, name + ".json");
    writeFileSync(file, JSON.stringify(value));
    return file;
  };
  const ast = write("ast", dump);
  const model = write("program", {
    entry: "main",
    modules: [{ name: "main", imports: [], exports: core.exports, ids: core.ids, tokens: core.tokens }],
  });
  // Request a plan even on rejection so we can verify the schedule Lean actually checked.
  const rendered = path.join(process.env.TEST_TMPDIR, chunk);
  for (const name of owners.values()) mkdirSync(path.join(rendered, path.dirname(name)), { recursive: true });
  const planFile = path.join(dir, "plan.json");
  const r = spawnSync(
    process.env.SPLIT_PREVIEW,
    [
      writeDeployment(ast, spec, rendered),
      rendered,
      "--corejs",
      model,
      "--plan",
      planFile,
      "--force",
      "--move-report",
      path.join(dir, "moves.json"),
    ],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }
  );
  if (r.error) throw r.error;
  writeFileSync(path.join(dir, "checker.log"), r.stdout + r.stderr);
  assert([0, 1].includes(r.status), r.stderr);
  const plan = JSON.parse(readFileSync(planFile, "utf8"));
  assert.deepEqual(
    plan.order.flatMap((name) => plan.modules[name]),
    schedule
  );
  assert.deepEqual(Object.keys(plan.modules).sort(), [...owners.values()].sort());
  const planOwners = Object.entries(plan.modules).flatMap(([name, ordinals]) =>
    ordinals.map((ordinal) => [ordinal, name])
  );
  assert.deepEqual(
    planOwners.toSorted(([a], [b]) => a - b),
    nodes.map((node) => [node.statement_ordinal, owners.get(node.destination)])
  );
  for (const [modulePath, ordinals] of Object.entries(plan.modules)) {
    const emittedText = readFileSync(path.join(rendered, modulePath), "utf8");
    assertSelectedSourceUnits(
      units.map((unit) => unit.node),
      emittedText,
      ordinals,
      `${chunk}/${modulePath}`
    );
  }
  const slice = r.stdout.split("\n").find((line) => line.includes("model diagnostic:"));
  assert(slice, r.stderr);
  const moves = JSON.parse(readFileSync(path.join(dir, "moves.json"), "utf8"));
  const summary = {
    chunk,
    units: nodes.length,
    nonentryModules: owners.size - 1,
    moved: nodes.filter((node) => owners.get(node.destination) !== entry).length,
    rustInversions: validation.swapped_pairs,
    structural: r.status === 0,
    failures: r.stdout.split("\n").filter((line) => line.includes("FAIL")),
    slice: slice.trim(),
    moveReportError: moves.error,
    invertedPairsWithoutImports: moves.invertedPairs,
    failedPairsWithoutImports: moves.failedPairs,
    firstFailedPairWithoutImports: moves.failedPairExamples?.[0],
  };
  write("result", summary);
  console.log(JSON.stringify(summary));
}
