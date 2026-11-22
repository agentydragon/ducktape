import { BINARY, LOGICAL, UNARY } from "./to_core.mjs";
import { rng } from "../translation/gen/core.mjs";

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
      `(!${left})`,
      `(-${left})`,
    ]);
  };
  const n = num();
  const limit = num();
  const families = {
    expressions: `
      let value = ${expression(3)};
      {
        const right = ${expression(2)};
        ${Object.entries(UNARY)
          .map(([op, name]) => `console.log("unary ${name}", ${op} value);`)
          .join("\n")}
        ${Object.entries(BINARY)
          .map(([op, name]) => `console.log("binary ${name}", value ${op} right);`)
          .join("\n")}
        ${Object.entries(LOGICAL)
          .map(([op, name]) => `console.log("logical ${name}", value ${op} right);`)
          .join("\n")}
      }
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

// Linked programs run as complete module graphs, including errors during evaluation.
export function generatedModuleCases(seed) {
  const n = 1 + ((seed >>> 0) % 7);
  const defaults = [
    { declaration: "export default value;", read: "item", after: n },
    { declaration: "export { value as default };", read: "item", after: n + 1 },
    { declaration: "export default function () { return value; }", read: "item()", after: n + 1 },
    { declaration: "export default class { read() { return value; } }", read: "new item().read()", after: n + 1 },
  ];
  const choice = defaults[(seed >>> 0) % defaults.length];
  const write = ["value++", "value += 2", "value = 0"][(seed >>> 0) % 3];
  return [
    {
      name: `linked default export and frozen import seed ${seed}`,
      end: "normal",
      expectedOutput: [`before ${n}`, `read only ${n}`, `after ${choice.after} ${n + 1}`],
      files: {
        "state.mjs": `export let value = ${n}; export function bump() { value++; } ${choice.declaration}`,
        "main.mjs": `import item, { value, bump } from "./state.mjs";
          console.log("before", ${choice.read});
          try { ${write}; } catch (e) { console.log("read only", value); }
          bump(); console.log("after", ${choice.read}, value);`,
      },
    },
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

// Each member renders both its JavaScript body and the requests needed to execute it.
// The observation must call back into modeled JavaScript with the live operands.
function observedClassMember(signature, role, arity, value, capture, finish) {
  const steps = [];
  const request = (...step) => {
    const register = ["r", 2 + arity + steps.length];
    steps.push(step);
    return register;
  };
  // observe is the first free name in the body. Any captured operand follows it.
  const observe = request("readVar", 0);
  const operand = value.request ? request(...value.request) : value.register;
  const args = capture ? [request("readVar", 1)] : [];
  const result = request("call", observe, ["u"], ["s", role], operand, ...args);
  const ending = finish(result);
  steps.push(...ending.steps);
  return {
    source: `${signature} {
      const [observed] = [observe(${JSON.stringify(role)}, ${value.source}${capture ? `, ${capture}` : ""})];
      ${ending.source}
    }`,
    steps,
  };
}

// The class crosses an independent allocation before either of its captured bindings is initialized.
export function generatedOpaqueClassCase(seed) {
  const choice = seed >>> 0;
  const value = 1 + (choice % 17);
  const method = ["instance", "static", "detached"][choice % 3];
  const accessor = ["absent", "instance", "static"][Math.floor(choice / 3) % 3];
  const [ctorCapture, memberCapture] = choice % 2 === 0 ? ["left", "right"] : ["right", "left"];
  const receiver = ["r", 1];
  const argument = ["r", 2];
  const property = (key) => ({ source: `this.${key}`, request: ["get", receiver, ["s", key]] });
  const returning = (result) => ({ source: "return observed;", steps: [["done", result]] });
  const members = [
    observedClassMember(
      "constructor(argument)",
      "constructor",
      1,
      {
        source: ctorCapture,
        request: ["readVar", 1],
      },
      null,
      (result) => ({
        source: "this.value = observed; this.argument = argument;",
        steps: [
          ["set", receiver, ["s", "value"], result],
          ["set", receiver, ["s", "argument"], argument],
        ],
      })
    ),
    observedClassMember(
      method === "static" ? "static read()" : "read()",
      `${method}-method`,
      0,
      method === "detached" ? { source: "this", register: receiver } : property("value"),
      memberCapture,
      returning
    ),
  ];
  const observations = [method === "static" ? "Box.read()" : method === "detached" ? "read()" : "box.read()"];
  const bodyCalls = ["constructor", `${method}-method`];
  let assignment = "";
  if (accessor !== "absent") {
    const prefix = accessor === "static" ? "static " : "";
    const owner = accessor === "static" ? "Box" : "box";
    members.push(
      observedClassMember(`${prefix}get item()`, `${accessor}-getter`, 0, property("stored"), ctorCapture, returning),
      observedClassMember(
        `${prefix}set item(argument)`,
        `${accessor}-setter`,
        1,
        {
          source: "argument",
          register: argument,
        },
        memberCapture,
        (result) => ({
          source: "this.stored = observed;",
          steps: [["set", receiver, ["s", "stored"], result]],
        })
      )
    );
    assignment = `${owner}.item = ${value + 8};`;
    observations.push(`${owner}.item`);
    bodyCalls.splice(1, 0, `${accessor}-setter`);
    bodyCalls.push(`${accessor}-getter`);
  }
  if (choice % 2 === 0) {
    members.push({ source: "modeled() { return this.argument; }" });
    observations.push("box.modeled()");
  }
  // The lexical part numbers include modeled members and are independent of the invocation order.
  const rotation = choice % members.length;
  const ordered = [...members.slice(rotation), ...members.slice(0, rotation)];
  const scripts = Object.fromEntries(
    ordered.flatMap((member, i) => (member.steps ? [[`0/${i + 1}`, member.steps]] : []))
  );
  return {
    name: `opaque class ${method} method, ${accessor} accessor seed ${seed}`,
    source: `
      class Box { ${ordered.map((member) => member.source).join("\n")} }
      const independent = ${value};
      let left = ${value + 1};
      let right = ${value + 2};
      left = ${value + 3};
      right = ${value + 4};
      function observe(role, item, captured) { console.log("body:" + role, item, captured); return item; }
      const box = new Box(${value + 5});
      Box.value = ${value + 6};
      ${method === "detached" ? "const read = box.read;" : ""}
      ${assignment}
      console.log(box.value, box.argument, ${observations.join(", ")}, independent, box.constructor === Box);
    `,
    scripts,
    bodyParts: Object.keys(scripts),
    bodyCalls,
    generatedClass: true,
    move: true,
  };
}

// Give each generated program a fixed split so every seed exercises the intended move or refusal.
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
      name: `fresh record with a negated initializer seed ${seed}`,
      source: `var input = ${literal};
        var record = { flag: ${"!".repeat(1 + ((seed >>> 0) % 3))}input, offset: -${value} };
        var make = () => ${value}; console.log(record.flag, record.offset, make());`,
      spec: { entry: "entry.mjs", modules: { "side.mjs": ["#0", "#2"], "entry.mjs": ["#1", "#3"] } },
      localNegation: true,
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

// Each seed constructs a chain and exercises one additional base-constructor outcome.
export function generatedInheritanceCases(seed) {
  const cycle = (value, length) => ((value % length) + length) % length;
  const field = 1 + cycle(seed, 7);
  const depth = 1 + cycle(seed, 4);
  const arity = 2 + cycle(seed, 3);
  const parameters = Array.from({ length: arity }, (_, i) => `arg${i}`);
  const args = parameters.map((_, i) => 3 + cycle(seed * 5, 13) + i * (field + 2));
  const classes = ["Base", ...Array.from({ length: depth - 1 }, (_, i) => `Middle${i}`), "Leaf"];
  const overrideAt = 1 + cycle(seed, depth);
  const last = parameters.at(-1);
  const outcome = cycle(seed + Math.floor(seed / 4), 3);

  const witnesses = [];
  const observe = (feature, expected, expressions) => {
    witnesses.push({ feature, line: [feature, ...expected].join(" ") });
    return `console.log(${JSON.stringify(feature)}, ${expressions.join(", ")});`;
  };

  const links = (suffix) =>
    classes.slice(1).map((name, i) => `Object.getPrototypeOf(${name}${suffix}) === ${classes[i]}${suffix}`);
  const declaration = outcome === 1 ? `const replacement = { value: ${field + 100} };` : "";
  const earlyCompletion =
    outcome === 1 ? "if (arg0 < 0) return replacement;" : outcome === 2 ? "if (arg0 < 0) throw arg1;" : "";
  const derived = classes.slice(1).map((name, i) => {
    const override = i + 1 === overrideAt ? `measure() { return this.arg0 * ${field + 1} + this.${last}; }` : "";
    return `class ${name} extends ${classes[i]} { ${override} }`;
  });

  let additional;
  if (outcome === 0) {
    const index = 1 + cycle(seed + Math.floor(seed / 2), depth);
    const target = classes[index];
    additional = `
      class Alternate { constructor(a, b) { this.adjusted = a - b; } }
      Object.setPrototypeOf(${target}, Alternate);
      const shifted = new Leaf(${args.join(", ")});
      ${observe(
        "retargeted constructor",
        [args[0] - args[1], true, true],
        [
          "shifted.adjusted",
          "Object.getPrototypeOf(shifted) === Leaf.prototype",
          `Object.getPrototypeOf(${target}.prototype) === ${classes[index - 1]}.prototype`,
        ]
      )}`;
  } else if (outcome === 1) {
    additional = `
      const returned = new Leaf(-1, ${args.slice(1).join(", ")});
      ${observe(
        "returned object",
        [true, field + 100, false],
        ["returned === replacement", "returned.value", "Object.getPrototypeOf(returned) === Leaf.prototype"]
      )}`;
  } else {
    additional = `
      try { new Leaf(-1, ${args.slice(1).join(", ")}); }
      catch (caught) { ${observe("thrown value", [args[1]], ["caught"])} }`;
  }

  const measured = args[0] * (field + 1) + args.at(-1);
  const inherited = args[0] + field;
  const source = `
      const independent = () => ${field};
      const first = ${field + 1};
      ${declaration}
      class Base {
        field = ${field};
        constructor(${parameters.join(", ")}) {
          ${earlyCompletion}
          ${parameters.map((p) => `this.${p} = ${p};`).join(" ")}
          this.receivedPrototype = Object.getPrototypeOf(this);
          this.initialField = this.field;
        }
        baseValue() { return this.arg0 + this.field; }
        measure() { return -this.arg0; }
        get total() { return this.measure() + this.baseValue(); }
      }
      ${derived.join("\n")}
      const instance = new Leaf(${args.join(", ")});
      ${observe(
        "forwarded arguments",
        args,
        parameters.map((p) => `instance.${p}`)
      )}
      ${observe("constructor links", Array(depth).fill(true), links(""))}
      ${observe("prototype links", Array(depth).fill(true), links(".prototype"))}
      ${observe(
        "construction receiver",
        [true, true, true, field, 0],
        [
          "instance.receivedPrototype === Leaf.prototype",
          "Object.getPrototypeOf(instance) === Leaf.prototype",
          "instance.constructor === Leaf",
          "instance.initialField",
          "Leaf.length",
        ]
      )}
      ${observe(
        "method dispatch",
        [inherited, measured, inherited + measured],
        ["instance.baseValue()", "instance.measure()", "instance.total"]
      )}
      ${additional}
      console.log(independent(), first);
    `;
  return [{ name: `default inheritance seed ${seed}`, family: "inheritance", witnesses, source }];
}

export function generatedHeritageSplit(seed) {
  const random = rng(seed);
  const n = 1 + random.int(9);
  const choose = (options, index, count) => {
    const remaining = [...options];
    const first = remaining.splice(index % options.length, 1)[0];
    return [first, ...Array.from({ length: count - 1 }, () => remaining.splice(random.int(remaining.length), 1)[0])];
  };
  const block = (seed >>> 0) % 2 === 0;
  const constructor = block
    ? "constructor(value) { if (value > 0) { const offset = calibration; this.value = value + offset; } else { this.value = value; } }"
    : "constructor(value) { this.value = value; }";
  const base = choose(
    [
      ["method", `baseRead() { return this.value + ${n}; }`, "sample.baseRead()"],
      ["getter", "get baseValue() { return this.value + calibration; }", "sample.baseValue"],
      ["accessor", `get value() { return this.saved; } set value(v) { this.saved = v + ${n}; }`, "sample.saved"],
    ],
    seed >>> 0,
    1 + random.int(3)
  );
  const members = choose(
    [
      ["method", "read() { const old = this.value; return old + calibration; }", "sample.read()"],
      ["getter", `get adjusted() { return this.value * ${n}; }`, "sample.adjusted"],
      [
        "accessor",
        "get savedValue() { return this.savedExtra; } set savedValue(v) { this.savedExtra = v * 2; }",
        "sample.savedValue",
      ],
      ["static", `static label() { return ${JSON.stringify(`label-${n}`)}; }`, "Pivot.label()"],
    ],
    seed >>> 1,
    1 + random.int(4)
  );
  // Each distinct prefix item contributes an observation after the move.
  const between = choose(
    [
      ["inert", "function readCalibration() { return calibration; }", "readCalibration()"],
      ["allocation", "var savedBase = { ctor: Base };", "savedBase.ctor === Base"],
      [
        "subclass",
        `var Earlier = class extends Base { note() { return this.value - ${n}; } };`,
        `new Earlier(${n + 1}).note()`,
      ],
      [
        "subclass",
        "var Sibling = class extends Base {};",
        "Object.getPrototypeOf(Sibling.prototype) === Base.prototype",
      ],
    ],
    seed >>> 0,
    1 + random.int(4)
  );
  const allocationKinds = ["object", "array", "closure", "class", "reference"];
  const allocations = choose(allocationKinds, seed >>> 0, 1 + ((seed >>> 0) % 4)).map((kind, i) => {
    const name = `local${i}`;
    const value = n + i;
    const [initializer, observation] = {
      object: [`{ inner: { value: ${value} } }`, `${name}.inner.value`],
      array: [`[${value}, { value: ${value + 1} }]`, `${name}[1].value`],
      closure: [`() => calibration + ${value}`, `${name}()`],
      class: [`class { read() { return ${value}; } }`, `new ${name}().read()`],
      reference: ["Base", `${name} === Base`],
    }[kind];
    const declaration = (seed + i) % 2 === 0 ? "let" : "var";
    return [kind, `${declaration} ${name} = ${initializer};`, observation];
  });
  const coverage = {
    baseMembers: base.map(([kind]) => kind),
    pivotMembers: members.map(([kind]) => kind),
    prefix: between.map(([kind]) => kind),
    crossed: allocations.map(([kind]) => kind),
    block: [block ? "block" : "straight"],
    width: [allocations.length],
  };
  const calibration = n + (block ? 2 : 0);
  const setterOffset = base.some(([kind]) => kind === "accessor") ? n : 0;
  const constructedValue = n + 3 + (block ? calibration : 0) + setterOffset;
  const observedPrefix = [calibration, constructedValue, -n + setterOffset, true, true].join(" ");
  const mutations = ["static base", "superclass alias", "named pivot", "superclass write", "crossed assignment"];
  const render = (mutation) => {
    const setup = [`var calibration = ${n};`];
    if (block) setup.push("{ const offset = 2; calibration += offset; }");
    const baseSyntax = base.map(([, syntax]) => syntax);
    if (mutation === "static base") baseSyntax.push("static extra() { return 0; }");
    setup.push(`var Base = class { ${constructor} ${baseSyntax.join(" ")} };`);
    if (mutation === "superclass write") setup.push("Base = Base;");
    setup.push(...between.map(([, source]) => source));
    if (mutation === "superclass alias") setup.push("var Alias = Base;");
    const pivot = setup.length;
    const self = mutation === "named pivot" ? " NamedPivot" : "";
    const parent = mutation === "superclass alias" ? "Alias" : "Base";
    const subclass = `var Pivot = class${self} extends ${parent} { ${members.map(([, syntax]) => syntax).join(" ")} };`;
    const crossed = allocations.map(([, source]) => source);
    if (mutation === "crossed assignment") crossed.push("var assigned;", `assigned = ${n};`);
    const observations = [
      "calibration",
      "sample.value",
      `new Pivot(${-n}).value`,
      "Object.getPrototypeOf(Pivot) === Base",
      "Object.getPrototypeOf(Pivot.prototype) === Base.prototype",
      ...[...base, ...members, ...between, ...allocations].map(([, , observation]) => observation),
    ];
    if (mutation === "crossed assignment") observations.push("assigned");
    const suffix = [`var sample = new Pivot(${n + 3});`];
    if (members.some(([kind]) => kind === "accessor")) suffix.push(`sample.savedValue = ${n + 4};`);
    suffix.push(`console.log(${observations.join(", ")});`);
    const crossedIds = crossed.map((_, i) => pivot + i + 1);
    return {
      name: `heritage seed ${seed}${mutation ? ` with ${mutation}` : ""}`,
      source: [...setup, subclass, ...crossed, ...suffix].join("\n"),
      heritageObservedPrefix: observedPrefix,
      spec: {
        entry: "entry.mjs",
        modules: {
          "prefix.mjs": [...setup.map((_, i) => i), ...crossedIds].map((id) => `#${id}`),
          "entry.mjs": [pivot, ...suffix.map((_, i) => pivot + crossed.length + i + 1)].map((id) => `#${id}`),
        },
      },
      ...(mutation
        ? { uncertifiedHeritage: true, heritageMutation: mutation }
        : { certifiedHeritage: { pivot, crossed: crossedIds }, heritageCoverage: coverage }),
    };
  };
  const mutationIndex = ((seed % mutations.length) + mutations.length) % mutations.length;
  return [render(null), render(mutations[mutationIndex])];
}
