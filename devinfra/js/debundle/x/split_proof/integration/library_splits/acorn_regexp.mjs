import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";

// Read and link the artifact as written. A fresh realm for each run makes the
// supplied lookup and the failure probe independent of module caching.
async function observe(entry, stopAtFirst) {
  const context = vm.createContext({});
  const NativeRegExp = vm.runInContext("RegExp", context);
  const trace = [];
  const sentinel = new Error("supplied RegExp constructor failed");
  let next = 0;
  let reentrantBindings;
  Object.defineProperty(context, "RegExp", {
    get() {
      const index = next++;
      trace.push(["lookup", index]);
      function Replacement() {
        assert.fail("expected construction");
      }
      const replacement = new Proxy(Replacement, {
        construct(_target, args, newTarget) {
          assert.equal(newTarget, replacement);
          assert(args.every((arg) => typeof arg === "string"));
          trace.push(["construct", index, [...args]]);
          if (index === 0) {
            // While the first construction is still in progress, invoke the
            // already linked public function on an ASCII input.
            const ascii = main.namespace.isIdentifierStart(65);
            reentrantBindings = Object.keys(main.namespace).map((name) => {
              try {
                return [name, typeof main.namespace[name]];
              } catch (error) {
                assert.equal(error.name, "ReferenceError");
                return [name, "tdz"];
              }
            });
            trace.push(["reentry", ascii]);
            assert.equal(ascii, true);
          }
          if (stopAtFirst) throw sentinel;
          const value = Reflect.construct(NativeRegExp, args);
          const test = value.test;
          value.test = function (input) {
            assert.equal(this, value, "public code must use the object returned by construction");
            const answer = Reflect.apply(test, this, [input]);
            trace.push(["test", index, input, answer]);
            return answer;
          };
          return value;
        },
      });
      return replacement;
    },
  });
  const modules = new Map();
  function module(file) {
    if (modules.has(file)) return modules.get(file);
    const result = new vm.SourceTextModule(readFileSync(file, "utf8"), { context, identifier: file });
    modules.set(file, result);
    return result;
  }
  const main = module(entry);
  await main.link((request, parent) => {
    assert(request.startsWith("./") || request.startsWith("../"), request);
    return module(path.resolve(path.dirname(parent.identifier), request));
  });
  assert.deepEqual(trace, [], "construction belongs to evaluation, not linking");
  if (stopAtFirst) {
    await assert.rejects(main.evaluate(), (error) => error === sentinel);
    assert.deepEqual(
      trace.map(([kind]) => kind),
      ["lookup", "construct", "reentry"]
    );
    return trace;
  }
  await main.evaluate();
  const api = main.namespace;
  const evaluation = trace.splice(0);
  const pairs = evaluation.filter(([kind]) => kind !== "reentry");
  assert(pairs.length > 0 && pairs.length % 2 === 0);
  for (let i = 0; i < pairs.length; i += 2) {
    assert.deepEqual(pairs[i], ["lookup", i / 2]);
    assert.deepEqual(pairs[i + 1].slice(0, 2), ["construct", i / 2]);
  }
  const result = [
    api.isIdentifierStart(0x3b1),
    api.isIdentifierChar(0x301),
    api.parse("const α = 1", { ecmaVersion: "latest" }).body[0].declarations[0].id.name,
  ];
  assert.deepEqual(result, [true, true, "α"]);
  assert.deepEqual(
    trace.slice(0, 2).map(([kind, , input, result]) => [kind, input, result]),
    [
      ["test", "α", true],
      ["test", "\u0301", true],
    ]
  );
  return { evaluation, calls: trace, result, names: Object.keys(api), reentrantBindings };
}

export async function compareRegExpConstruction({ original, emitted, units, control }) {
  const source = readFileSync(original, "utf8");
  const first = units.slice(8, 10);
  const ranges = first.map(({ node, start, end }, i) => {
    const [decl] = node.declarations;
    assert.equal(decl.id.name, ["nonASCIIidentifierStart", "nonASCIIidentifier"][i]);
    assert.equal(decl.init.type, "NewExpression");
    assert.equal(decl.init.callee.name, "RegExp");
    return { statement: i + 8, unit: [start, end], construct: [decl.init.start, decl.init.end] };
  });
  const [a, b] = first;
  writeFileSync(
    control,
    source.slice(0, a.start) +
      source.slice(b.start, b.end) +
      source.slice(a.end, b.start) +
      source.slice(a.start, a.end) +
      source.slice(b.end)
  );
  const before = await observe(original, false);
  const after = await observe(emitted, false);
  assert.deepEqual(after, before);
  assert.deepEqual(
    before.calls.slice(0, 2).map(([, index]) => index),
    [0, 1]
  );
  const throwsBefore = await observe(original, true);
  assert.deepEqual(await observe(emitted, true), throwsBefore);
  const swapped = await observe(control, false);
  assert.deepEqual(swapped.names, before.names);
  assert.deepEqual(swapped.result, before.result);
  assert.deepEqual(
    swapped.calls.slice(0, 2).map(([, index]) => index),
    [1, 0]
  );
  assert.notDeepEqual(swapped.evaluation, before.evaluation);
  const swappedThrow = await observe(control, true);
  assert.notDeepEqual(swappedThrow, throwsBefore);
  return {
    ranges,
    input: before,
    output: after,
    firstThrow: throwsBefore,
    swappedFirstThrow: swappedThrow,
    swappedEvaluation: swapped.evaluation,
  };
}
