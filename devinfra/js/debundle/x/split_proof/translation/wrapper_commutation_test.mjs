import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { generate } from "astring";

const id = (name) => ({ type: "Identifier", name });
const literal = (value) => ({ type: "Literal", value });
const block = (body) => ({ type: "BlockStatement", body });
const returns = (argument) => ({ type: "ReturnStatement", argument });
const statement = (expression) => ({ type: "ExpressionStatement", expression });
const call = (callee, args = []) => ({ type: "CallExpression", callee, arguments: args });
const arrow = (params, body) => ({
  type: "ArrowFunctionExpression",
  params,
  body,
  expression: body.type !== "BlockStatement",
});
const variable = (name, init) => ({
  type: "VariableDeclaration",
  kind: "var",
  declarations: [{ type: "VariableDeclarator", id: id(name), init }],
});
const exported = (name, alias = name) => ({ type: "ExportSpecifier", local: id(name), exported: id(alias) });
const exportsOf = (specifiers) => ({ type: "ExportNamedDeclaration", declaration: null, specifiers, source: null });
const program = (body) => generate({ type: "Program", sourceType: "module", body });

function observe(file, count) {
  // A new Node process for every observation gives each version a fresh module cache,
  // including when an eager provider replaces a deferred one at the same URL.
  const run = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--observe", file, String(count)], {
    encoding: "utf8",
  });
  assert.equal(run.status, 0, run.error?.message ?? run.stderr);
  return JSON.parse(run.stdout);
}

async function observeModule(file, count) {
  const mod = await import(pathToFileURL(file));
  const creation = [...mod.events];
  const loads = [];
  for (let i = 0; i < count; i++) loads.push(mod["load" + i]());
  return { creation, loads, events: mod.events };
}

function run() {
  const root = process.env.TEST_TMPDIR;
  const provider = path.join(root, "provider.mjs");
  const consumer = path.join(root, "consumer.mjs");
  const reordered = path.join(root, "reordered.mjs");
  const pairsFile = path.join(root, "pairs.json");
  const variants = Array.from({ length: 8 }, (_, i) => {
    const factory = "create" + i,
      alias = "imported" + i,
      publicName = "factory" + i;
    const moved = "other" + i,
      result = "load" + i;
    const param = id("work" + i);
    const returned = arrow([], call(param));
    const parameters = i & 1 ? [param, id("cache")] : [param];
    const factoryDecl = (eager) =>
      variable(
        factory,
        arrow(
          parameters,
          eager ? block([statement(call(param)), returns(returned)]) : i & 2 ? block([returns(returned)]) : returned
        )
      );
    const type = { type: "UnaryExpression", operator: "typeof", prefix: true, argument: id(moved) };
    const effect = i & 2 ? { type: "BinaryExpression", operator: "===", left: type, right: literal("function") } : type;
    const push = call({ type: "MemberExpression", object: id("events"), property: id("push"), computed: false }, [
      effect,
    ]);
    const callback =
      i & 4 ? { type: "FunctionExpression", id: null, params: [], body: block([returns(push)]) } : arrow([], push);
    return {
      alias,
      factory,
      publicName,
      result,
      moved,
      factoryDecl,
      make: variable(result, call(id(alias), [callback])),
      other: variable(moved, arrow([], literal(i))),
    };
  });
  const imp = {
    type: "ImportDeclaration",
    source: literal("./provider.mjs"),
    specifiers: variants.map((v) => ({ type: "ImportSpecifier", local: id(v.alias), imported: id(v.publicName) })),
  };
  const source = (swapped) =>
    program([
      imp,
      variable("events", { type: "ArrayExpression", elements: [] }),
      ...variants.flatMap((v) => (swapped ? [v.other, v.make] : [v.make, v.other])),
      exportsOf([exported("events"), ...variants.map((v) => exported(v.result))]),
    ]);
  writeFileSync(consumer, source(false));
  writeFileSync(reordered, source(true));
  const pairs = variants.map((_, i) => [2 + 2 * i, 3 + 2 * i]);
  writeFileSync(pairsFile, JSON.stringify(pairs));
  for (const mode of ["deferred", "eager", "reassigned"]) {
    const eager = mode !== "deferred";
    writeFileSync(
      provider,
      program([
        ...variants.map((v) => v.factoryDecl(mode === "eager")),
        ...(mode === "reassigned"
          ? variants.map((v, i) =>
              variable("replacement" + i, {
                type: "AssignmentExpression",
                operator: "=",
                left: id(v.factory),
                right: v.factoryDecl(true).declarations[0].init,
              })
            )
          : []),
        exportsOf(variants.map((v) => exported(v.factory, v.publicName))),
      ])
    );
    const checked = spawnSync(process.env.LOWERSPLIT, ["--check-wrapper-swaps", consumer, provider, pairsFile], {
      encoding: "utf8",
    });
    assert.equal(checked.status, 0, checked.error?.message ?? checked.stdout + checked.stderr);
    const { checked: results } = JSON.parse(checked.stdout);
    assert.equal(results.length, variants.length);
    for (const [i, row] of results.entries()) {
      const v = variants[i];
      assert.deepEqual([row.earlier, row.later], pairs[i]);
      assert.equal(row.status, eager ? "unsupported" : "conditional-normal", JSON.stringify(row));
      if (!eager)
        assert.deepEqual(
          [row.import, row.export, row.providerBinding, row.resultBinding, row.movedBinding],
          [v.alias, v.publicName, v.factory, v.result, v.moved]
        );
      else assert.match(row.reason, mode === "eager" ? /factory block/ : /outside its declaration/);
    }
    const original = observe(consumer, variants.length);
    const candidate = observe(reordered, variants.length);
    if (eager) {
      assert.equal(original.creation.length, variants.length);
      assert.equal(candidate.creation.length, variants.length);
      assert(original.creation.every((v, i) => v !== candidate.creation[i]));
    } else {
      assert.deepEqual(original.creation, []);
      assert.equal(original.events.length, variants.length);
      assert.deepEqual(candidate, original);
    }
  }
}

if (process.argv[2] === "--observe") {
  console.log(JSON.stringify(await observeModule(process.argv[3], Number(process.argv[4]))));
} else {
  run();
}
