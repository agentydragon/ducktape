import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { generateBindings, validateBindings } from "./bindings.mjs";

// Construct ESTree nodes first, then print the subset of JavaScript used here.
// Adding grammar productions does not require changing the splitter's parser.
const id = (name) => ({ type: "Identifier", name });
const literal = (value) => ({ type: "Literal", value });
const expr = (expression) => ({ type: "ExpressionStatement", expression });
const call = (callee, args = []) => ({ type: "CallExpression", callee, arguments: args });
const variable = (name, value, kind = "let") => ({
  type: "VariableDeclaration",
  kind,
  declarations: [{ type: "VariableDeclarator", id: id(name), init: literal(value) }],
});
const fn = (name, body) => ({
  type: "FunctionDeclaration",
  id: id(name),
  params: [],
  body: { type: "BlockStatement", body: [body] },
});
const add = (target, source) =>
  expr({ type: "AssignmentExpression", operator: "+=", left: id(target), right: id(source) });
const reader = (name) => ({ type: "ReturnStatement", argument: id(name) });
const record = (name) =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [id(name)])
  );

function print(node) {
  switch (node.type) {
    case "Program":
      return node.body.map(print).join("\n") + "\n";
    case "Identifier":
      return node.name;
    case "Literal":
      return JSON.stringify(node.value);
    case "VariableDeclaration":
      return `${node.kind} ${node.declarations.map(print).join(", ")};`;
    case "VariableDeclarator":
      return `${print(node.id)} = ${print(node.init)}`;
    case "FunctionDeclaration":
      return `function ${print(node.id)}() ${print(node.body)}`;
    case "BlockStatement":
      return `{ ${node.body.map(print).join(" ")} }`;
    case "ReturnStatement":
      return `return ${print(node.argument)};`;
    case "ExpressionStatement":
      return `${print(node.expression)};`;
    case "AssignmentExpression":
      return `${print(node.left)} ${node.operator} ${print(node.right)}`;
    case "CallExpression":
      return `${print(node.callee)}(${node.arguments.map(print).join(", ")})`;
    case "MemberExpression":
      return `${print(node.object)}.${print(node.property)}`;
    case "ExportNamedDeclaration":
      return `export { ${node.specifiers.map(print).join(", ")} };`;
    case "ExportSpecifier":
      return print(node.local);
    case "IfStatement":
      return `if (${print(node.test)}) ${print(node.consequent)}`;
    case "EmptyStatement":
      return ";";
    case "ArrowFunctionExpression":
      return `() => ${print(node.body)}`;
    default:
      throw new Error(`unsupported generated AST node: ${node.type}`);
  }
}

function random(seed, index) {
  let state = (seed ^ Math.imul(index + 1, 0x9e3779b9)) >>> 0;
  return (limit) => {
    state = (state + 0x6d2b79f5) >>> 0;
    let n = state;
    n = Math.imul(n ^ (n >>> 15), n | 1);
    n ^= n + Math.imul(n ^ (n >>> 7), n | 61);
    return ((n ^ (n >>> 14)) >>> 0) % limit;
  };
}
function shuffle(items, rand) {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = rand(i + 1);
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// Generate declarations and effects independently of their module placement.
// Mode changes how effects are distributed, not the program grammar.
export function makeCase(seed, index) {
  const rand = random(seed, index);
  const mode = index % 4;
  const count = 2 + rand(3);
  const state = "state.mjs";
  const others = Array.from({ length: 1 + rand(3) }, (_, i) => `part${i}.mjs`);
  const entry = "entry.mjs";
  const modules = Object.fromEntries([state, ...others, entry].map((name) => [name, { imports: [], statements: [] }]));
  for (const name of others) modules[name].imports = [state];
  modules[entry].imports = shuffle([state, ...others], rand);
  const cells = Array.from({ length: count }, (_, i) => `cell${i}`);
  const bumps = Array.from({ length: count }, (_, i) => `bump${i}`);
  const getters = Array.from({ length: count }, (_, i) => `get${i}`);
  const ranked = [state, ...others, entry];
  const owners = cells.map(() => (mode === 2 ? rand(ranked.length - 1) : 0));
  const dependency = (owner, target) => {
    if (owner !== target && !modules[owner].imports.includes(target)) modules[owner].imports.push(target);
  };
  const chooseOwner = (minimum) => (mode === 2 ? minimum + rand(ranked.length - minimum) : 1 + rand(others.length));
  const declarations = [];
  for (let i = 0; i < count; i++) {
    const owner = ranked[owners[i]];
    const candidates = cells.map((_, j) => j).filter((j) => owners[j] <= owners[i]);
    const source = candidates[rand(candidates.length)];
    dependency(owner, ranked[owners[source]]);
    const readerOwner = ranked[chooseOwner(owners[i])];
    dependency(readerOwner, owner);
    declarations.push([variable(cells[i], 1 + rand(8)), owner]);
    declarations.push([fn(bumps[i], add(cells[i], cells[source])), owner]);
    declarations.push([fn(getters[i], reader(cells[i])), readerOwner]);
  }
  const statements = shuffle(declarations, rand);
  const calls = 1 + rand(3);
  const records = 1 + rand(3);
  // A call in entry executes after a record in an imported module. Its source
  // order is the opposite; the selected cell guarantees an observable witness.
  const selected = rand(count);
  const placeCall = (i, owner) => {
    dependency(owner, ranked[owners[i]]);
    statements.push([expr(call(id(bumps[i]))), owner]);
  };
  const placeRecord = (i, owner) => {
    dependency(owner, ranked[owners[i]]);
    statements.push([record(cells[i]), owner]);
  };
  placeCall(selected, mode === 1 ? entry : mode === 2 ? ranked[chooseOwner(owners[selected])] : state);
  for (let i = 1; i < calls; i++) {
    const cell = rand(count);
    placeCall(cell, mode === 2 ? ranked[chooseOwner(owners[cell])] : state);
  }
  placeRecord(selected, mode === 2 ? ranked[chooseOwner(owners[selected])] : others[0]);
  for (let i = 1; i < records; i++) {
    const cell = rand(count);
    placeRecord(cell, mode === 2 ? ranked[chooseOwner(owners[cell])] : others[rand(others.length)]);
  }
  if (mode === 3) {
    // Valid JavaScript outside the modeled grammar. Different AST productions
    // exercise refusal rather than silently narrowing the fuzzer to supported JS.
    const extra = [
      variable("extra", rand(8), "const"),
      { type: "IfStatement", test: literal(false), consequent: { type: "BlockStatement", body: [] } },
      expr({ type: "ArrowFunctionExpression", body: literal(rand(8)) }),
      { type: "EmptyStatement" },
    ][rand(4)];
    statements.push([extra, entry]);
  }
  const publicNames = shuffle([...bumps, ...getters], rand);
  const ast = { type: "Program", sourceType: "module", body: statements.map(([node]) => node) };
  ast.body.push({
    type: "ExportNamedDeclaration",
    specifiers: publicNames.map((name) => ({ type: "ExportSpecifier", local: id(name), exported: id(name) })),
  });
  statements.forEach(([, owner], i) => modules[owner].statements.push(i));
  const cyclic = index % 16 === 6;
  const invalidAssignment = index % 16 === 14;
  if (cyclic) modules[state].imports.push(entry);
  if (invalidAssignment) modules[entry].statements.push(0);
  const observations = [];
  for (let i = 0; i < 2 + rand(4); i++) observations.push(getters[rand(count)], bumps[rand(count)]);
  observations.push(...getters);
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry, modules },
    calls: observations,
    cell0Owner: ranked[owners[0]],
    unsupported: mode === 3,
    cyclic,
    invalidAssignment,
  };
}

export function execute(path, calls) {
  const code = `const events=[];globalThis.record=v=>events.push(v);let phase='import';let index=-1;let names=[];const results=[];try{const m=await import(${JSON.stringify(pathToFileURL(path).href)});names=Object.keys(m);phase='call';for(const n of ${JSON.stringify(calls)}){index++;const v=m[n]();results.push(v===undefined?{undefined:true}:{value:v});}console.log(JSON.stringify({events,exports:names,results,error:null}));}catch(e){console.log(JSON.stringify({events,exports:names,results,error:{name:e.name,phase,index}}));}`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8", timeout: 10000 });
  if (child.error || child.status !== 0) throw new Error(`Node oracle failed: ${child.error ?? child.stderr}`);
  return JSON.parse(child.stdout);
}

// Every generated case is reproducible independently from seed and index.
// Results and all emitted modules are retained on an invariant failure.
export async function runFuzz({ seed, start = 0, cases, outputDir, validateEvery = 1, shard = 0, shards = 1 }) {
  await mkdir(outputDir, { recursive: true });
  const summary = {
    seed,
    start,
    cases: 0,
    generated: 0,
    refused: 0,
    compared: 0,
    equal: 0,
    different: 0,
    mutated: 0,
    splitCompared: 0,
    splitDifferent: 0,
    mutatedDifferent: 0,
    validated: 0,
    proved: 0,
    unsafe: 0,
    unknown: 0,
    unvalidated: 0,
    unexplainedDifferences: [],
  };
  for (let index = start; index < start + cases; index++) {
    if (index % shards !== shard) continue;
    summary.cases++;
    const root = await mkdtemp(join(outputDir, `case-${index}-`));
    const source = join(root, "source.mjs");
    const spec = join(root, "spec.json");
    const out = join(root, "out");
    const input = makeCase(seed, index);
    let keep = true;
    try {
      await writeFile(source, input.source);
      await writeFile(spec, JSON.stringify(input.spec, null, 2) + "\n");
      await writeFile(
        join(root, "case.json"),
        JSON.stringify({ seed, index, calls: input.calls, unsupported: input.unsupported }, null, 2) + "\n"
      );
      try {
        await generateBindings(source, spec, out);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
        summary.refused++;
        if (!input.unsupported && !input.invalidAssignment)
          throw new Error(`supported generated case was refused: ${error}`);
        keep = false;
        continue;
      }
      if (input.unsupported || input.invalidAssignment) throw new Error("unsupported input unexpectedly accepted");
      summary.generated++;
      const mutated = index % 8 === 2;
      if (mutated) {
        const file = join(out, input.cell0Owner);
        const original = await readFile(file, "utf8");
        const changed = original.replace(/let cell0 = (\d+);/, (_, n) => `let cell0 = ${Number(n) + 1};`);
        if (changed === original) throw new Error("mutation failed");
        await writeFile(file, changed);
        summary.mutated++;
      }
      const before = execute(source, input.calls);
      const after = execute(join(out, input.spec.entry), input.calls);
      const equal = JSON.stringify(before) === JSON.stringify(after);
      summary.compared++;
      summary[equal ? "equal" : "different"]++;
      if (!mutated) summary.splitCompared++;
      if (!equal) summary[mutated ? "mutatedDifferent" : "splitDifferent"]++;
      await writeFile(join(root, "node.json"), JSON.stringify({ before, after }, null, 2) + "\n");
      let verdictName = "unvalidated";
      if (random(seed ^ 0xa5a5a5a5, index)(validateEvery) === 0) {
        const verdict = await validateBindings(source, spec, out);
        summary.validated++;
        verdictName = verdict.result;
        if (input.cyclic && verdict.result !== "unknown") throw new Error("cycle unexpectedly accepted");
        await writeFile(join(root, "verdict.json"), JSON.stringify(verdict, null, 2) + "\n");
        if (verdict.result === "proved under assumptions") {
          summary.proved++;
          if (!equal || before.error || after.error) throw new Error("proved case differs or fails in Node");
        } else if (verdict.result === "unsafe") {
          summary.unsafe++;
          if (
            equal ||
            JSON.stringify(verdict.original) !== JSON.stringify({ events: before.events, exports: before.exports }) ||
            JSON.stringify(verdict.output) !== JSON.stringify({ events: after.events, exports: after.exports })
          )
            throw new Error("unsafe witness does not match Node");
        } else if (verdict.result === "unknown") summary.unknown++;
        else throw new Error(`unexpected validator result: ${verdict.result}`);
      } else summary.unvalidated++;
      keep = !equal && (verdictName === "unknown" || verdictName === "unvalidated");
      if (keep) summary.unexplainedDifferences.push({ index, mutated, verdict: verdictName, artifacts: root });
    } catch (error) {
      throw new Error(`seed=${seed} index=${index}; artifacts: ${root}\n${error.stack ?? error}`);
    } finally {
      if (!keep) await rm(root, { recursive: true, force: true });
    }
  }
  return summary;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  try {
    const args = process.argv.slice(2);
    if (args.length % 2) throw new Error("options require values");
    const opts = Object.fromEntries(Array.from({ length: args.length / 2 }, (_, i) => [args[2 * i], args[2 * i + 1]]));
    if (
      Object.keys(opts).some(
        (key) => !["--seed", "--start", "--cases", "--output-dir", "--validate-every"].includes(key)
      )
    )
      throw new Error("unknown option");
    const seed = Number(opts["--seed"] ?? 1),
      start = Number(opts["--start"] ?? 0),
      cases = Number(opts["--cases"] ?? 100),
      validateEvery = Number(opts["--validate-every"] ?? 1);
    if (
      ![seed, start, cases, validateEvery].every(Number.isSafeInteger) ||
      seed < 0 ||
      seed > 0xffffffff ||
      start < 0 ||
      cases < 1 ||
      validateEvery < 1 ||
      !opts["--output-dir"]
    )
      throw new Error("usage: fuzz.mjs --output-dir DIR [--seed UINT32] [--start N] [--cases N] [--validate-every N]");
    console.log(
      JSON.stringify(
        await runFuzz({ seed, start, cases, validateEvery, outputDir: resolve(opts["--output-dir"]) }),
        null,
        2
      )
    );
  } catch (error) {
    console.error(error.stack ?? String(error));
    process.exitCode = 1;
  }
}
