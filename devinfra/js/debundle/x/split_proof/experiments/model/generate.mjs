// Bounded CoreJS programs. Every selected family executes; no unit filtering or unknown-code retry.
// Prefix units 0 and 1 are independent allocations. Swapping them is a required real-emitter move control.
export function generatedCases(seed) {
  let state = seed >>> 0;
  const pick = (xs) => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return xs[state % xs.length];
  };
  const num = () => pick([1, 2, 3, 5, 7]);
  const expression = (depth) => {
    if (depth === 0) return String(num());
    const left = expression(depth - 1);
    const right = expression(depth - 1);
    return pick([
      `(${left} + ${right})`,
      `(${left} * ${right})`,
      `(${left} ^ ${right})`,
      `(${left} < ${right} ? ${left} : ${right})`,
      `(${left} && ${right})`,
      `(${left} ?? ${right})`,
      `(~${left})`,
    ]);
  };
  const n = num();
  const limit = num();
  const families = {
    expressions: `
      let value = ${expression(3)};
      value += ${num()}; value ^= ${num()};
      console.log(value++, --value, !value, typeof value, String(value), null == undefined);
    `,
    closures: `
      let captured = ${n};
      function factory(step) { let privateValue = captured; return () => { privateValue += step; return privateValue; }; }
      const next = factory(${num()});
      const holder = { next, read() { return captured + this.next(); } };
      captured += ${num()};
      console.log(holder.read(), next(), captured, factory.name, factory.length);
    `,
    objects: `
      let writes = 0;
      const base = { inherited: ${n} };
      const object = Object.create(base);
      Object.defineProperty(object, "value", { get() { return writes; }, set(v) { writes = v; }, configurable: true });
      object.value = ${num()};
      console.log(object.value, "value" in object, delete object.value, object.value, object.inherited);
      Object.setPrototypeOf(object, null);
      console.log(object.inherited, Object.getPrototypeOf(object));
    `,
    arrays: `
      const array = [${n}, [${num()}, ${num()}], "value"];
      const same = array;
      console.log(array[0], array[1][0], array[1].length, array.length, same === array);
      console.log(0 in array, 9 in array, array[9], Object.getPrototypeOf(array) === Object.getPrototypeOf([]));
    `,
    classes: `
      class Box {
        static count = 0;
        field = ${n};
        constructor(value) { this.value = value; Box.count++; }
        get total() { return this.field + this.value; }
        set total(value) { this.value = value - this.field; }
        read = () => this.total;
      }
      const box = new Box(${num()});
      box.total = ${num()};
      const read = box.read;
      console.log(read(), Box.count, Box.name, box.constructor === Box);
    `,
    control: `
      let value = 0;
      const saved = {};
      for (let i = 0; i < ${limit}; i++) { saved[i] = () => i; if (i === 1) continue; value += i; }
      let left = ${limit};
      while (left > 0) { left--; if (left === 1) break; value++; }
      value++;
      console.log(value, left, saved[0](), saved[${limit - 1}]());
    `,
    exceptions: `
      let trace = "";
      function finish() { try { throw ${n}; } catch (e) { trace += e; return e; } finally { trace += "f"; } }
      function override() { try { return ${n}; } finally { return ${num()}; } }
      console.log(finish(), override(), trace);
      try { null.x; } catch (e) { console.log("engine error caught"); }
      console.log("after catch");
    `,
    host: `
      console.log(Math.max(${n}, ${num()}), parseInt("${num()}2", 10), " abc ".trim().toUpperCase());
      try { "x".repeat(-${n}); } catch (e) { console.log("host error caught"); }
      console.log((123).toFixed(${n % 3}), String.fromCharCode(65 + ${n}));
    `,
  };
  return Object.entries(families).map(([family, body]) => {
    const [allocation, observation] =
      family === "objects"
        ? [`{ value: ${n} }`, "independent.value"]
        : family === "arrays"
          ? [`[${n}]`, "independent[0]"]
          : [`() => ${n}`, "independent()"];
    return {
      name: `core generator ${family} seed ${seed}`,
      family,
      source: `const independent = ${allocation};\nconst first = ${num()};\n${body}\nconsole.log("${family}:${seed}", ${observation}, first);`,
    };
  });
}

// Linked programs are kept whole too: live updates in a diamond, caught TDZ in a cycle, and an uncaught exit.
export function generatedModuleCases(seed) {
  const n = 1 + ((seed >>> 0) % 7);
  return [
    {
      name: `linked live diamond seed ${seed}`,
      files: {
        "main.mjs": `import { value as local, bump } from "./state.mjs"; import { read } from "./reader.mjs";
          console.log(local, read()); bump(); console.log(local, read());`,
        "state.mjs": `export let value = ${n}; export function bump() { value += ${n}; }`,
        "reader.mjs": `import { value } from "./state.mjs"; export function read() { return value; }`,
      },
    },
    {
      name: `linked cycle caught dead zone seed ${seed}`,
      files: {
        "main.mjs": `import "./early.mjs"; export let value = ${n}; export function read() { return value; }
          console.log("initialized", read());`,
        "early.mjs": `import { read } from "./main.mjs";
          try { read(); } catch (e) { console.log("caught dead zone"); }`,
      },
    },
    {
      name: `linked abrupt completion seed ${seed}`,
      end: "throw",
      files: {
        "main.mjs": `import "./early.mjs"; import "./unreached.mjs"; console.log("unreached main");`,
        "early.mjs": `console.log("before throw", ${n}); throw ${n};`,
        "unreached.mjs": `console.log("unreached dependency");`,
      },
    },
  ];
}

export function generatedOpaqueCase(seed) {
  const n = 1 + ((seed >>> 0) % 7);
  const callback = seed % 2 === 0 ? `throw value;` : `return value + ${n};`;
  return {
    name: `opaque closure callback ${seed % 2 === 0 ? "caught throw" : "normal return"} seed ${seed}`,
    source: `
      const pick = ({ value }) => add(value);
      const independent = ${n};
      const add = (value) => { ${callback} };
      try { console.log(pick({ value: ${n} })); } catch (e) { console.log("caught", e); }
      console.log("after", independent);
    `,
    // The opaque closure captures add before its initializer runs, then reenters it after initialization.
    scripts: {
      "0/1": [
        ["get", ["r", 2], ["s", "value"]],
        ["readVar", 1],
        ["call", ["r", 4], ["u"], ["r", 3]],
        ["done", ["r", 5]],
      ],
    },
    move: true,
  };
}

// The class crosses an independent allocation before its captured binding is initialized.
// The scripts read current bindings and receivers; no generated result is hard-coded in the world.
export function generatedOpaqueClassCase(seed) {
  const value = 1 + ((seed >>> 0) % 17);
  return {
    name: `opaque class capture seed ${seed}`,
    source: `
      class Box {
        constructor(argument) { const [v] = [captured]; this.value = v; this.argument = argument; }
        read() { const [v] = [this.argument]; return v; }
      }
      const independent = ${value};
      let captured = ${value + 1};
      captured = ${value + 2};
      const box = new Box(${value + 3});
      console.log(box.value, box.read(), independent, box.constructor === Box);
    `,
    scripts: {
      "0/1": [
        ["readVar", 0],
        ["set", ["r", 1], ["s", "value"], ["r", 3]],
        ["set", ["r", 1], ["s", "argument"], ["r", 2]],
      ],
      "0/2": [
        ["get", ["r", 1], ["s", "argument"]],
        ["done", ["r", 2]],
      ],
    },
    bodyParts: ["0/1", "0/2"],
    generatedClass: true,
    move: true,
  };
}

// Each generated plan crosses two real writes, with each assignment kept beside its own declaration.
// Successful certification is mandatory; these cases do not rely on random split selection.
export function generatedBindingCases(seed) {
  const value = 1 + ((seed >>> 0) % 17);
  const literals = ["undefined", "null", "false", "true", String(value), JSON.stringify(`value-${value}`)];
  const literal = literals[(seed >>> 0) % literals.length];
  const assigned = `(x = ${value})`;
  const unaryExpressions = [
    `-${assigned}`,
    `+${assigned}`,
    `!${assigned}`,
    `~${assigned}`,
    `void ${assigned}`,
    "`value=${" + assigned + "}`",
  ];
  const unary = unaryExpressions[(seed >>> 0) % unaryExpressions.length];
  const spec = { entry: "entry.mjs", modules: { "m0.mjs": ["#1", "#3"], "entry.mjs": ["#0", "#2", "#4"] } };
  return [
    {
      name: `certified binding assignments seed ${seed}`,
      bindingFamily: "literal",
      source: `let x = 0; let y = 0; (x = ${literal}, x); (y = ${value}, y); console.log(x, y);`,
      spec,
      certified: true,
      generatedBinding: true,
    },
    {
      name: `certified binary operands seed ${seed}`,
      bindingFamily: "binary",
      source: `let x = 0; let y = 0;
        (x = (x = ${value}) ${["+", "*", "<", "===", "^", "<<"][(seed >>> 0) % 6]} (x = ${value + 1}), x);
        (y = (y = ${value + 2}) + (y = ${value + 3}), y); console.log(x, y);`,
      spec,
      certified: true,
      generatedBinding: true,
    },
    {
      name: `certified unary operand seed ${seed}`,
      bindingFamily: "unary",
      source: `let x = 0; let y = 0; (x = ${unary}, x); (y = ${value}, y); console.log(x, y);`,
      spec,
      certified: true,
      generatedBinding: true,
    },
    {
      name: `certified conditional and short circuit seed ${seed}`,
      bindingFamily: "branch",
      source: `let x = 0; let y = 0;
        (x = ((x = ${seed % 2 === 0}) ? (x = ${value}) : (x = ${value + 1})), x);
        (y = ((y = ${seed % 2 === 0 ? "null" : "7"}) ${["&&", "||", "??"][(seed >>> 0) % 3]} (y = ${value})), y);
        console.log(x, y);`,
      spec,
      certified: true,
      generatedBinding: true,
    },
    {
      name: `dependent binding assignments seed ${seed}`,
      source: `let x = 0; let y = 0; (x = ${literal}, x); (y = x, y); console.log(x, y);`,
      spec,
      refused: true,
      generatedBinding: true,
    },
  ];
}

export function generatedInheritanceCases(seed) {
  const n = 1 + ((seed >>> 0) % 7);
  return [
    {
      name: `default inheritance seed ${seed}`,
      family: "inheritance",
      source: `
      const independent = () => ${n};
      const first = ${n + 1};
      class Base { field = ${n}; constructor(value) { this.value = value; } }
      class Middle extends Base {}
      class Leaf extends Middle { read() { return this.field + this.value; } }
      const instance = new Leaf(${n + 2});
      console.log(instance.read(), Object.getPrototypeOf(instance) === Leaf.prototype,
        Object.getPrototypeOf(Leaf) === Middle, Leaf.length, independent(), first);
    `,
    },
  ];
}

export function generatedHeritageSplit(seed) {
  const n = 1 + ((seed >>> 0) % 7);
  const locals = Array.from({ length: 1 + ((seed >>> 0) % 4) }, (_, i) => `config${i}`);
  const pivot = 2;
  const crossed = locals.map((_, i) => pivot + 1 + i);
  return {
    name: `certified heritage seed ${seed}`,
    source: `
      var Base = class { constructor(value) { this.value = value; } };
      var Earlier = class extends Base { read() { return this.value + ${n}; } };
      var Pivot = class extends Base { get adjusted() { return this.value * ${n + 1}; } };
      ${locals.map((name, i) => `var ${name} = { value: ${n + i} };`).join("\n")}
      console.log(new Earlier(${n + 2}).read(), new Pivot(${n + 3}).adjusted,
        Object.getPrototypeOf(Pivot) === Base, Object.getPrototypeOf(Pivot.prototype) === Base.prototype,
        ${locals.map((name) => `${name}.value`).join(", ")});
    `,
    spec: {
      entry: "entry.mjs",
      modules: {
        "prefix.mjs": ["#0", "#1", ...crossed.map((id) => `#${id}`)],
        "entry.mjs": [`#${pivot}`, `#${pivot + locals.length + 1}`],
      },
    },
    certifiedHeritage: { pivot, crossed },
  };
}
