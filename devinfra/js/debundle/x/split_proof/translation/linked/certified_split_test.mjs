import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import { dump } from "../parser.mjs";
import { caseShard, moduleEvaluationOrder, observe as observeFresh } from "./harness.mjs";

const root = process.env.TEST_TMPDIR;
const runCase = caseShard();
const input = path.join(root, "input.json");
const placement = path.join(root, "placement.json");
function prepare(source, modules, order, entry = "entry.mjs") {
  const ast = dump(source);
  fs.writeFileSync(input, JSON.stringify(ast));
  fs.writeFileSync(placement, JSON.stringify({ entry, order, sequential: true, modules }));
  return ast;
}
function requestFile(file, out, requested, ...args) {
  const checkOutput = args.includes("--check-output");
  const hostAt = args.indexOf("--host-contract");
  const result = split({
    ...(file.endsWith(".json") ? { ast: file } : { source: file }),
    spec: placement,
    out,
    require: requested,
    checkOutput,
    allowExistingOut: fs.existsSync(out),
    ...(hostAt >= 0 ? { hostContract: args[hostAt + 1] } : {}),
  });
  return result.verdict;
}
function request(out, requested, ...args) {
  return requestFile(input, out, requested, ...args);
}
function check(out, requested, status, ...args) {
  const verdict = request(out, requested, ...args);
  assert.equal(verdict.status, status, JSON.stringify(verdict));
  return verdict;
}
async function observe(url) {
  try {
    return Object.entries(await import(url));
  } catch (e) {
    return e.name;
  }
}
const all = "all-outcomes";
const normal = "normal-completion";
const entry = "entry.mjs";
const left = "left.mjs";
const right = "right.mjs";
const source = "const a = 1; const b = 2; export const result = a;";
const placement3 = { [entry]: ["#2"], [left]: ["#0"], [right]: ["#1"] };
if (runCase("ordered-disk-mutations")) {
  const out = path.join(root, "mutations");
  const ast = prepare(source, placement3, [left, right]);
  const orderedVerdict = check(out, all, "certified");
  assert.equal(orderedVerdict.scope, "named-declarations");
  assert.equal(orderedVerdict.guarantee, all);
  // The ordinary JavaScript entry point and the annotated dump share the same checker.
  const sourceFile = path.join(root, "source.mjs");
  const rawOutput = path.join(root, "from-source");
  fs.writeFileSync(sourceFile, source);
  assert.equal(requestFile(sourceFile, rawOutput, undefined).status, "certified");
  assert.equal(requestFile(sourceFile, out, all, "--check-output").status, "certified");
  assert.deepEqual(await observe(pathToFileURL(path.join(rawOutput, entry))), await observe(pathToFileURL(sourceFile)));
  const entryFile = path.join(out, entry);
  const emitted = fs.readFileSync(entryFile, "utf8");
  const imports = emitted.split("\n").filter((line) => line.startsWith("import "));
  assert.equal(imports.length, 2, emitted);
  for (const changed of [
    emitted.replace("result = a", "result = 0"),
    emitted.replace(/export \{[^}]*\}/, "export {}"),
    emitted.replace("result =", "different ="),
    emitted.replace("./left.mjs", "./right.mjs"),
    emitted.replace(/\ba\b/, "a as shadow"),
    emitted.replace(imports.join("\n"), imports.toReversed().join("\n")),
    emitted + "\nconst extra = 1;\n",
    emitted + "\nexport {",
  ]) {
    assert.notEqual(changed, emitted);
    fs.writeFileSync(entryFile, changed);
    check(out, all, "rejected", "--check-output");
  }
  fs.writeFileSync(entryFile, "// harmless formatting change\n" + emitted);
  check(out, all, "certified", "--check-output");
  const extra = path.join(out, "extra.mjs");
  fs.writeFileSync(extra, "");
  check(out, all, "rejected", "--check-output");
  fs.unlinkSync(extra);
  fs.renameSync(path.join(out, left), extra);
  check(out, all, "rejected", "--check-output");
  fs.renameSync(extra, path.join(out, left));

  // Publication must leave a previously populated destination intact on failure.
  const sentinel = path.join(out, "unrelated");
  fs.writeFileSync(sentinel, "keep");
  check(out, all, "rejected");
  assert.equal(fs.readFileSync(sentinel, "utf8"), "keep");
  fs.unlinkSync(sentinel);
  ast.sourceText = source.replace("= 1", "= 3");
  fs.writeFileSync(input, JSON.stringify(ast));
  assert.match(check(out, all, "rejected", "--check-output").reason, /fresh parse/);
}

// Compose declarations, unary expressions, dependency chains and partition boundaries.
// Every seed checks Node's actual namespace (or abrupt outcome), then changes a generated
// initializer without changing the layout. The model must catch each syntactically valid mutant.
for (const [first, end] of [
  [0, 8],
  [8, 16],
]) {
  if (!runCase(`declaration-seeds-${first}-${end - 1}`)) continue;
  for (let seed = first; seed < end; seed++) {
    const length = 3 + (seed % 5);
    const cut = seed % 4 === 0 ? 2 : 1 + (seed % (length - 1));
    const name = (i) => "v" + seed + "_" + i;
    const decls = [];
    for (let i = 0; i < length; i++) {
      const earlier = (seed + i * 3) % Math.max(i, 1);
      const operand = i === 0 ? ["false", "0", "null", '"abc"'][seed % 4] : name(earlier);
      const kind = (seed + i) % 2 ? "const" : "let";
      decls.push(
        (i === length - 1 ? "export " : "") + kind + " " + name(i) + " = " + "!".repeat((seed + i) % 4) + operand + ";"
      );
    }
    const code = decls.join("\n");
    const selectors = decls.map((_, i) => "#" + i);
    const generated = path.join(root, "seed-" + seed);
    prepare(code, { [left]: selectors.slice(0, cut), [entry]: selectors.slice(cut) }, [left]);
    assert.equal(check(generated, all, "certified").guarantee, all, code);
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
    check(generated, all, "rejected", "--check-output");
  }
}

if (runCase("grouped-declarations-and-placement")) {
  // A printer can keep consecutive module-scope declarators in one statement.
  prepare("const a = 1, b = a; export const result = b;", { [entry]: ["#2"], [left]: ["#0", "#1"] }, [left]);
  const groupedOut = path.join(root, "grouped-declarators");
  check(groupedOut, normal, "certified");
  const groupedFile = path.join(groupedOut, left);
  const groupedCode = fs.readFileSync(groupedFile, "utf8").replace("const a = 1;\nconst b = a;", "const a = 1, b = a;");
  assert.match(groupedCode, /const a = 1, b = a;/);
  fs.writeFileSync(groupedFile, groupedCode);
  check(groupedOut, normal, "certified", "--check-output");
  for (const changed of ["const b = a, a = 1;", "let a = 1, b = a;", "const a = 2, b = a;"]) {
    fs.writeFileSync(groupedFile, groupedCode.replace("const a = 1, b = a;", changed));
    check(groupedOut, normal, "rejected", "--check-output");
  }
  // This cyclic placement needs the entry to expose a previously private binding to its peer.
  // Even an order-preserving CoreJS plan with all nine structural checks cannot certify that change.
  const leakingSource = "const first = later; var later = 1; export const publicValue = first;";
  prepare(leakingSource, { [left]: ["#0"], [entry]: ["#1", "#2"] }, [left]);
  const leakage = path.join(root, "leakage");
  const leakageRefusal = check(leakage, undefined, "rejected");
  assert.equal(leakageRefusal.reason_code, "no_constructed_rule");
  assert(!fs.existsSync(leakage));

  // A forced right-before-left schedule for b = a observes the temporal dead zone.
  // The cycle forces right to evaluate before left despite right importing a.
  const forced = path.join(root, "forced-dependent-swap");
  fs.mkdirSync(forced);
  fs.writeFileSync(path.join(forced, right), 'import { a } from "./left.mjs"; export const b = a;');
  fs.writeFileSync(path.join(forced, left), 'import "./right.mjs"; export const a = 1;');
  fs.writeFileSync(path.join(forced, entry), 'import "./left.mjs"; import "./right.mjs"; export const result = 1;');
  assert.deepEqual(observeFresh(path.join(forced, entry), "result"), { error: "ReferenceError" });

  const rawSource = path.join(root, "literal-swap-source.mjs");
  const dependent = source.replace("b = 2", "b = a");
  fs.writeFileSync(rawSource, dependent);
  prepare(dependent, placement3, [right, left]);
  assert.deepEqual(observeFresh(rawSource, "result"), { result: 1 });
  for (const guarantee of [all, normal]) {
    const fresh = path.join(root, `dependent-fresh-${guarantee}`);
    assert.equal(requestFile(rawSource, fresh, guarantee).status, "rejected");
    assert(!fs.existsSync(fresh));
    const existing = path.join(root, `dependent-existing-${guarantee}`);
    fs.mkdirSync(existing);
    const sentinel = path.join(existing, "sentinel.txt");
    fs.writeFileSync(sentinel, "preserve existing bytes\n");
    assert.equal(requestFile(rawSource, existing, guarantee).status, "rejected");
    assert.deepEqual(fs.readdirSync(existing), ["sentinel.txt"]);
    assert.equal(fs.readFileSync(sentinel, "utf8"), "preserve existing bytes\n");
  }

  // Original request 8ccb05394d872465: retain its source, ordinals and owners.
  fs.writeFileSync(rawSource, source);
  prepare(source, placement3, [right, left]);
  const reordered = path.join(root, "reordered");
  const movedVerdict = requestFile(rawSource, reordered, all);
  assert.equal(movedVerdict.status, "certified", JSON.stringify(movedVerdict));
  assert.equal(movedVerdict.strategy, "compiled-constructor");
  assert.equal(movedVerdict.guarantee, all);
  assert.deepEqual(requestFile(rawSource, reordered, all, "--check-output"), movedVerdict);
  assert.equal(requestFile(rawSource, reordered, normal, "--check-output").guarantee, all);
  assert.deepEqual(fs.readdirSync(reordered).sort(), [entry, left, right].sort());
  assert.match(fs.readFileSync(path.join(reordered, left), "utf8"), /\bconst a = 1\s*;/);
  assert.match(fs.readFileSync(path.join(reordered, right), "utf8"), /\bconst b = 2\s*;/);
  assert.match(fs.readFileSync(path.join(reordered, entry), "utf8"), /\bconst result = a\s*;/);
  assert.deepEqual(moduleEvaluationOrder(reordered, entry), [right, left, entry]);
  assert.deepEqual(await observe(pathToFileURL(rawSource)), [["result", 1]]);
  assert.deepEqual(await observe(pathToFileURL(path.join(reordered, entry))), [["result", 1]]);

  // a and result have equal values, but exporting a changes the public live cell.
  const entryFile = path.join(reordered, entry);
  const emitted = fs.readFileSync(entryFile, "utf8");
  const wrongCell = emitted.replace(/export \{[^}]*\}/, "export { a as result }");
  assert.notEqual(wrongCell, emitted);
  fs.writeFileSync(entryFile, wrongCell);
  assert.deepEqual(observeFresh(entryFile, "result"), { result: 1 });
  assert.equal(requestFile(rawSource, reordered, all, "--check-output").status, "rejected");
  assert.equal(fs.readFileSync(entryFile, "utf8"), wrongCell);
}

if (runCase("hashbang-and-literal-type")) {
  const hashbangSource = "#!/usr/bin/env node\nexport const marker = 1;";
  const hashbangFile = path.join(root, "hashbang.mjs");
  fs.writeFileSync(hashbangFile, hashbangSource);
  prepare(hashbangSource, { [entry]: ["#0"] }, []);
  const hashbangOut = path.join(root, "hashbang-output");
  assert.equal(requestFile(hashbangFile, hashbangOut, all).status, "certified");
  const hashbangEntry = path.join(hashbangOut, entry);
  const hashbangOutput = fs.readFileSync(hashbangEntry, "utf8");
  assert(hashbangOutput.startsWith("#!/usr/bin/env node\n"), hashbangOutput);
  fs.writeFileSync(hashbangEntry, hashbangOutput.replace("#!/usr/bin/env node", "#!/usr/bin/env node --no-warnings"));
  check(hashbangOut, all, "rejected", "--check-output");

  // JSON stores both of these Literal.value fields as the string "Infinity".
  const infiniteString = 'export const value = "Infinity";';
  prepare(infiniteString, { [entry]: ["#0"] }, []);
  const literalOut = path.join(root, "literal-type");
  check(literalOut, all, "certified");
  fs.writeFileSync(path.join(literalOut, entry), "export const value = 1e999;");
  check(literalOut, all, "rejected", "--check-output");
}
