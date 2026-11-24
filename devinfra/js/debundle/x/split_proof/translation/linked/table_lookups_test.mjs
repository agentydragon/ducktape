import assert from "node:assert/strict";
import { cpSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { generate } from "astring";
import {
  array,
  exportNamed,
  expression,
  forcePreview,
  id,
  literal,
  lower,
  member,
  moduleEvaluationOrder,
  prepare,
  program,
  property,
  root,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const dataOwner = "data.mjs";
const firstOwner = "first.mjs";
const secondOwner = "second.mjs";
const order = [dataOwner, secondOwner, firstOwner];
const exportNames = ["aliases", "first", "second", "summary"];
const lookup = (key, computed = false) => member(id("table"), id(key), computed);
const set = (left, value) => expression({ type: "AssignmentExpression", operator: "=", left, right: literal(value) });

function fixture(name, width, rows, mutation) {
  const keys = Array.from({ length: width }, (_, i) => `field${i}`);
  const value = (key) => `value-${key}`;
  const last = keys.at(-1);
  const alternate = keys.at(-2);
  const rowKeys = (side, index) => [keys.at(-3 - side - (index % 4)), keys[(index + side) % 4]];
  const makeRows = (side) =>
    array(
      Array.from({ length: rows }, (_, index) => {
        const [tail, head] = rowKeys(side, index);
        return array([
          literal(side),
          literal(index),
          mutation === "key" ? lookup("key", true) : lookup(last),
          array([lookup(tail), array([lookup(head), id("undefined")]), array([])]),
        ]);
      })
    );

  const units = [
    [
      dataOwner,
      variable("const", "table", {
        type: "ObjectExpression",
        properties: keys.map((key) => property(key, literal(value(key)))),
      }),
    ],
    [dataOwner, variable("const", "aliases", array([lookup(keys[0]), lookup(last)]))],
  ];
  if (mutation === "key") units.push([dataOwner, variable("let", "key", literal(last))]);
  // The split evaluates summary and second before first. Both arrays depend on
  // the same provider and capture its field values during initialization.
  units.push(
    [firstOwner, variable("const", "first", makeRows(0))],
    [dataOwner, variable("const", "summary", literal("ready"))],
    [secondOwner, variable("const", "second", makeRows(1))]
  );
  if (mutation === "field") units.push([dataOwner, set(lookup(last), "updated")]);
  if (mutation === "key") units.push([dataOwner, set(id("key"), alternate)]);
  units.push([entry, exportNamed(exportNames.map((name) => [name, name]))]);

  const modules = Object.fromEntries([...order, entry].map((owner) => [owner, []]));
  for (const [index, [owner]] of units.entries()) modules[owner].push(`#${index}`);
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source: program(units.map(([, node]) => node)),
    provider: "",
    spec: { entry, modules, order, sequential: true },
  });

  function expectedRows(side, lookedUp) {
    return Array.from({ length: rows }, (_, index) => {
      const [tail, head] = rowKeys(side, index);
      return [side, index, lookedUp, [value(tail), [value(head), undefined], []]];
    });
  }

  function expected(lookedUp = value(last)) {
    return {
      aliases: [value(keys[0]), value(last)],
      first: expectedRows(0, lookedUp),
      second: expectedRows(1, lookedUp),
      summary: "ready",
    };
  }

  return { dir, files, expected, last, alternate, alternateValue: value(alternate) };
}

async function expectExports(file, expected) {
  const result = await import(pathToFileURL(file).href);
  assert.deepEqual(Object.keys(result), exportNames);
  for (const name of exportNames) assert.deepEqual(result[name], expected[name], `${file}: ${name}`);
}

{
  const test = fixture("reversed-table", 128, 4);
  const out = path.join(test.dir, "out");
  const { sourceFile, specFile } = test.files;
  const { verdict, log } = lower(sourceFile, specFile, out);
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
  assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);
  await expectExports(sourceFile, test.expected());
  await expectExports(path.join(out, entry), test.expected());

  for (const alteration of ["row-order", "lookup-key"]) {
    const tampered = path.join(test.dir, alteration);
    cpSync(out, tampered, { recursive: true });
    const changedFile = path.join(tampered, firstOwner);
    const ast = parse(readFileSync(changedFile, "utf8"), { ecmaVersion: "latest", sourceType: "module" });
    const array = ast.body.find((node) => node.type === "VariableDeclaration").declarations[0].init;
    const changed = test.expected();
    if (alteration === "row-order") {
      array.elements.reverse();
      changed.first.reverse();
    } else {
      const member = array.elements[0].elements[2];
      assert.equal(member.property.name, test.last);
      member.property.name = test.alternate;
      changed.first[0][2] = test.alternateValue;
    }
    writeFileSync(changedFile, generate(ast));
    const disk = lower(sourceFile, specFile, tampered, ["--check-output"]);
    assert.equal(disk.verdict.status, "rejected", disk.log);
    assert.match(disk.verdict.reason, /output module first\.mjs differs from its generated ESTree/, disk.log);
    await expectExports(path.join(tampered, entry), changed);
  }
}

// The same reversal moves an own-property write or a key-binding write ahead
// of the reads. Both executions finish, but the captured rows are different.
for (const mutation of ["field", "key"]) {
  const test = fixture(`reversed-table-${mutation}`, 128, 3, mutation);
  const out = path.join(test.dir, "out");
  const { verdict, log } = lower(test.files.sourceFile, test.files.specFile, out);
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /probe executions completed normally with different final states/, log);
  assert(!existsSync(out), log);

  const preview = path.join(test.dir, "preview");
  forcePreview(test.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [...order, entry]);
  await expectExports(test.files.sourceFile, test.expected());
  await expectExports(path.join(preview, entry), test.expected(mutation === "field" ? "updated" : test.alternateValue));
}
