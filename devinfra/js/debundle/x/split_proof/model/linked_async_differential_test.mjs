import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { acorn, dump } from "../translation/parser.mjs";
import {
  afterCaller,
  afterFirst,
  afterRejection,
  afterSecond,
  first,
  payload,
  rejected,
  second,
} from "./linked_async_fixture.mjs";

function model(source, mode) {
  return JSON.parse(
    execFileSync(process.env.LINKED_ASYNC_TEST, {
      input: JSON.stringify({ source: dump(source, { includeOpaqueCaptures: true }), mode }) + "\n",
      encoding: "utf8",
    })
  );
}

const reason = rejected.catch((error) => error);
const promises = [first, second, rejected];
const source = readFileSync(new URL("./linked_async_fixture.mjs", import.meta.url), "utf8");
assert.deepEqual(model(source, "normal"), {
  afterFirst,
  afterSecond,
  afterRejection,
  afterCaller,
  firstValue: await first,
  secondValue: await second,
  fresh: new Set(promises).size === promises.length,
  rejectionIdentity: (await reason) === payload,
  noOwnKeys: promises.every((p) => Reflect.ownKeys(p).length === 0),
  sharedPrototype: promises.every((p) => Object.getPrototypeOf(p) === Promise.prototype),
});

for (const source of [
  "async function run() { await 0; } run();",
  "async function run() { for await (const item of []) {} } run();",
  "async function run(x = 1) { return x; } run();",
  "const run = async () => { await 0; }; run();",
]) {
  assert.match(model(source, "unknown").unknown, /deferred function run: async invocation requires Promise semantics/);
}
assert.match(
  model("async function run() { return { then() {} }; } run();", "unknown").unknown,
  /async object resolution requires thenable semantics/
);

// Use the exact arrow bytes from the saved memo case, with a host-free
// caller that supplies its lexical label independently of the real cache.
const memo = readFileSync(
  new URL("../../../e2e/cases/emission/syntactic_holes/memoized_sequence_tail/static/app.js", import.meta.url),
  "utf8"
);
const parsed = acorn.parse(memo, { ecmaVersion: "latest", sourceType: "module" });
const assignment = parsed.body[1].body.body[1].argument.expressions[0].consequent.expressions[0];
assert.equal(assignment.left.name, "memo");
assert.equal(assignment.right.type, "ArrowFunctionExpression");
assert.equal(assignment.right.async, true);
const exactArrow = memo.slice(assignment.right.start, assignment.right.end);

async function arrows(source) {
  const { run, first, second } = await import("data:text/javascript," + encodeURIComponent(source));
  let constructor = true;
  try {
    Reflect.construct(Object, [], run);
  } catch (err) {
    assert(err instanceof TypeError);
    constructor = false;
  }
  assert.deepEqual(model(source, "arrows"), {
    values: [await first, await second],
    constructor,
    asyncPrototype: Object.getPrototypeOf(run) === Object.getPrototypeOf(async function () {}),
    ownPrototype: Object.hasOwn(run, "prototype"),
  });
}

await arrows(`
  let label = "initial";
  const run = ${exactArrow};
  label = "first";
  const first = run("a");
  label = "second";
  const second = run("b");
  export {run, first, second};
`);

await arrows(`
  const maker = { tag: "lexical", make(seed) {
    let live = seed;
    const run = async (id) => {
      return this.tag + ":" + local();
      function local() { return live + id; }
    };
    live = seed + 1;
    return run;
  }};
  const run = maker.make(7);
  const recipient = {tag: "wrong", run};
  const first = run(2);
  const second = recipient.run(3);
  export {run, first, second};
`);

await arrows(`
  const label = "concise:";
  const run = async (id) => label + id;
  const first = run("a");
  const second = run("b");
  export {run, first, second};
`);

// Run the unchanged saved member call on a Promise whose prototype getter
// was replaced. The model compares the original scope with its capture.
assert.deepEqual(model(memo, "memo-call"), {
  value: "load:first:a",
  trace: ["lookup", "call"],
  receiverSeen: true,
  callbackSeen: true,
  queue: [1],
});
