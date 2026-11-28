import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn, dump as dumpSource, eslintScope } from "./parser.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { coreFromParsed } from "../model/to_core.mjs";
import { bundleFromParsed } from "./frontend.mjs";
import { parseModule } from "./scope_metadata.mjs";
import { writeDeployment } from "./testing/deployment_manifest.mjs";
import { coreOf } from "./split_dump.mjs";

const dir = process.env.TEST_TMPDIR;
function file(name, contents) {
  const dest = path.join(dir, name);
  writeFileSync(dest, contents);
  return dest;
}

function reference(source) {
  const { ids, tokens, exports } = coreOf(source);
  return { entry: "main", modules: [{ name: "main", imports: [], ids, tokens, exports }] };
}

function run(programs, ast) {
  const input = file(ast ? "asts.jsonl" : "programs.jsonl", programs.map((p) => JSON.stringify(p)).join("\n") + "\n");
  return execFileSync(process.env.COREJS, [...(ast ? ["--ast"] : []), input], { encoding: "utf8" })
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
}

// Census counts concrete refusals even if structural mode could retain the unit.
const concreteCensusSource = `import { imported } from "./provider.mjs";
  const a = 1, b = imported;
  ${Array.from({ length: 25 }, (_, i) => `const f${i} = 2 ** 3;`).join("\n")}
  const date = new Date();
  export { a };`;
const concretePath = file(
  "concrete-census.json",
  JSON.stringify(dumpSource(concreteCensusSource, { includeOpaqueCaptures: true }))
);
const concreteBrowserContract = file("concrete-browser.json", JSON.stringify({ profile: "browser-page" }));
const concreteCensus = (args = []) =>
  JSON.parse(execFileSync(process.env.COREJS, ["--lowering-census", concretePath, ...args], { encoding: "utf8" }));
const concreteEmpty = concreteCensus();
const concreteBrowser = concreteCensus(["--host-contract", concreteBrowserContract]);
assert.equal(concreteEmpty.units, 28);
assert.equal(concreteBrowser.units, concreteEmpty.units);
assert.equal(concreteBrowser.lowering_refusals.length, 25);
assert.equal(concreteEmpty.lowering_refusals.length, 26);
assert.deepEqual(
  concreteBrowser.lowering_refusals.map(({ unit }) => unit),
  Array.from({ length: 25 }, (_, i) => i + 3)
);

async function nodeOutcome(url) {
  try {
    await import(url);
    return ["normal", ""];
  } catch (e) {
    return ["throw", e instanceof Error ? e.name : e == null ? String(e) : `${typeof e}:${e}`];
  }
}

// The move census lowers the annotated source units through the linked rules.
// The static field is allocation-only; the call is not. The exponentiation
// initializer falls back to opaque, while top-level await is refused even as opaque.
const censusSource = `
  import { value } from "external-package";
  function f() {}
  function deferred() { return 2 ** 3; }
  const a = 1, b = class { field = value; static field = 1; },
    c = class { static field = console.log("effect"); };
  const { x } = value;
  const unmodeled = 2 ** 3;
  await 1;
  export { a };
`;
const censusJs = file("census.mjs", censusSource);
const censusAst = JSON.parse(execFileSync(process.env.DUMP_AST, [censusJs, "--opaque-captures"], { encoding: "utf8" }));
assert.deepEqual(censusAst, dumpSource(censusSource, { includeOpaqueCaptures: true }));
assert.deepEqual(
  JSON.parse(execFileSync(process.env.DUMP_AST, [censusJs], { encoding: "utf8" })),
  dumpSource(censusSource)
);
const censusFile = file("census.json", JSON.stringify(censusAst));
const census = JSON.parse(
  execFileSync(process.env.COREJS, ["--moves-ast", censusFile, "--node-recording"], { encoding: "utf8" })
);
assert.equal(census.hostProfile, "node-recording");
assert.deepEqual(
  [census.units, census.inert, census.allocate, census.opaque, census.other, census.refused],
  [8, 2, 2, 1, 3, 1]
);
assert.deepEqual([census.may_move, census.may_move_denominator], [4, 8]);
assert.match(census.problems[0], /await/i);
const censusConcrete = JSON.parse(
  execFileSync(process.env.COREJS, ["--lowering-census", censusFile], { encoding: "utf8" })
);
const emptyMoves = JSON.parse(execFileSync(process.env.COREJS, ["--moves-ast", censusFile], { encoding: "utf8" }));
assert.deepEqual(emptyMoves.lowering_refusals, censusConcrete.lowering_refusals);
assert.equal(emptyMoves.unit_details.length, censusConcrete.units);

// An ambient host read must not become allocation-only under production
// profiles merely because recorded Node replay treats it as a host value.
const globalsFile = file("census-globals.json", JSON.stringify(dumpSource("const a = Object, b = Math;")));
function censusFor(args = []) {
  return JSON.parse(execFileSync(process.env.COREJS, ["--moves-ast", globalsFile, ...args], { encoding: "utf8" }));
}
function contract(name, value) {
  return file(`${name}.json`, JSON.stringify(value));
}
const emptyCensus = censusFor();
assert.deepEqual([emptyCensus.hostProfile, emptyCensus.allocate, emptyCensus.opaque], ["empty", 0, 2]);
const emptyPath = contract("empty", { profile: "empty" });
assert.deepEqual(censusFor(["--host-contract", emptyPath]), emptyCensus);
const ecmaPath = contract("ecma", { profile: "ecmascript" });
const ecmaCensus = censusFor(["--host-contract", ecmaPath]);
assert.deepEqual([ecmaCensus.hostProfile, ecmaCensus.allocate, ecmaCensus.opaque], ["ecmascript", 0, 1]);
const browserPath = contract("browser", { profile: "browser-page" });
const browserCensus = censusFor(["--host-contract", browserPath]);
assert.deepEqual([browserCensus.hostProfile, browserCensus.allocate, browserCensus.opaque], ["browser-page", 0, 0]);
const nodeCensus = censusFor(["--node-recording"]);
assert.deepEqual([nodeCensus.hostProfile, nodeCensus.allocate, nodeCensus.opaque], ["node-recording", 2, 0]);
const ecmaStable = contract("ecma-stable", {
  profile: "ecmascript",
  stableIntrinsicValues: ["Object.defineProperty@1"],
});
assert.deepEqual(censusFor(["--host-contract", ecmaStable]), ecmaCensus);
const browserStable = contract("browser-stable", { profile: "browser-page", stableIntrinsicValues: ["Math.PI@1"] });
assert.deepEqual(censusFor(["--host-contract", browserStable]), browserCensus);
// Both reports use the certified contract reader and preserve the same
// first refusals, including for supported stable-intrinsic contracts.
for (const [contractPath, expected] of [
  [ecmaStable, ecmaCensus],
  [browserStable, browserCensus],
]) {
  const concrete = JSON.parse(
    execFileSync(process.env.COREJS, ["--lowering-census", globalsFile, "--host-contract", contractPath], {
      encoding: "utf8",
    })
  );
  assert.equal(concrete.hostProfile, expected.hostProfile);
  assert.deepEqual(concrete.lowering_refusals, expected.lowering_refusals);
}
const invalidContract = contract("invalid-stable", {
  profile: "browser-page",
  stableIntrinsicValues: ["Object.defineProperty@1"],
});
const malformedContract = file("malformed-contract.json", "{");
for (const mode of ["--moves-ast", "--lowering-census"]) {
  for (const invalidContractPath of [invalidContract, malformedContract]) {
    const invalid = spawnSync(process.env.COREJS, [mode, globalsFile, "--host-contract", invalidContractPath]);
    assert.notEqual(invalid.status, 0, `${mode} must reject malformed or mismatched contracts`);
  }
}

const forbidden = contract("bad-profile", { profile: "node-recording" });
assert.notEqual(spawnSync(process.env.COREJS, ["--moves-ast", globalsFile, "--host-contract", forbidden]).status, 0);
assert.notEqual(
  spawnSync(process.env.COREJS, ["--moves-ast", globalsFile, "--node-recording", "--host-contract", browserPath])
    .status,
  0
);

// Each seed builds a fresh module and binding chain. Throwing the last value
// makes precision, shadowing and initialization observable in Node and CoreJS.
const literals = [
  "null",
  "true",
  "false",
  '"a\\nβ"',
  "undefined",
  "NaN",
  "Infinity",
  "5e-324",
  "1.7976931348623157e308",
  "1e400",
];
const sources = [
  "let a; var b; export { a, b };",
  "const before = later; let later = 2;",
  "var before = later; var later = 2; throw before;",
  'const undefined = "shadow"; throw undefined;',
  "const nested = !(!(!false)); throw nested;",
  "const nested = !(!(-2.5)); throw nested;",
  "const negative = -2.5; throw negative;",
  "const negativeZero = -0; throw negativeZero;",
  "export const early = !late, late = -2;",
];

for (let seed = 0; seed < 48; seed++) {
  const value = seed % 3 === 0 ? String((seed + 1) * 1.234567891234567e-13) : literals[seed % literals.length];
  const declarations = [`export const first = ${value};`, "; debugger;"];
  let last = "first";
  for (let i = 0; i <= seed % 5; i++) {
    const name = `v${i}`;
    declarations.push(`${["var", "let", "const"][(seed + i) % 3]} ${name} = ${last};`);
    last = name;
  }
  // A later initializer observes either the preceding binding, a hoisted var,
  // or an uninitialized lexical binding in this same declaration list.
  const group = Array.from({ length: 2 + (seed % 5) }, (_, i) => {
    const operand = i > 0 ? `part${i - 1}` : seed % 2 ? "part1" : value;
    const init = seed % 4 === 0 && i === 0 ? `-${seed}` : "!".repeat((seed + i) % 4) + operand;
    return `part${i} = ${init}`;
  });
  sources.push(`export ${["var", "let", "const"][seed % 3]} ${group.join(", ")}; throw part1;`);
  sources.push(`${declarations.join("\n")} export { ${last} as result }; throw ${last};`);
}
const actual = run(sources.map(dumpSource), true);
assert.deepEqual(actual, run(sources.map(reference), false));
for (const [i, source] of sources.entries()) {
  assert.deepEqual(
    [actual[i].end, actual[i].detail],
    await nodeOutcome(`data:text/javascript,${encodeURIComponent(source)}#${i}`),
    source
  );
}

// The linked rules lower unary operators on any operand; Node observes the thrown value.
const unarySources = ["-binding", "-true", "-Infinity", "+1", "~1"].map(
  (e) => `const binding = 1, value = ${e}; throw value;`
);
const unary = run(unarySources.map(dumpSource), true);
for (const [i, source] of unarySources.entries()) {
  assert.deepEqual(
    [unary[i].end, unary[i].detail],
    await nodeOutcome(`data:text/javascript,${encodeURIComponent(source)}#unary-${i}`),
    source
  );
}

// The standalone AST path also needs the realm's template registry. Two
// evaluations of the same site share one object; different sites stay distinct.
const taggedSource =
  "const tag = x => x; const site = () => tag`same`; throw site() === site() && site() !== tag`same`;";
const [tagged] = run([dumpSource(taggedSource)], true);
assert.deepEqual(
  [tagged.end, tagged.detail],
  await nodeOutcome(`data:text/javascript,${encodeURIComponent(taggedSource)}`)
);

// A named class expression has an immutable name binding throughout its body.
// That binding is distinct from an outer binding with the same spelling.
const namedClassSource = `
  let Named = "outer";
  const C = class Named {
    static self = Named;
    static fromArrow = () => Named;
    static rejectsWrite() {
      try { Named = 1; return false; }
      catch (error) { return true; }
    }
    self() { return Named; }
  };
  console.log(C.self === C, C.fromArrow() === C, C.rejectsWrite(), new C().self() === C, Named, C.name);
`;
const [namedClass] = run([dumpSource(namedClassSource)], true);
const namedClassNode = spawnSync(process.execPath, ["--input-type=module", "-e", namedClassSource], {
  encoding: "utf8",
});
assert.equal(namedClassNode.status, 0, namedClassNode.stderr);
assert.deepEqual(namedClass.out, namedClassNode.stdout.split("\n").slice(0, -1));
assert.deepEqual(
  [namedClass.end, namedClass.detail],
  await nodeOutcome(`data:text/javascript,${encodeURIComponent(namedClassSource)}`)
);

// `corejs --ast` lowers under the recorded Node host: `console.log` output, the model's `Object`
// functions, fresh symbols and undeclared names. Certification profiles still refuse these names.
const hostSources = [
  'console.log("a", 1 + 2, true);',
  "const proto = { x: 1 }; const made = Object.create(proto); console.log(made.x, Object.getPrototypeOf(made) === proto);",
  'const tag = Symbol("tag"); console.log(typeof tag, tag === Symbol("tag")); missing;',
  'try { missing = 1; } catch (e) { console.log("assignment"); } missing;',
];
// Node's ambient Symbol stays the intrinsic constructor throughout these runs.
const host = run(
  hostSources.map((source) => ({ ...dumpSource(source), symbolConstructor: true })),
  true
);
for (const [i, source] of hostSources.entries()) {
  const printed = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.deepEqual(host[i].out, printed.stdout.split("\n").slice(0, -1), source);
  assert.deepEqual(
    [host[i].end, host[i].detail],
    await nodeOutcome(`data:text/javascript,${encodeURIComponent(source)}#host-${i}`),
    source
  );
}

// Compare the same source through the production linked AST lowering and Node.
// Keys and getters record evaluation order, receiver identity, and abrupt completion.
const optionalComputedSources = [
  'const seed = null; export const result = seed?.["value"];',
  'const seed = null; export const result = seed?.["value"]();',
  `let trace = "";
   function key() { trace += "k"; return "value"; }
   const base = null;
   console.log(base?.[key()], trace);`,
  `let trace = "";
   function key() { trace += "k"; return "value"; }
   const base = { get value() { trace += this === base ? "g" : "x"; return 7; } };
   console.log(base?.[key()], trace);`,
  `let trace = "";
   function key() { trace += "k"; return "method"; }
   const base = { get method() { trace += this === base ? "g" : "x"; return function () { trace += this === base ? "m" : "x"; return 7; }; } };
   console.log(base?.[key()](), trace);`,
  `let trace = "";
   function key() { trace += "k"; return "method"; }
   const base = { get method() { trace += this === base ? "g" : "x"; return function () { trace += this === base ? "m" : "x"; return 7; }; } };
   console.log(base[key()]?.(), trace);`,
  `let trace = "";
   function key() { trace += "k"; return "method"; }
   function arg() { trace += "a"; return 1; }
   const base = null;
   console.log(base?.[key()](arg()), trace);`,
  `let trace = "";
   function key() { trace += "k"; return "method"; }
   function arg() { trace += "a"; return 1; }
   const base = { get method() { trace += "g"; return null; } };
   console.log(base[key()]?.(arg()), trace);`,
  `let trace = "";
   function key() { trace += "k"; throw 9; }
   try { ({})?.[key()](); } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
  `let trace = "";
   const base = { get method() { trace += this === base ? "g" : "x"; throw 9; } };
   try { base?.["method"](); } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
  `let trace = "";
   function key() { trace += "k"; throw 9; }
   try { null?.[key()]; (null?.[key()]).value; } catch (error) { trace += "t"; }
   console.log(trace);`,
  `let trace = "";
   function key() { trace += "k"; throw 9; }
   try { ({})?.[key()]; } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
  `let trace = "";
   const base = { get value() { trace += this === base ? "g" : "x"; throw 9; } };
   try { base?.["value"]; } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
];
const optionalComputed = run(
  optionalComputedSources.map((source) => dumpSource(source)),
  true
);
for (const [i, source] of optionalComputedSources.entries()) {
  const node = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.equal(node.status, 0, node.stderr);
  assert.deepEqual(
    [optionalComputed[i].end, optionalComputed[i].detail, optionalComputed[i].out],
    ["normal", "", node.stdout.split("\n").slice(0, -1)],
    source
  );
}
// Keep a chain's short circuit separate from an actual undefined read.
const continuationSources = [
  'const base = null; base?.["value"].next;',
  'const base = { value: undefined }; base?.["value"].next;',
  'const base = { value: null }; base?.["value"].next;',
  'const base = null; (base?.["value"]).next;',
];
const continuations = run(
  continuationSources.map((source) => dumpSource(source)),
  true
);
for (const [i, source] of continuationSources.entries()) {
  const [end, detail] = await nodeOutcome(`data:text/javascript,${encodeURIComponent(source)}#continuation-${i}`);
  assert.equal(continuations[i].end, end, source);
  if (end === "throw") {
    assert.equal(detail, "TypeError");
    assert.equal(continuations[i].detail, detail, source);
  }
}

const optionalTailSources = [
  `function debug(root) { return root?.process?.env.DEBUG; }
   console.log(debug(null), debug({ process: null }), debug({ process: { env: { DEBUG: 7 } } }));
   try { debug({ process: {} }); } catch { console.log("missing env throws"); }`,
  `let trace = "";
   function receiver() { trace += "r"; return null; }
   function key() { trace += "k"; throw 4; }
   console.log(receiver()?.[key()]?.[key()][key()], trace);`,
  `let trace = "";
   function receiver() { trace += "r"; return base; }
   function key(k) { trace += k; return k; }
   const env = { get DEBUG() { trace += this === env ? "d" : "x"; return 7; } };
   const process = { get env() { trace += this === process ? "e" : "x"; return env; } };
   const base = { get process() { trace += this === base ? "p" : "x"; return process; } };
   console.log(receiver()?.[key("process")]?.[key("env")][key("DEBUG")], trace);`,
  `let trace = "";
   const base = { get process() { trace += this === base ? "p" : "x"; return null; } };
   function key() { trace += "k"; throw 9; }
   console.log(base?.process?.[key()][key()], trace);`,
  `let trace = "";
   const base = { get env() { trace += this === base ? "e" : "x"; throw 9; } };
   function key() { trace += "k"; throw 8; }
   try { base?.env[key()]; } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
  `let trace = "";
   const base = { get env() { trace += this === base ? "e" : "x"; return undefined; } };
   function key() { trace += "k"; return "DEBUG"; }
   try { base?.env[key()]; } catch { trace += "t"; }
   console.log(trace);`,
  `let trace = "";
   function key() { trace += "k"; return "missing"; }
   try { false?.[key()].value; } catch { trace += "f"; }
   try { 0?.[key()].value; } catch { trace += "z"; }
   try { ""?.[key()].value; } catch { trace += "s"; }
   console.log(trace);`,
  `let trace = "";
   function receiver() { trace += "r"; return base; }
   function argument() { trace += "a"; return 1; }
   const base = { get method() { trace += this === base ? "g" : "x";
     return function () { trace += this === base ? "m" : "x"; throw 9; }; } };
   try { receiver()?.method(argument()); } catch (error) { trace += error === 9 ? "t" : "x"; }
   console.log(trace);`,
];
const optionalTail = run(
  optionalTailSources.map((source) => dumpSource(source)),
  true
);
for (const [i, source] of optionalTailSources.entries()) {
  const node = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.equal(node.status, 0, node.stderr);
  assert.deepEqual(
    [optionalTail[i].end, optionalTail[i].detail, optionalTail[i].out],
    ["normal", "", node.stdout.split("\n").slice(0, -1)],
    source
  );
}

// The empty-profile classifier still refuses the untouched ambient root.
const ambientTailPath = file(
  "optional-tail-ambient.json",
  JSON.stringify(dumpSource("globalThis?.process?.env.DEBUG;"))
);
const ambientTail = JSON.parse(
  execFileSync(process.env.COREJS, ["--lowering-census", ambientTailPath], { encoding: "utf8" })
);
assert.equal(ambientTail.lowering_refusals.length, 1);
assert.match(JSON.stringify(ambientTail.lowering_refusals[0]), /unsupported global globalThis/);

// The rule does not lower longer ordinary tails or a call whose target is a chain.
for (const unsupported of ["null?.x.y.z;", "(null?.x.y)();"]) {
  const [result] = run([dumpSource(unsupported)], true);
  assert.equal(result.end, "malformed", unsupported);
  assert.match(result.detail, /unsupported optional chain/, unsupported);
}

// Object rest excludes selected string keys and copies only enumerable own
// properties, including symbols, after the selected getters have run.
const objectRestSource = `
  let trace = "";
  const source = {
    __proto__: { inherited: 8 },
    get tab() { trace += "t"; return 3; },
    get extra() { trace += "e"; return 4; },
    [Symbol.iterator]: 7,
    ["__proto__"]: 9
  };
  Object.defineProperty(source, "hidden", { value: 5, enumerable: false });
  function pick({ tab: first, tab: second, ...rest }) {
    var first;
    return [first, second, rest];
  }
  const selected = pick(source), remaining = selected[2];
  console.log(trace, selected[0], selected[1], remaining.extra, "tab" in remaining,
    "hidden" in remaining, "inherited" in remaining, remaining[Symbol.iterator], remaining.__proto__,
    Object.getPrototypeOf(remaining) === Object.getPrototypeOf({}), pick.length);
  const copy = ({ ...rest }) => rest;
  const chars = ({ "0": first, ...rest }) => [first, rest[1], "0" in rest];
  const letters = chars("ab");
  console.log(letters[0], letters[1], letters[2], copy(42).missing);
  try { copy(null); } catch { console.log("null throws"); }
  try { copy(); } catch { console.log("undefined throws"); }
`;
const [objectRest] = run([{ ...dumpSource(objectRestSource), symbolConstructor: true, iteratorSymbol: true }], true);
const objectRestNode = spawnSync(process.execPath, ["--input-type=module", "-e", objectRestSource], {
  encoding: "utf8",
});
assert.equal(objectRestNode.status, 0, objectRestNode.stderr);
assert.deepEqual(
  [objectRest.end, objectRest.detail, objectRest.out],
  ["normal", "", objectRestNode.stdout.trimEnd().split("\n")]
);

// A destructured rest parameter iterates the newly packed array. Elisions still
// step the iterator, and an unfinished iterator is closed before the body.
const restArraySources = [
  `function pick(head, ...[first, , third]) {
     var first;
     first = first + 1;
     return [head, first, third, arguments.length];
   }
   const a = pick("head", 2, 3, 4, 5), b = pick("missing");
   const arrow = (...[first, second]) => first + second;
   console.log(pick.length, a[0], a[1], a[2], a[3], b[2], b[3], arrow.length, arrow(4, 3));`,
  `let trace = "";
   const proto = Object.getPrototypeOf([]);
   delete proto[Symbol.iterator];
   proto[Symbol.iterator] = function() {
     trace += "I";
     let index = 0;
     return {
       next() { trace += "N"; index += 1; return { value: index * 10, done: false }; },
       return() { trace += "R"; return {}; }
     };
   };
   const pick = (...[first, , third]) => first + third;
   const empty = (...[]) => "body";
   const answer = pick(1, 2, 3), body = empty();
   console.log(answer, body, trace);`,
];
const restArray = run(
  restArraySources.map((source) => ({ ...dumpSource(source), symbolConstructor: true, iteratorSymbol: true })),
  true
);
for (const [i, source] of restArraySources.entries()) {
  const printed = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
  assert.equal(printed.status, 0, printed.stderr);
  assert.deepEqual(
    [restArray[i].end, restArray[i].detail, restArray[i].out],
    ["normal", "", printed.stdout.trimEnd().split("\n")]
  );
}

// A callable in a parameter default comes before a callable in the body.
// Distinct scripted results make both dispatch and lexical numbering observable.
const scriptedSource =
  'const f = (callback = () => "param") => { const inside = () => "body"; return callback() + ":" + inside(); }; throw f();';
const scripted = dumpSource(scriptedSource, { includeOpaqueCaptures: true });
const [withoutScripts, withScripts] = run(
  [
    scripted,
    {
      ...scripted,
      scripts: [
        { code: 0, part: 2, steps: [["done", ["s", "first"]]] },
        { code: 0, part: 3, steps: [["done", ["s", "second"]]] },
      ],
    },
  ],
  true
);
assert.deepEqual(
  [withoutScripts.end, withoutScripts.detail],
  await nodeOutcome(`data:text/javascript,${encodeURIComponent(scriptedSource)}`)
);
assert.deepEqual([withScripts.end, withScripts.detail], ["throw", "string:first:second"]);

const guardedScripts = [
  { source: 'eval("value");', part: 0, reason: /direct .eval./ },
  { source: "await 1;", part: 0, reason: /top-level .await./ },
  { source: "function f() { return import.meta.url; }", part: 1, reason: /lexical context/ },
  { source: 'function f() { return import("./module.mjs"); }', part: 1, reason: /lexical context/ },
];
const guardedResults = run(
  guardedScripts.map(({ source, part }) => ({
    ...dumpSource(source, { includeOpaqueCaptures: true }),
    scripts: [{ code: 0, part, steps: [] }],
  })),
  true
);
for (const [i, { source, reason }] of guardedScripts.entries()) {
  assert.equal(guardedResults[i].end, "malformed", source);
  assert.match(guardedResults[i].detail, reason, source);
}

// Array elisions are absent own properties, while explicit undefined is present.
const sparseSource = `
  const sparse = [, -0, , 3];
  const empty = [,,];
  const explicit = [, undefined];
  const constants = [, NaN, Infinity];
  const copy = [...sparse];
  console.log(sparse.length, 0 in sparse, 1 in sparse, 2 in sparse, 3 in sparse, 1 / sparse[1]);
  console.log(empty.length, 0 in empty, 1 in empty, 0 in explicit, 1 in explicit, explicit[1]);
  console.log(constants.length, 0 in constants, constants[1], constants[2]);
  console.log(copy.length, 0 in copy, 2 in copy, copy[0], 1 / copy[1]);
  const parent = { 0: "inherited" };
  Object.setPrototypeOf(sparse, parent);
  console.log(0 in sparse, sparse[0], 2 in sparse);
`;
const [sparse] = run([dumpSource(sparseSource)], true);
const sparseNode = spawnSync(process.execPath, ["--input-type=module", "-e", sparseSource], { encoding: "utf8" });
assert.equal(sparseNode.status, 0, sparseNode.stderr);
assert.deepEqual([sparse.end, sparse.detail, sparse.out], ["normal", "", sparseNode.stdout.trimEnd().split("\n")]);

for (const source of [
  "const mark = () => 1; const value = [, mark()];",
  "const value = [, ...[1]];",
  "const undefined = 1; const value = [, undefined];",
]) {
  const [refusal] = run([dumpSource(source)], true);
  assert.equal(refusal.end, "malformed");
  assert.match(refusal.detail, /unsupported array hole/);
}

// Duplicate definitions update one property without moving its original position.
// Spread invokes the final getters in key order, exposing that the first key stays first.
// Accessors also merge opposite halves and replace data descriptors.
const duplicateSource = `
  let getterOrder = "";
  const record = {
    first: 0,
    get after() { getterOrder = getterOrder + "a"; return "after"; },
    get replaced() { return "old getter"; },
    replaced: "data",
    winner: 1,
    winner: 3,
    get first() { getterOrder = getterOrder + "f"; return "first getter"; },
    get pair() { return this.saved; },
    set pair(value) { this.saved = value; },
    saved: "initial"
  };
  const copy = { ...record };
  record.pair = "updated";
  console.log(getterOrder, copy.first, copy.after, copy.replaced, record.winner, record.pair, record.saved);
`;
const [duplicate] = run([dumpSource(duplicateSource)], true);
const duplicateNode = spawnSync(process.execPath, ["--input-type=module", "-e", duplicateSource], {
  encoding: "utf8",
});
assert.equal(duplicateNode.status, 0, duplicateNode.stderr);
assert.deepEqual(
  [duplicate.end, duplicate.detail, duplicate.out],
  ["normal", "", duplicateNode.stdout.split("\n").slice(0, -1)]
);
assert.deepEqual(duplicate.out, ["fa first getter after data 3 updated updated"]);

// Computed object and class methods/accessors evaluate keys in source order.
// ToPropertyKey preserves strings and well-known symbols and converts numbers,
// booleans, and null to strings. Object-to-primitive coercion is not modeled.
const computedSource = `
  let trace = "";
  const mark = (step, value) => { trace = trace + step; return value; };
  const record = {
    [mark("a", "data")]: mark("A", "value"),
    [mark("b", 1)]() { return this.label; },
    label: "object",
    get [mark("c", "slot")]() { return this.saved; },
    set [mark("d", "slot")](value) { this.saved = value; },
    [mark("e", true)]() { return this.label; },
    [mark("f", null)]() { return this.label; },
    [mark("g", Symbol.iterator)]() { return "object symbol"; }
  };
  record.slot = "saved";
  class Box {
    [mark("h", 2)]() { return this.value; }
    get [mark("i", "slot")]() { return this.value; }
    set [mark("j", "slot")](value) { this.value = value; }
    [mark("k", Symbol.iterator)]() { return this.value; }
    constructor() { this.value = "class"; }
  }
  const box = new Box();
  box.slot = "updated";
  console.log(
    trace,
    record.data, record[1](), record[1].name, record.true(), record.true.name,
    record.null(), record.null.name, record.slot,
    record[Symbol.iterator](), record[Symbol.iterator].name,
    box[2](), Box.prototype[2].name, box.slot,
    box[Symbol.iterator](), Box.prototype[Symbol.iterator].name
  );
`;
const [computed] = run([{ ...dumpSource(computedSource), symbolConstructor: true, iteratorSymbol: true }], true);
const computedNode = spawnSync(process.execPath, ["--input-type=module", "-e", computedSource], {
  encoding: "utf8",
});
assert.equal(computedNode.status, 0, computedNode.stderr);
assert.deepEqual(
  [computed.end, computed.detail, computed.out],
  ["normal", "", computedNode.stdout.split("\n").slice(0, -1)]
);
assert.deepEqual(computed.out, [
  "aAbcdefghijk value object 1 object true object null saved object symbol [Symbol.iterator] updated 2 updated updated [Symbol.iterator]",
]);

// Object-to-primitive conversion remains an explicit runtime unknown. It is
// not a lowering refusal, and Node can execute the same computed property.
const coercionSource = `
  const key = { toString() { return "dynamic"; } };
  const record = { [key]: "value" };
  console.log(record.dynamic);
`;
const [coercion] = run([dumpSource(coercionSource)], true);
assert.deepEqual([coercion.end, coercion.detail], ["unknown", "an object is converted to a primitive"]);
const coercionNode = spawnSync(process.execPath, ["--input-type=module", "-e", coercionSource], {
  encoding: "utf8",
});
assert.equal(coercionNode.status, 0, coercionNode.stderr);
assert.equal(coercionNode.stdout, "value\n");

// The line names its entry, so a provider can import the entry back.
const cycleFiles = {
  "main.mjs": 'import { read } from "./reader.mjs"; export const value = 7; console.log(read());',
  "reader.mjs": 'import { value } from "./main.mjs"; export function read() { return value; }',
};
const cycleDir = path.join(dir, "cycle");
mkdirSync(cycleDir, { recursive: true });
for (const [name, text] of Object.entries(cycleFiles)) writeFileSync(path.join(cycleDir, name), text);
const [cycle] = run(
  [
    {
      ...dumpSource(cycleFiles["main.mjs"]),
      entry: "main.mjs",
      providers: { "reader.mjs": dumpSource(cycleFiles["reader.mjs"]) },
    },
  ],
  true
);
const cycleNode = spawnSync(process.execPath, [path.join(cycleDir, "main.mjs")], { encoding: "utf8" });
assert.equal(cycleNode.status, 0, cycleNode.stderr);
assert.deepEqual([cycle.end, cycle.out], ["normal", cycleNode.stdout.split("\n").slice(0, -1)]);

// Every assignment form on an imported binding reads it, then throws TypeError on the write.
const frozenFiles = {
  "main.mjs": [
    'import { n, zero } from "./m.mjs";',
    'try { n = 5; } catch (e) { console.log("assign", n); }',
    'try { n++; } catch (e) { console.log("update", n); }',
    'try { n += (console.log("right side runs"), 1); } catch (e) { console.log("compound", n); }',
    "console.log(n ||= 7, n);",
    'try { zero ||= 7; } catch (e) { console.log("logical", zero); }',
  ].join("\n"),
  "m.mjs": "export let n = 1; export let zero = 0;",
};
const frozenDir = path.join(dir, "frozen");
mkdirSync(frozenDir, { recursive: true });
for (const [name, text] of Object.entries(frozenFiles)) writeFileSync(path.join(frozenDir, name), text);
const [frozen] = run(
  [{ ...dumpSource(frozenFiles["main.mjs"]), providers: { "m.mjs": dumpSource(frozenFiles["m.mjs"]) } }],
  true
);
const frozenNode = spawnSync(process.execPath, [path.join(frozenDir, "main.mjs")], { encoding: "utf8" });
assert.equal(frozenNode.status, 0, frozenNode.stderr);
assert.deepEqual([frozen.end, frozen.out], ["normal", frozenNode.stdout.split("\n").slice(0, -1)]);

// Default imports and the four forms of `export default`, compared with Node on disk.
const defaultFiles = {
  "main.mjs":
    'import f, { other } from "./f.mjs"; import value from "./value.mjs"; import Shape from "./shape.mjs";' +
    ' import anonymous from "./anonymous.mjs";' +
    ' throw f(1) + "," + other + "," + value + "," + new Shape().sides() + "," + anonymous() + "," +' +
    ' f.name + "," + Shape.name + "," + anonymous.name;',
  "f.mjs": "export default function named(x) { return x + 1; } export const other = 2;",
  "value.mjs": "export default 6 * 7;",
  "shape.mjs": "export default class { sides() { return 4; } }",
  "anonymous.mjs": 'export default function () { return "anonymous"; }',
};
const defaultDir = path.join(dir, "defaults");
mkdirSync(defaultDir, { recursive: true });
for (const [name, text] of Object.entries(defaultFiles)) writeFileSync(path.join(defaultDir, name), text);
const providers = Object.fromEntries(
  Object.entries(defaultFiles)
    .filter(([name]) => name !== "main.mjs")
    .map(([name, text]) => [name, dumpSource(text)])
);
const [defaults] = run([{ ...dumpSource(defaultFiles["main.mjs"]), providers }], true);
assert.deepEqual(
  [defaults.end, defaults.detail],
  await nodeOutcome(pathToFileURL(path.join(defaultDir, "main.mjs")).href)
);

// Top-level blocks, loops and switches: a var binds a module cell; let and const stay in the block.
// A function declaration in a top-level block is still refused (function_declaration_test).
const blockSource = [
  "{ let hidden = 1; var visible = hidden + 1; const made = () => visible; console.log(made()); }",
  "console.log(visible, typeof hidden, typeof made);",
  'let i = 0; while (i < 3) { i++; if (i === 2) continue; console.log("loop", i); }',
  'switch (i) { case 3: console.log("three"); break; default: console.log("other"); }',
].join("\n");
const [blocks] = run([dumpSource(blockSource)], true);
const blocksNode = spawnSync(process.execPath, ["--input-type=module", "-e", blockSource], { encoding: "utf8" });
assert.equal(blocksNode.status, 0, blocksNode.stderr);
assert.deepEqual([blocks.end, blocks.detail, blocks.out], ["normal", "", blocksNode.stdout.split("\n").slice(0, -1)]);

const spec = file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["a", "b"] } }));
function split(source, args = []) {
  const ast = file("bundle.json", JSON.stringify(dumpSource(source)));
  return execFileSync(process.env.LOWERSPLIT, ["--diagnostic", writeDeployment(ast, spec, dir), dir, ...args], {
    encoding: "utf8",
  });
}

const source = "var a = 1, b = a;";
// The checker obtains identities from Program.body even when presented with a
// conflicting legacy table. A table on its own cannot stand in for a module.
const program = dumpSource(source);
assert.equal(program.type, "Program");
assert.equal(program.body.length, 1);
assert.equal(program.body[0].declarations.length, 2);
const [fromBody, legacyOnly] = run([{ ...program, statements: [] }, { statements: [] }], true);
assert.deepEqual(fromBody, run([reference(source)], false)[0]);
assert.equal(legacyOnly.end, "malformed");

// Acorn ranges count UTF-16 code units. Exercise both a supplementary
// character before the declaration and one inside the first declarator.
split('/*😀*/ export var a = "🌱", b = a;');
const badRange = dumpSource("/*😀*/ var a = 1, b = a;");
badRange.body[0].declarations[0].start = 3;
const invalid = file("bad-range.json", JSON.stringify(badRange));
assert.throws(
  () =>
    execFileSync(process.env.LOWERSPLIT, ["--diagnostic", writeDeployment(invalid, spec, dir), dir], { stdio: "pipe" }),
  /surrogate pair/
);
const moves = path.join(dir, "moves.json");
assert.match(split(source, ["--move-report", moves]), /model diagnostic: order kept/);
const nativeReport = JSON.parse(readFileSync(moves));
assert.equal(nativeReport.footprintsOfSyntax, true);
assert.equal("externallyLowered" in nativeReport, false);
// A JSON CoreJS program is not an alternate source for a splitter verdict.
const sidecar = file("other.json", JSON.stringify(reference("var a = 2, b = a;")));
const sidecarOption = spawnSync(
  process.env.LOWERSPLIT,
  [
    "--diagnostic",
    writeDeployment(file("bundle.json", JSON.stringify(dumpSource(source))), spec, dir),
    dir,
    "--corejs",
    sidecar,
  ],
  { encoding: "utf8" }
);
assert.notEqual(sidecarOption.status, 0);
assert.match(sidecarOption.stderr, /usage/);
const unsupported = "var a = [], b = a;";
assert.match(split(unsupported), /model diagnostic: none\..*statement 0: unsupported expression ArrayExpression/);

// Use both forms of the Lean placement spec. For each generated declaration list,
// keep source order in one plan and move its final independent binding in the other.
for (let width = 2; width <= 4; width++) {
  const names = Array.from({ length: width }, (_, i) => `d${i}`);
  const declarations = names
    .map((name, i) => {
      const number = i === 0 ? "-0" : `-${width + i}.5`;
      return `${name} = ${"!".repeat(i)}${number}`;
    })
    .join(", ");
  const code = `${["let", "const", "var"][width - 2]} ${declarations}; export const result = ${names[0]};`;
  for (const moved of [0, width - 1]) {
    const side = `side-${width}-${moved}.mjs`;
    const modules = {
      [side]: [names[moved]],
      "entry.mjs": [...names.filter((_, i) => i !== moved), `#${width}`],
    };
    file("spec.json", JSON.stringify({ entry: "entry.mjs", modules }));
    const log = split(code);
    assert.match(log, /Declaration extraction: unsplit declaration lists and the checked CoreJS plan agree/);
    assert.match(log, moved === 0 ? /all modeled outcomes/ : /normally terminating executions/);
    const original = await import(`data:text/javascript,${encodeURIComponent(code)}`);
    assert.ok(Object.is(original.result, -0));
  }
}

// This declaration-only AST is split without `--corejs`; the certificate also
// covers a lexical read that throws in Node because the later declarator is in
// its TDZ.
file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["early", "late"] } }));
const tdzDeclaration = "export const early = !late, late = -2;";
const tdzLog = split(tdzDeclaration);
assert.match(
  tdzLog,
  /Declaration extraction: unsplit declaration lists and the checked CoreJS plan agree on all modeled outcomes/
);
assert.match(tdzLog, /FootprintsOfSyntax: against Lean ESTree lowering/);
assert.deepEqual(await nodeOutcome(`data:text/javascript,${encodeURIComponent(tdzDeclaration)}#tdz-declaration`), [
  "throw",
  "ReferenceError",
]);
file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["a", "b"] } }));
assert.match(split("var a; let b;"), /Declaration extraction: .*all modeled outcomes/);
assert.match(split("export let a = b, b = 1;"), /Declaration extraction: .*all modeled outcomes/);
assert.doesNotMatch(split(unsupported), /Declaration extraction:/);

// Gaps in executable bodies still have source ordinals. Vary the width of an
// exported list before a destructuring and an opaque closure so their IDs move.
for (let width = 1; width <= 6; width++) {
  const names = Array.from({ length: width }, (_, i) => `n${i}`);
  const code = `import {bag} from "./dep.mjs";
    export var ${names.map((n, i) => `${n} = ${i}`).join(", ")},
      {value: x = n0} = bag, read = ({v}) => x + v;
    export {x as renamed}; debugger; import "./side.mjs"; read;`;
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const dump = dumpParsed(parsed);
  const tree = bundleFromParsed(parsed);
  const core = coreFromParsed(parsed, { external: true });
  const bindings = [...names, "x", "read"];
  assert.equal(dump.body.length, 6);
  assert.equal(core.numbered, width + 7);
  const declarators = dump.body[1].declaration.declarations;
  assert.deepEqual(
    declarators.map((d) => d.id.type),
    [...names.map(() => "Identifier"), "ObjectPattern", "Identifier"]
  );
  assert.deepEqual(tree.program, dump);
  assert.deepEqual(
    core.exports.map(([name]) => name),
    [...bindings, "renamed"]
  );
  assert.deepEqual(core.ids, [...bindings.map((_, i) => i + 1), width + 6]);
  assert.deepEqual(
    core.opaque.map((o) => o.id),
    [width + 1, width + 2]
  );
  const destructuring = declarators[width];
  assert.equal(destructuring.init.k, "V");
  assert.equal(destructuring.id.properties[0].value.right.k, "T");
  const closure = declarators[width + 1].init;
  assert.equal(closure.body.left.k, "T");
  assert.equal(closure.body.right.k, "L");
  assert.equal(closure.body.right.b, closure.params[0].properties[0].value.b);
  // Linked programs take their IDs from the caller's compact table (imports are linked separately).
  const ids = Array.from({ length: width + 5 }, (_, i) => 100 + 2 * i);
  const linked = coreFromParsed(parsed, { ids });
  assert.deepEqual(linked.ids, [...ids.slice(0, bindings.length), ids.at(-1)]);
}
