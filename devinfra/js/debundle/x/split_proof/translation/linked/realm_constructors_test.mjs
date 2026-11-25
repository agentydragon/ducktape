import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  array,
  arrow,
  call,
  conditional,
  assertRejectedOrOrderKeptSlice,
  exportNamed,
  id,
  literal,
  lower,
  prepare,
  program,
  root,
  variable,
} from "./harness.mjs";

const profile = path.join(root, "profile.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const entry = "entry.mjs";
const owner = "factory.mjs";

const constructors = [
  { name: "Date", args: [literal(0)], observe: (v) => v.toISOString(), expected: "1970-01-01T00:00:00.000Z" },
  {
    name: "RegExp",
    args: [literal("^a$"), literal("i")],
    observe: (v) => [v.test("A"), v.test("aa")],
    expected: [true, false],
  },
  {
    name: "Set",
    args: [array([literal(1), literal(1), literal(2)])],
    observe: (v) => [...v],
    expected: [1, 2],
  },
];

for (const { name, args, observe, expected } of constructors) {
  for (const reached of [false, true]) {
    const dir = path.join(root, name + (reached ? "-eager" : "-deferred"));
    const construction = { type: "NewExpression", callee: id(name), arguments: args };
    const source = program([
      variable("const", "make", arrow([id("active")], conditional(id("active"), construction, literal(null)))),
      variable("const", "result", call(id("make"), [literal(reached)])),
      exportNamed([
        ["make", "make"],
        ["result", "result"],
      ]),
    ]);
    const files = prepare(dir, {
      source,
      provider: "",
      spec: {
        entry,
        modules: { [owner]: ["#0"], [entry]: ["#1", "#2"] },
        order: [owner],
        sequential: true,
      },
    });
    const out = path.join(dir, "out");
    const opts = ["--host-contract", profile];
    const { verdict, log } = lower(files.sourceFile, files.specFile, out, opts);
    if (
      reached &&
      !assertRejectedOrOrderKeptSlice({ verdict, log, out }, new RegExp(`ambient constructor ${name} is reached`))
    ) {
      continue;
    }
    assert.equal(verdict.status, "certified", log);
    assert.deepEqual(verdict.assumptions, []);
    assert.deepEqual(lower(files.sourceFile, files.specFile, out, [...opts, "--check-output"]).verdict, verdict);
    for (const file of [files.sourceFile, path.join(out, entry)]) {
      const mod = await import(pathToFileURL(file).href);
      if (reached) assert.deepEqual(observe(mod.result), expected);
      else assert.equal(mod.result, null);
      const value = mod.make(true);
      assert.deepEqual(observe(value), expected);
      assert.notEqual(mod.make(true), value);
    }
  }
}
