import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";

export function leanSpec(graph, emittedOrder) {
  const chunk = graph.chunk_id;
  const entry = "entry.mjs";
  const owners = new Map();
  const modules = {};
  assert(Array.isArray(graph.module_graph?.nodes), `${chunk}: missing owner table`);
  assert(Array.isArray(emittedOrder), `${chunk}: missing emitted order`);
  let entryCount = 0;
  for (const owner of graph.module_graph.nodes) {
    assert(typeof owner.key === "string" && owner.key, `${chunk}: missing owner key`);
    assert(!owners.has(owner.key), `${chunk}: duplicate owner key ${owner.key}`);
    if (owner.kind === "entry") entryCount++;
    else {
      assert(
        typeof owner.path === "string" &&
          !owner.path.includes("\\") &&
          !owner.path.split("/").some((part) => ["", ".", ".."].includes(part)),
        `${chunk}: invalid owner path ${owner.path}`
      );
    }
    const name = owner.kind === "entry" ? entry : owner.path + ".mjs";
    assert(!Object.hasOwn(modules, name), `${chunk}: duplicate output module ${name}`);
    owners.set(owner.key, name);
    modules[name] = [];
  }
  assert.equal(entryCount, 1, `${chunk}: expected one entry owner`);

  const nodes = graph.nodes.toSorted((a, b) => a.statement_ordinal - b.statement_ordinal);
  const byId = new Map();
  for (const [ordinal, node] of nodes.entries()) {
    assert.equal(node.statement_ordinal, ordinal, `${chunk}: missing or duplicate source ordinal ${ordinal}`);
    assert(typeof node.id === "string" && /^unit:\d+$/.test(node.id), `${chunk}: invalid source unit ID ${node.id}`);
    assert(!byId.has(node.id), `${chunk}: duplicate source unit ID ${node.id}`);
    const owner = owners.get(node.destination);
    assert(owner, `${chunk}: no owner ${node.destination} for source unit ${node.id}`);
    const selector = "#" + ordinal;
    byId.set(node.id, { owner, selector });
    modules[owner].push(selector);
  }

  // Rust's import priority and the emitter's evaluation order can differ.
  const order = new Set();
  const seen = new Set();
  const schedule = emittedOrder.map((id) => {
    const unit = byId.get("unit:" + id);
    assert(Number.isSafeInteger(id) && unit, `${chunk}: unknown emitted source unit ID ${id}`);
    assert(!seen.has(id), `${chunk}: repeated emitted source unit ID ${id}`);
    seen.add(id);
    if (unit.owner !== entry) order.add(unit.owner);
    return unit.selector;
  });
  assert.equal(seen.size, byId.size, `${chunk}: emitted order omits source units`);
  assert.equal(order.size + 1, owners.size, `${chunk}: emitted order omits nonentry modules`);
  assert.deepEqual(
    [...order, entry].flatMap((name) => modules[name]),
    schedule,
    `${chunk}: emitted order cannot be represented as consecutive modules`
  );
  return { entry, modules, order: [...order], sequential: true };
}

if (import.meta.main) {
  const args = process.argv.slice(2);
  assert.equal(args.length, 2, "usage: generate_specs.mjs <spec-graphs> <output-directory>");
  const execRoot = process.env.JS_BINARY__EXECROOT ?? process.cwd();
  const [graphs, out] = args.map((arg) => path.resolve(execRoot, arg));
  mkdirSync(out, { recursive: true });
  for (const chunk of ["editor", "main"]) {
    const reports = path.join(graphs, "reports/tree", chunk);
    const graph = JSON.parse(gunzipSync(readFileSync(path.join(reports, "dependency_graph.json.gz"))));
    assert.equal(graph.chunk_id, chunk);
    const effects = JSON.parse(readFileSync(path.join(reports, "effect_translation_validation.json"), "utf8"));
    writeFileSync(
      path.join(out, chunk + ".json"),
      JSON.stringify(leanSpec(graph, effects.emitted_order), null, 2) + "\n"
    );
  }
}
