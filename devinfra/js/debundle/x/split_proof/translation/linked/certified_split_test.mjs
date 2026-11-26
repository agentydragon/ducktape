import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import { dump } from "../parser.mjs";
import { caseShard } from "./harness.mjs";

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
  assert.equal(orderedVerdict.scope, "closed-declarations");
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
    // Every fourth seed reads a later lexical binding before initialization.
    // The CoreJS theorem preserves the ReferenceError, but moving its source
    // unit changes the JavaScript rejection stack's module URL.
    if (seed % 4 === 0) {
      assert.equal(await observe("data:text/javascript," + encodeURIComponent(code)), "ReferenceError", code);
      const refused = check(generated, all, "rejected");
      assert.equal(refused.reason_code, "strict_observation_unsupported", code);
      assert(!fs.existsSync(generated), "a TDZ stack refusal must not emit modules");
      continue;
    }
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
  assert.match(leakageRefusal.reason, /entry public exports/);
  assert(!fs.existsSync(leakage));

  const reordered = path.join(root, "reordered");
  prepare(source, placement3, [right, left]);
  assert.match(check(reordered, all, "rejected").reason, /normal-completion/);
  assert(!fs.existsSync(reordered));
  const movedVerdict = check(reordered, normal, "certified");
  assert.equal(movedVerdict.scope, "closed-declarations");
  assert.equal(movedVerdict.guarantee, normal);
  assert.deepEqual(check(reordered, normal, "certified", "--check-output"), movedVerdict);
  assert.deepEqual(await observe(pathToFileURL(path.join(reordered, entry))), [["result", 1]]);
}

if (runCase("browser-and-path-contracts")) {
  prepare("const sparse = [1, , (() => 2)()];", { [entry]: ["#0"] }, []);
  const unsupported = path.join(root, "unsupported");
  assert.match(check(unsupported, normal, "rejected").reason, /unsupported array hole/);
  check(unsupported, undefined, "rejected");

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
  assert.match(check(browserTree, all, "rejected").reason, /safe relative \.mjs path/);
  const browserArgs = ["--host-contract", pageProfile];
  check(browserTree, all, "certified", ...browserArgs);
  check(browserTree, all, "certified", ...browserArgs, "--check-output");
  const valueFile = path.join(browserTree, "value.js");
  const valueSource = fs.readFileSync(valueFile, "utf8");
  assert(valueSource.includes("value = 1"), valueSource);
  fs.writeFileSync(valueFile, valueSource.replace("value = 1", "value = 2"));
  assert.match(check(browserTree, all, "rejected", ...browserArgs, "--check-output").reason, /value\.js differs/);

  // A CommonJS extension has no portable ESM loading contract.
  prepare("const local = 1;", { [entry]: [], "other.cjs": ["#0"] }, ["other.cjs"]);
  assert.match(check(unsupported, all, "rejected").reason, /\.mjs/);
  assert.match(check(unsupported, all, "rejected", ...browserArgs).reason, /safe relative/);
  assert(!fs.existsSync(unsupported));
  const escaping = "../escaped.mjs";
  prepare("const local = 1;", { [entry]: [], [escaping]: ["#0"] }, [escaping]);
  check(unsupported, all, "rejected");
  assert(!fs.existsSync(path.join(root, "escaped.mjs")));
}

if (runCase("generated-partitions-0-7")) {
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
    prepare(code, owners, [left]);
    const output = path.join(root, "partition-" + seed);
    const expected = await observe("data:text/javascript," + encodeURIComponent(code));
    const result = split({ ast: input, spec: placement, out: output });
    if (result.status === "certified") {
      certifiedCount++;
      assert.deepEqual(await observe(pathToFileURL(path.join(output, entry))), expected, code);
    } else {
      assert.equal(result.files.length, 0, result.log);
      if (/entry public exports/.test(result.reason ?? "")) exposedByStructuralMode++;
    }
  }
  assert(certifiedCount > 0, "generated placements must include a certified control");
  assert(
    exposedByStructuralMode > 0,
    "generated placements must count refusals caused by private entry bindings exposed by glue"
  );
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
