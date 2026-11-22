// Compose setup, dispatch, and observation separately so one protocol can run through several built-in operators.
import { rng } from "./core.mjs";

const arrMethods = {
  map: "(x) => x",
  filter: "(x) => x > 0",
  slice: "0, 1",
  splice: "0, 1",
  concat: "[4]",
  flat: "1",
  flatMap: "(x) => [x]",
};

const regexpConsumers = {
  exec: (pattern, text) => `${pattern}.exec(${text})`,
  test: (pattern, text) => `${pattern}.test(${text})`,
  replace: (pattern, text) => `${text}.replace(${pattern}, "_")`,
  match: (pattern, text) => `${text}.match(${pattern})`,
  split: (pattern, text) => `${text}.split(${pattern})`,
};

const installers = {
  accessor: {
    fn: "Object.defineProperty",
    args: (delta) => `box, "read", { get() { meter.n += ${delta}; return 1; } }`,
    observe: "box.read;",
  },
  conversion: {
    fn: "Reflect.set",
    args: (delta) => `box, "toString", () => { meter.n += ${delta}; return "text"; }`,
    observe: "String(box);",
  },
  prototype: {
    fn: "Object.setPrototypeOf",
    args: (delta) => `box, { get read() { meter.n += ${delta}; return 1; } }`,
    observe: "box.read;",
  },
};

export const requiredFamilies = { species: 14, array_factory: 3, inspect: 2, regexp: 5, installed_hook: 9 };

function program(name, family, knownGap, setup, run, observe = "const seen = meter.n;") {
  const lines = ["const x = 3;", "const y = 5;", ...setup, run, observe, "console.log(x + y, seen);"];
  const control = lines.map((_, index) => index);
  [control[0], control[1]] = [control[1], control[0]];
  const reordered = lines.map((_, index) => index);
  const reader = reordered.length - 2;
  [reordered[reader - 1], reordered[reader]] = [reordered[reader], reordered[reader - 1]];
  return {
    name,
    family,
    premises: knownGap === "P3" ? { noWorldHooks: true } : {},
    source: `${lines.join("\n")}\n`,
    reorders: [
      { order: control, behavior: "same", accepted: true },
      { order: reordered, behavior: "differs", stdoutDiffers: true, knownGap },
    ],
  };
}

export function generateCases(seed) {
  const random = rng(seed);
  const pick = (values) => values[random.int(values.length)];
  const score = () => 1 + random.int(9);
  const meter = "const meter = { n: 0 };";
  const speciesInstallation = {
    assignment: "array.constructor = { [Symbol.species]: make };",
    descriptor: 'Object.defineProperty(array, "constructor", { value: { [Symbol.species]: make } });',
  };
  const species = Object.entries(arrMethods).flatMap(([method, args]) =>
    Object.entries(speciesInstallation).map(([kind, store]) => {
      const init = pick(["[1, 2]", "Array.of(1, 2)"]);
      return program(
        `species via ${method} after ${kind}`,
        "species",
        "intrinsic summaries",
        [meter, `const array = ${init};`, `function make() { meter.n += ${score()}; return {}; }`, store],
        `array.${method}(${args});`
      );
    })
  );
  const factories = [
    ["from", "[1, 2]", "iterable"],
    ["from", "{ 0: 1, length: 1 }", "array-like"],
    ["of", "1, 2", "arguments"],
  ].map(([method, args, kind]) =>
    program(
      `Array.${method} with a subclass and ${kind}`,
      "array_factory",
      "intrinsic summaries",
      [meter, `class Derived extends Array { constructor(...args) { super(...args); meter.n += ${score()}; } }`],
      `Derived.${method}(${args});`
    )
  );
  const inspect = ["{ nested: hooked }", "[{ nested: hooked }]"].map((shape) =>
    program(
      `Node inspection of ${shape}`,
      "inspect",
      "intrinsic summaries",
      [
        meter,
        `const hooked = { [Symbol.for("nodejs.util.inspect.custom")]() { meter.n += ${score()}; return "hook"; } };`,
        `const argument = ${shape};`,
      ],
      pick(["console.log(argument);", 'console.log("value %o", argument);'])
    )
  );
  const regexp = Object.entries(regexpConsumers).map(([method, make]) => {
    const [a, b] = pick(["ab", "cd", "ef"]);
    const capture = 1 + random.int(2);
    return program(
      `RegExp.$${capture} via ${method}`,
      "regexp",
      "P1",
      [],
      `${make(`/(${a})(${b})/`, JSON.stringify(a + b))};`,
      `const seen = RegExp.$${capture};`
    );
  });
  const hooks = Object.entries(installers).flatMap(([kind, { fn, args, observe }]) => {
    const argumentsText = args(score());
    const direct = `${fn}(${argumentsText});`;
    const forms = {
      iterator: [`const install = () => { ${direct} };`, "[1].values().forEach(install);"],
      generator: [`function* install() { ${direct} yield 1; }`, "install().next();"],
      apply: [`Reflect.apply(${fn}, null, [${argumentsText}]);`],
    };
    return Object.entries(forms).map(([dispatch, installation]) =>
      program(
        `install ${kind} via ${dispatch}`,
        "installed_hook",
        "P3",
        [meter, "const box = {};", ...installation],
        observe
      )
    );
  });
  return [...species, ...factories, ...inspect, ...regexp, ...hooks].map((test) => ({
    ...test,
    name: `seed ${seed}: ${test.name}`,
  }));
}
