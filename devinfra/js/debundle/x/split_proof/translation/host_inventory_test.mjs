// The host inventory on small sources: what counts as a global, how far a member path goes, and what the report prints.
import assert from "node:assert/strict";
import { environmentsOf, render, resolvedMembers, scanSource, standardMemberOwners } from "./host_inventory.mjs";

const paths = (inventory, name) => Object.keys(inventory.globals[name]?.paths ?? {});

// ---- a free identifier is a global; a name the file binds is not
{
  const inventory = scanSource(`
    import { imported } from "./other.js";
    const local = 1;
    function declared(parameter) { return parameter + local + imported + Math.max(1, 2); }
    class Shape extends Base {}
    try { declared(); } catch (caught) { caught; }
  `);
  assert.deepEqual(Object.keys(inventory.globals), ["Base", "Math"]);
  assert.deepEqual(paths(inventory, "Math"), ["max"]);
  assert.equal(inventory.globals.Math.paths.max.call, 1);
}

// ---- a classic script: its top-level `var` is bound, an assignment to an undeclared name is a write of a global
{
  const inventory = scanSource(`var top = 1; leaked = top; return;`);
  assert.deepEqual(inventory.files, { module: 0, script: 1 });
  assert.deepEqual(Object.keys(inventory.globals), ["leaked"]);
  assert.equal(inventory.globals.leaked.usage.write, 1);
}

// ---- member paths: prefixes count as reads, `call`/`apply`/`bind` belong to the function, three members at most
{
  const inventory = scanSource(`
    Object.prototype.hasOwnProperty.call(a, "x");
    Array.prototype.slice.apply(b);
    const bound = Promise.resolve.bind(Promise);
    one.two.three.four.five;
    JSON?.parse("1");
    new Intl.NumberFormat();
    Symbol["iterator"];
    Reflect[dynamic];
    Reflect["not a name"];
  `);
  assert.deepEqual(paths(inventory, "Object"), ["prototype", "prototype.hasOwnProperty"]);
  assert.equal(inventory.globals.Object.paths["prototype.hasOwnProperty"].call, 1);
  assert.equal(inventory.globals.Array.paths["prototype.slice"].call, 1);
  assert.deepEqual(inventory.globals.Promise.paths.resolve, { read: 1, call: 0, new: 0, write: 0, typeof: 0 });
  assert.deepEqual(paths(inventory, "one"), ["two", "two.three", "two.three.four"]);
  assert.equal(inventory.globals.JSON.paths.parse.call, 1);
  assert.equal(inventory.globals.Intl.paths.NumberFormat.new, 1);
  assert.deepEqual(paths(inventory, "Symbol"), ["iterator"]);
  assert.deepEqual(paths(inventory, "Reflect"), []);
}

// ---- the global object: `window.fetch` is the global `fetch`, and a destructuring pattern names members
{
  const inventory = scanSource(`
    window.fetch("u");
    globalThis.self.crypto.subtle;
    const { keys, entries: renamed, ...rest } = Object;
    ({ freeze } = Object);
    const { structuredClone, ...others } = globalThis;
    typeof document !== "undefined" && document.title;
    process.env.SETTING_OF_ONE_PROGRAM;
  `);
  assert.equal(inventory.globals.fetch.usage.call, 1);
  assert.deepEqual(paths(inventory, "crypto"), ["subtle"]);
  assert.deepEqual(paths(inventory, "Object"), ["entries", "freeze", "keys"]);
  assert.equal(inventory.globals.structuredClone.usage.read, 1);
  assert.deepEqual(paths(inventory, "globalThis"), []);
  assert.equal(inventory.globals.document.usage.typeof, 1);
  assert.deepEqual(paths(inventory, "process"), ["env"]);
}

// ---- writes: an assignment, an update and a `delete` of a global or of a member of one
{
  const inventory = scanSource(`
    Symbol.dispose ??= Symbol("dispose");
    Array.prototype.last = function () {};
    delete Object.prototype.toJSON;
    counter++;
    [first, second] = pair;
  `);
  assert.equal(inventory.globals.Symbol.paths.dispose.write, 1);
  assert.equal(inventory.globals.Array.paths["prototype.last"].write, 1);
  assert.equal(inventory.globals.Object.paths["prototype.toJSON"].write, 1);
  for (const name of ["counter", "first", "second"]) assert.equal(inventory.globals[name].usage.write, 1, name);
  assert.equal(inventory.globals.pair.usage.read, 1);
}

// ---- a member of a standard object on an unknown receiver is counted by name; a name no standard object has is not
{
  const inventory = scanSource(
    `function f(list) { list.map(g).filter(Boolean); list.size; list.ownMethod(); list.length = 0; }`
  );
  assert.deepEqual(Object.keys(inventory.memberNames), ["filter", "length", "map", "size"]);
  assert.equal(inventory.memberNames.map.call, 1);
  assert.equal(inventory.memberNames.size.read, 1);
  assert.equal(inventory.memberNames.length.write, 1);
}

// ---- host modules and `import.meta`
{
  const inventory = scanSource(`
    import fs from "node:fs";
    import "./side.js";
    export * from "bun:ffi";
    const lazy = () => import("path");
    require("lodash");
    new URL("./asset.png", import.meta.url);
    import.meta;
  `);
  assert.deepEqual(inventory.hostModules, { "bun:ffi": 1, "node:fs": 1, "node:path": 1 });
  assert.equal(inventory.otherSpecifiers, 2);
  assert.equal(inventory.dynamicImports, 1);
  assert.deepEqual(Object.keys(inventory.importMeta), ["(the object itself)", "url"]);
}

// ---- the lists: which environment has a name, and which members this Node's objects have
{
  assert.deepEqual(environmentsOf("Map"), ["ecmascript"]);
  assert.equal(environmentsOf("fetch")[0], "web");
  assert.deepEqual(environmentsOf("Deno"), ["otherHosts"]);
  assert.equal(environmentsOf("Buffer")[0], "node");
  assert.deepEqual(environmentsOf("definedByTheProgram"), []);
  assert.equal(resolvedMembers("Object", ["prototype", "hasOwnProperty"]), 2);
  assert.equal(resolvedMembers("Object", ["prototype", "definedByTheProgram"]), 1);
  assert.equal(resolvedMembers("definedByTheProgram", ["member"]), 0);
  const owners = standardMemberOwners();
  assert(owners.get("map").includes("Array.prototype") && owners.get("map").includes("%TypedArray%.prototype"));
  assert(owners.get("next").includes("%GeneratorPrototype%"));
}

// ---- the report prints only names a list has, unless asked for all
{
  const inventory = scanSource(`
    Object.keys(x); Object.definedByTheProgram; document.body.appendChild(y); console.log(1);
    definedByThePage.member; window.alsoDefinedByThePage = 1; Array.prototype.patched = 1; require("node:os");
  `);
  const report = render(inventory);
  for (const printed of ["`Object.keys`", "`console.log`", "`document`", "`node:os`"])
    assert(report.includes(printed), printed);
  for (const hidden of ["definedByTheProgram", "definedByThePage", "alsoDefinedByThePage", "appendChild", "patched"]) {
    assert(!report.includes(hidden), hidden);
  }
  assert(report.includes("4 globals that no list has, 5 member paths that do not resolve"), report);
  const full = render(inventory, { full: true });
  for (const printed of [
    "definedByThePage",
    "alsoDefinedByThePage",
    "`document.body.appendChild`",
    "`Array.prototype.patched`",
  ]) {
    assert(full.includes(printed), printed);
  }
}

console.log("host inventory: ok");
