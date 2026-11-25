import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { dump } from "../parser.mjs";

const root = process.env.TEST_TMPDIR;
const input = path.join(root, "input.json");
const placement = path.join(root, "placement.json");
function prepare(source, modules, order, entry = "entry.mjs") {
  const ast = dump(source);
  fs.writeFileSync(input, JSON.stringify(ast));
  fs.writeFileSync(placement, JSON.stringify({ entry, order, sequential: true, modules }));
  return ast;
}
function requestFile(file, out, ...args) {
  const result = spawnSync(process.env.LOWERSPLIT, [writeDeployment(file, placement, out), out, ...args], {
    encoding: "utf8",
  });
  if (result.error) throw result.error;
  const diagnostics = result.stdout + result.stderr;
  const verdict = JSON.parse(result.stdout.trim().split("\n").at(-1));
  assert.equal(result.stdout.trim().split("\n").length, 1, diagnostics);
  assert(["certified", "rejected"].includes(verdict.status), diagnostics);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, diagnostics);
  if (verdict.status === "certified") {
    assert.equal(verdict.guarantee, "normal-equivalence", diagnostics);
    assert.equal(typeof verdict.strategy, "string", diagnostics);
    assert(Array.isArray(verdict.premises), diagnostics);
    assert.equal(typeof verdict.every_ending_matches, "boolean", diagnostics);
  }
  if (verdict.status === "rejected") {
    assert.equal(verdict.guarantee, null);
    assert(verdict.reason, diagnostics);
  }
  return verdict;
}
function request(out, ...args) {
  return requestFile(input, out, ...args);
}
function check(out, status, ...args) {
  const verdict = request(out, ...args);
  assert.equal(verdict.status, status, JSON.stringify(verdict));
  return verdict;
}
function requestWithStrategy(strategy, out) {
  const result = spawnSync(process.env.LOWERSPLIT, [writeDeployment(input, placement, out), out], {
    encoding: "utf8",
    env: {
      ...process.env,
      SPLIT_PROOF_STRATEGY_CENSUS: "1",
      SPLIT_PROOF_CENSUS_STRATEGY: strategy,
    },
  });
  if (result.error) throw result.error;
  const diagnostics = result.stdout + result.stderr;
  const verdict = JSON.parse(result.stdout.trim().split("\n").at(-1));
  assert.equal(result.stdout.trim().split("\n").length, 1, diagnostics);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, diagnostics);
  return verdict;
}
async function observe(url) {
  try {
    return Object.entries(await import(url));
  } catch (e) {
    return e.name;
  }
}
const entry = "entry.mjs";
const left = "left.mjs";
const right = "right.mjs";
const source = "const a = 1; const b = 2; export const result = a;";
const out = path.join(root, "mutations");
const placement3 = { [entry]: ["#2"], [left]: ["#0"], [right]: ["#1"] };
const ast = prepare(source, placement3, [left, right]);
const orderedVerdict = check(out, "certified");
assert.equal(orderedVerdict.strategy, "structural");
assert.equal(orderedVerdict.guarantee, "normal-equivalence");
assert.equal(orderedVerdict.every_ending_matches, true);
// The ordinary JavaScript entry point and the annotated dump share the same checker.
const sourceFile = path.join(root, "source.mjs");
const rawOutput = path.join(root, "from-source");
fs.writeFileSync(sourceFile, source);
assert.equal(requestFile(sourceFile, rawOutput).status, "certified");
assert.equal(requestFile(sourceFile, out, "--check-output").status, "certified");
assert.deepEqual(await observe(pathToFileURL(path.join(rawOutput, entry))), await observe(pathToFileURL(sourceFile)));
const entryFile = path.join(out, entry);
const emitted = fs.readFileSync(entryFile, "utf8");
const imports = emitted.split("\n").filter((line) => line.startsWith("import "));
assert.equal(imports.length, 2, emitted);
for (const changed of [
  emitted.replace("result = a", "result = 0"),
  emitted.replace("export const", "const"),
  emitted.replace("result =", "different ="),
  emitted.replace("./left.mjs", "./right.mjs"),
  emitted.replace(/\ba\b/, "a as shadow"),
  emitted.replace(imports.join("\n"), imports.toReversed().join("\n")),
  emitted + "\nconst extra = 1;\n",
  emitted + "\nexport {",
]) {
  assert.notEqual(changed, emitted);
  fs.writeFileSync(entryFile, changed);
  check(out, "rejected", "--check-output");
}
fs.writeFileSync(entryFile, "// harmless formatting change\n" + emitted);
check(out, "certified", "--check-output");
const extra = path.join(out, "extra.mjs");
fs.writeFileSync(extra, "");
check(out, "rejected", "--check-output");
fs.unlinkSync(extra);
fs.renameSync(path.join(out, left), extra);
check(out, "rejected", "--check-output");
fs.renameSync(extra, path.join(out, left));

// Publication must leave a previously populated destination intact on failure.
const sentinel = path.join(out, "unrelated");
fs.writeFileSync(sentinel, "keep");
check(out, "rejected");
assert.equal(fs.readFileSync(sentinel, "utf8"), "keep");
fs.unlinkSync(sentinel);
ast.sourceText = source.replace("= 1", "= 3");
fs.writeFileSync(input, JSON.stringify(ast));
assert.match(check(out, "rejected", "--check-output").reason, /fresh parse/);

// Compose declarations, unary expressions, dependency chains and partition boundaries.
// Every seed checks Node's actual namespace (or abrupt outcome), then changes a generated
// initializer without changing the layout. The model must catch each syntactically valid mutant.
for (let seed = 0; seed < 16; seed++) {
  const length = 3 + (seed % 5);
  const cut = seed % 4 === 0 ? 2 : 1 + (seed % (length - 1));
  const name = (i) => "v" + seed + "_" + i;
  const decls = [];
  for (let i = 0; i < length; i++) {
    const earlier = (seed + i * 3) % Math.max(i, 1);
    const operand = i === 0 ? (seed % 4 === 0 ? name(1) : ["false", "-0", "null", '"abc"'][seed % 4]) : name(earlier);
    const kind = i === 1 && seed % 4 === 0 ? "let" : ["var", "const", "let"][(seed + i) % 3];
    decls.push(
      (i === length - 1 ? "export " : "") + kind + " " + name(i) + " = " + "!".repeat((seed + i) % 4) + operand + ";"
    );
  }
  const code = decls.join("\n");
  const selectors = decls.map((_, i) => "#" + i);
  const generated = path.join(root, "seed-" + seed);
  prepare(code, { [left]: selectors.slice(0, cut), [entry]: selectors.slice(cut) }, [left]);
  const verdict = check(generated, "certified");
  assert.equal(verdict.guarantee, "normal-equivalence", code);
  assert.equal(verdict.strategy, "structural", code);
  assert.equal(verdict.every_ending_matches, true, code);
  assert.deepEqual(
    await observe(pathToFileURL(path.join(generated, entry))),
    await observe("data:text/javascript," + encodeURIComponent(code)),
    code
  );
  const file = path.join(generated, seed % 2 ? entry : left);
  const contents = fs.readFileSync(file, "utf8");
  const changed = contents.replace(/ = [^;]+/, " = 12345");
  assert.notEqual(changed, contents);
  fs.writeFileSync(file, changed);
  check(generated, "rejected", "--check-output");
}
// A printer can keep consecutive module-scope declarators in one statement.
prepare("const a = 1, b = a; export const result = b;", { [entry]: ["#2"], [left]: ["#0", "#1"] }, [left]);
const groupedOut = path.join(root, "grouped-declarators");
check(groupedOut, "certified");
const groupedFile = path.join(groupedOut, left);
const groupedCode = fs.readFileSync(groupedFile, "utf8").replace("const a = 1;\nconst b = a;", "const a = 1, b = a;");
assert.match(groupedCode, /const a = 1, b = a;/);
fs.writeFileSync(groupedFile, groupedCode);
check(groupedOut, "certified", "--check-output");
for (const changed of ["const b = a, a = 1;", "let a = 1, b = a;", "const a = 2, b = a;"]) {
  fs.writeFileSync(groupedFile, groupedCode.replace("const a = 1, b = a;", changed));
  check(groupedOut, "rejected", "--check-output");
}

// The linked strategies establish normal equivalence only, even when source
// and candidate keep the same unit order.
prepare("const value = 1; export const result = value;", { [entry]: ["#0", "#1"] }, []);
const replayOrderKept = requestWithStrategy("compiled-replay-premised", path.join(root, "replay-order-kept"));
assert.equal(replayOrderKept.status, "certified", JSON.stringify(replayOrderKept));
assert.equal(replayOrderKept.strategy, "compiled-replay-premised");
assert.equal(replayOrderKept.every_ending_matches, false);
const generatedOrderKept = requestWithStrategy("generated-proof", path.join(root, "generated-order-kept"));
assert.equal(generatedOrderKept.status, "certified", JSON.stringify(generatedOrderKept));
assert.equal(generatedOrderKept.strategy, "generated-proof");
assert.equal(generatedOrderKept.every_ending_matches, false);

// This cyclic placement needs the entry to expose a previously private binding to its peer.
// Even an order-preserving CoreJS plan with all nine structural checks cannot certify that change.
const leakingSource = "const first = later; var later = 1; export const publicValue = first;";
prepare(leakingSource, { [left]: ["#0"], [entry]: ["#1", "#2"] }, [left]);
const leakage = path.join(root, "leakage");
const structuralOutput = path.join(root, "structural-leakage");
const legacy = spawnSync(
  process.env.LOWERSPLIT,
  ["--diagnostic", writeDeployment(input, placement, structuralOutput), structuralOutput],
  {
    encoding: "utf8",
  }
);
assert.equal(legacy.status, 0, legacy.stdout + legacy.stderr);
assert.match(legacy.stdout, /uncertified preview/);
assert.notDeepEqual(
  await observe(pathToFileURL(path.join(structuralOutput, entry))),
  await observe("data:text/javascript," + encodeURIComponent(leakingSource))
);
assert.match(check(leakage, "rejected").reason, /entry exports resolve to different linked cells/);
assert(!fs.existsSync(leakage));

const reordered = path.join(root, "reordered");
prepare(source, placement3, [right, left]);
const movedVerdict = check(reordered, "certified");
assert.equal(movedVerdict.strategy, "structural");
assert.equal(movedVerdict.guarantee, "normal-equivalence");
assert.equal(movedVerdict.every_ending_matches, false);
assert.deepEqual(check(reordered, "certified", "--check-output"), movedVerdict);
assert.deepEqual(await observe(pathToFileURL(path.join(reordered, entry))), [["result", 1]]);
prepare("const sparse = [1, , 2];", { [entry]: ["#0"] }, []);
const unsupported = path.join(root, "unsupported");
const opaqueUnchanged = check(unsupported, "certified");
assert.equal(opaqueUnchanged.strategy, "structural");
assert.equal(opaqueUnchanged.every_ending_matches, true);
assert.deepEqual(check(unsupported, "certified", "--check-output"), opaqueUnchanged);
const sparseOutput = fs.readFileSync(path.join(unsupported, entry), "utf8");
check(unsupported, "rejected", "--force");
const mixed = spawnSync(process.env.LOWERSPLIT, [
  "--diagnostic",
  writeDeployment(input, placement, unsupported),
  unsupported,
  "--check-output",
]);
assert.notEqual(mixed.status, 0);
assert.match(mixed.stderr.toString(), /cannot be combined/);
assert.equal(fs.readFileSync(path.join(unsupported, entry), "utf8"), sparseOutput);

// Browser module scripts can use .js for both the entry and its imports.
const browserTree = path.join(root, "browser-tree");
const pageProfile = path.join(root, "browser.json");
fs.writeFileSync(pageProfile, JSON.stringify({ profile: "browser-page" }));
prepare(
  "const value = 1; export const result = value;",
  { "entry.js": ["#1"], "value.js": ["#0"] },
  ["value.js"],
  "entry.js"
);
assert.match(check(browserTree, "rejected").reason, /safe relative \.mjs path/);
const browserArgs = ["--host-contract", pageProfile];
check(browserTree, "certified", ...browserArgs);
check(browserTree, "certified", ...browserArgs, "--check-output");
const valueFile = path.join(browserTree, "value.js");
const valueSource = fs.readFileSync(valueFile, "utf8");
assert(valueSource.includes("value = 1"), valueSource);
fs.writeFileSync(valueFile, valueSource.replace("value = 1", "value = 2"));
assert.match(check(browserTree, "rejected", ...browserArgs, "--check-output").reason, /value\.js differs/);

// A CommonJS extension has no portable ESM loading contract.
prepare("const local = 1;", { [entry]: [], "other.cjs": ["#0"] }, ["other.cjs"]);
assert.match(check(unsupported, "rejected").reason, /\.mjs/);
assert.match(check(unsupported, "rejected", ...browserArgs).reason, /safe relative/);
assert.equal(fs.readFileSync(path.join(unsupported, entry), "utf8"), sparseOutput);
const escaping = "../escaped.mjs";
prepare("const local = 1;", { [entry]: [], [escaping]: ["#0"] }, [escaping]);
check(unsupported, "rejected");
assert(!fs.existsSync(path.join(root, "escaped.mjs")));

// Choose the owner independently of each initializer's read. This produces both directions
// of cross-module dependency, including cycles where the glue exposes a private entry binding.
// Compare observable results in Node without preclassifying which placements Lean should accept.
let certifiedCount = 0;
let exposedByStructuralMode = 0;
for (let seed = 0; seed < 8; seed++) {
  const names = Array.from({ length: 3 + (seed % 3) }, (_, i) => `local${seed}_${i}`);
  const owners = { [entry]: [], [left]: [] };
  const declarations = names.map((name, i) => {
    const owner = i === names.length - 1 || (i + seed) % 3 === 1 ? entry : left;
    owners[owner].push("#" + i);
    const ref = seed % 2 ? Math.max(i - 1, 0) : (i + 1) % names.length;
    const operand = i === 0 && seed % 2 ? "-2" : names[ref];
    return (
      (i === names.length - 1 ? "export " : "") + "var " + name + " = " + "!".repeat((i + seed) % 3) + operand + ";"
    );
  });
  const code = declarations.join("\n");
  const expected = await observe("data:text/javascript," + encodeURIComponent(code));
  prepare(code, owners, [left]);
  const structural = path.join(root, "structural-partition-" + seed);
  const baseline = spawnSync(
    process.env.LOWERSPLIT,
    ["--diagnostic", writeDeployment(input, placement, structural), structural],
    {
      encoding: "utf8",
    }
  );
  if (baseline.error) throw baseline.error;
  assert([0, 1].includes(baseline.status), baseline.stderr);
  const output = path.join(root, "partition-" + seed);
  const verdict = request(output);
  if (verdict.status === "certified") {
    certifiedCount++;
    assert.deepEqual(await observe(pathToFileURL(path.join(output, entry))), expected, code);
  }
  if (baseline.status === 0) {
    const observed = await observe(pathToFileURL(path.join(structural, entry)));
    if (Array.isArray(observed) && observed.length > expected.length) {
      assert.equal(verdict.status, "rejected", code);
      exposedByStructuralMode++;
    }
  }
}
assert(certifiedCount > 0, "generated placements must include a certified control");
assert(exposedByStructuralMode > 0, "generated placements must exercise private entry bindings exposed by glue");

const hashbangSource = "#!/usr/bin/env node\nexport const marker = 1;";
const hashbangFile = path.join(root, "hashbang.mjs");
fs.writeFileSync(hashbangFile, hashbangSource);
prepare(hashbangSource, { [entry]: ["#0"] }, []);
const hashbangOut = path.join(root, "hashbang-output");
assert.equal(requestFile(hashbangFile, hashbangOut).status, "certified");
const hashbangEntry = path.join(hashbangOut, entry);
const hashbangOutput = fs.readFileSync(hashbangEntry, "utf8");
assert(hashbangOutput.startsWith("#!/usr/bin/env node\n"), hashbangOutput);
fs.writeFileSync(hashbangEntry, hashbangOutput.replace("#!/usr/bin/env node", "#!/usr/bin/env node --no-warnings"));
check(hashbangOut, "rejected", "--check-output");

// JSON stores both of these Literal.value fields as the string "Infinity".
const infiniteString = 'export const value = "Infinity";';
prepare(infiniteString, { [entry]: ["#0"] }, []);
const literalOut = path.join(root, "literal-type");
check(literalOut, "certified");
fs.writeFileSync(path.join(literalOut, entry), "export const value = 1e999;");
check(literalOut, "rejected", "--check-output");
