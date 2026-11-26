import assert from "node:assert/strict";
import {
  array,
  arrow,
  assertRejectedOrOrderKeptSlice,
  assertWorkerResult,
  binary,
  block,
  call,
  exportNamed,
  id,
  literal,
  observe,
  primitive,
  program,
  returns,
  throws,
  unary,
  variable,
  workerSplit,
} from "./harness.mjs";

const entryUnits = ["#1", "#2"];

// Run the comparisons during initialization. A wrong numeric result then
// prevents certification even when both sides lower the operator the same way.
for (const [name, cases] of [
  [
    "bitwise",
    [
      [binary("&", primitive(0x100000001), primitive(3)), 1],
      [binary("|", primitive(" \t0x10 "), primitive("2")), 18],
      [binary("^", primitive(-3.9), primitive(true)), -4],
      [binary("|", primitive(Infinity), primitive("NaN")), 0],
    ],
  ],
  [
    "shifts",
    [
      [binary("<<", primitive(1), primitive(63)), -0x80000000],
      [binary("<<", primitive(1), primitive(-1)), -0x80000000],
      [binary(">>", primitive(-3), primitive(1)), -2],
      [binary(">>>", primitive(-1), primitive(0)), 0xffffffff],
      [binary(">>>", primitive(-1), primitive(33.5)), 0x7fffffff],
    ],
  ],
  [
    "unary",
    [
      [unary("+", primitive("0b101")), 5],
      [unary("-", primitive("5")), -5],
      [unary("-", primitive(true)), -1],
      [unary("~", primitive(0x80000000)), 0x7fffffff],
      [unary("~", primitive(undefined)), -1],
    ],
  ],
]) {
  const values = cases.map((_, i) => id(`value${i}`));
  const body = cases.flatMap(([expression, expected], i) => [
    variable("const", values[i].name, expression),
    {
      type: "IfStatement",
      test: binary("!==", values[i], primitive(expected)),
      consequent: throws(literal(`wrong ${name} result ${i}`)),
      alternate: null,
    },
  ]);
  assertWorkerResult(
    name,
    program([
      variable("const", "worker", arrow([], block([...body, returns(array(values))]))),
      variable("const", "result", call(id("worker"))),
      exportNamed([["result", "result"]]),
    ]),
    cases.map(([, expected]) => expected),
    entryUnits
  );
}

assertWorkerResult(
  "unary-zero-and-nan",
  `const worker = () => {
    const zero = +"-0";
    const invalid = +"not a number";
    if (1 / zero !== -Infinity || 1 / -false !== -Infinity || invalid === invalid) throw "bad conversion";
    return true;
  };
  const result = worker(); export { result };`,
  true,
  entryUnits
);

assertWorkerResult(
  "shift-operand-order",
  `const worker = () => {
    let phase = 0;
    const left = () => {
      if (phase !== 0) throw "left repeated";
      phase = 1;
      return { valueOf: () => { throw "left conversion ran"; } };
    };
    const right = () => {
      if (phase !== 1) throw "right out of order";
      throw "right stopped";
    };
    try { return left() >>> right(); }
    catch (error) {
      if (error !== "right stopped") throw error;
      return error;
    }
  };
  const result = worker(); export { result };`,
  "right stopped",
  entryUnits
);

for (const [name, expression, reason, runtime] of [
  ["bitwise-object", "({ valueOf: () => 3 }) & 1", /object is converted to a primitive/, { result: 1 }],
  ["unary-object", "+({ valueOf: () => 3 })", /object is converted to a primitive/, { result: 3 }],
  ["bitwise-bigint", "1n & 1", /unsupported literal/, { error: "TypeError" }],
  ["exponentiation", "2 ** 3", /unsupported linked binary operator/, { result: 8 }],
]) {
  const { files, out, verdict, log } = workerSplit(
    name,
    `const worker = () => ${expression};
    const result = worker(); export { result };`,
    entryUnits
  );
  assertRejectedOrOrderKeptSlice({ verdict, log, out }, reason);
  assert.deepEqual(observe(files.sourceFile, "result"), runtime);
}
