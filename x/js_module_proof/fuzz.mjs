import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { generateBindings, validateBindings } from "./bindings.mjs";

// Construct ESTree nodes first, then print the subset of JavaScript used here.
// Adding grammar productions does not require changing the splitter's parser.
const id = (name) => ({ type: "Identifier", name });
const literal = (value) => ({ type: "Literal", value });
const unicodeThrow = "café 🙂\n\t\0\ud800!\udc00";
const expr = (expression) => ({ type: "ExpressionStatement", expression });
const call = (callee, args = []) => ({ type: "CallExpression", callee, arguments: args });
const variable = (name, value, kind = "let") => ({
  type: "VariableDeclaration",
  kind,
  declarations: [{ type: "VariableDeclarator", id: id(name), init: literal(value) }],
});
const fn = (name, body) => ({
  type: "FunctionDeclaration",
  id: id(name),
  params: [],
  body: { type: "BlockStatement", body: [body] },
});
const add = (target, source) =>
  expr({ type: "AssignmentExpression", operator: "+=", left: id(target), right: id(source) });
const reader = (name) => ({ type: "ReturnStatement", argument: id(name) });
const record = (name) =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [id(name)])
  );
const recordLiteral = (value) =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [
      literal(value),
    ])
  );
const recordUndefined = () =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [
      { type: "UnaryExpression", operator: "void", prefix: true, argument: literal(0) },
    ])
  );
const recordNegative = (value) =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [
      { type: "UnaryExpression", operator: "-", prefix: true, argument: literal(value) },
    ])
  );
const recordSpecialNumber = (numerator) =>
  expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [
      {
        type: "BinaryExpression",
        operator: "/",
        left:
          numerator < 0
            ? { type: "UnaryExpression", operator: "-", prefix: true, argument: literal(1) }
            : literal(numerator),
        right: literal(0),
      },
    ])
  );
const recordBigInteger = (magnitude, negative = false) => {
  const value = { type: "Literal", value: null, bigint: magnitude };
  const argument = negative ? { type: "UnaryExpression", operator: "-", prefix: true, argument: value } : value;
  return expr(
    call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [argument])
  );
};

function print(node) {
  switch (node.type) {
    case "Program":
      return node.body.map(print).join("\n") + "\n";
    case "Identifier":
      return node.name;
    case "Literal":
      return node.bigint === undefined ? JSON.stringify(node.value) : `${node.bigint}n`;
    case "UnaryExpression":
      return `${node.operator}${node.operator === "!" ? "" : " "}${print(node.argument)}`;
    case "VariableDeclaration":
      return `${node.kind} ${node.declarations.map(print).join(", ")};`;
    case "VariableDeclarator":
      return `${print(node.id)} = ${print(node.init)}`;
    case "FunctionDeclaration":
      return `${node.async ? "async " : ""}function ${print(node.id)}(${node.params.map(print).join(", ")}) ${print(node.body)}`;
    case "FunctionExpression":
      return `function (${node.params.map(print).join(", ")}) ${print(node.body)}`;
    case "BlockStatement":
      return `{ ${node.body.map(print).join(" ")} }`;
    case "ReturnStatement":
      return `return ${print(node.argument)};`;
    case "ThrowStatement":
      return `throw ${print(node.argument)};`;
    case "ExpressionStatement":
      return `${print(node.expression)};`;
    case "AssignmentExpression":
      return `${print(node.left)} ${node.operator} ${print(node.right)}`;
    case "BinaryExpression":
    case "LogicalExpression":
      return `(${print(node.left)} ${node.operator} ${print(node.right)})`;
    case "ConditionalExpression":
      return `(${print(node.test)} ? ${print(node.consequent)} : ${print(node.alternate)})`;
    case "CallExpression":
      return `${print(node.callee)}(${node.arguments.map(print).join(", ")})`;
    case "MemberExpression":
      return node.computed
        ? `${print(node.object)}[${print(node.property)}]`
        : `${print(node.object)}.${print(node.property)}`;
    case "ObjectExpression":
      return `{ ${node.properties.map(print).join(", ")} }`;
    case "Property":
      return `${print(node.key)}: ${print(node.value)}`;
    case "ArrayExpression":
      return `[${node.elements.map(print).join(", ")}]`;
    case "ExportNamedDeclaration":
      return `export { ${node.specifiers.map(print).join(", ")} };`;
    case "ExportSpecifier":
      return print(node.local) + (node.local.name === node.exported.name ? "" : ` as ${print(node.exported)}`);
    case "IfStatement":
      return `if (${print(node.test)}) ${print(node.consequent)}${node.alternate ? ` else ${print(node.alternate)}` : ""}`;
    case "TryStatement":
      return `try ${print(node.block)}${node.handler ? ` catch${node.handler.param ? ` (${print(node.handler.param)})` : ""} ${print(node.handler.body)}` : ""}${node.finalizer ? ` finally ${print(node.finalizer)}` : ""}`;
    case "ForStatement":
      return `for (${print(node.init).slice(0, -1)}; ${print(node.test)}; ${print(node.update)}) ${print(node.body)}`;
    case "UpdateExpression":
      return node.prefix ? `${node.operator}${print(node.argument)}` : `${print(node.argument)}${node.operator}`;
    case "AwaitExpression":
      return `await ${print(node.argument)}`;
    case "EmptyStatement":
      return ";";
    case "ArrowFunctionExpression":
      return `(${node.params.map(print).join(", ")}) => ${print(node.body)}`;
    default:
      throw new Error(`unsupported generated AST node: ${node.type}`);
  }
}

function random(seed, index) {
  let state = (seed ^ Math.imul(index + 1, 0x9e3779b9)) >>> 0;
  return (limit) => {
    state = (state + 0x6d2b79f5) >>> 0;
    let n = state;
    n = Math.imul(n ^ (n >>> 15), n | 1);
    n ^= n + Math.imul(n ^ (n >>> 7), n | 61);
    return ((n ^ (n >>> 14)) >>> 0) % limit;
  };
}
function shuffle(items, rand) {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = rand(i + 1);
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// Each lexical scope records only its own declarations. Looking up visible
// names from inner to outer makes shadowed bindings unavailable to expressions.
class Scope {
  constructor(parent = null) {
    this.parent = parent;
    this.names = new Set();
  }
  declare(name) {
    if (this.names.has(name)) throw new Error(`duplicate generated binding: ${name}`);
    this.names.add(name);
  }
  visible() {
    return [...new Set([...this.names, ...(this.parent?.visible() ?? [])])];
  }
}
const declaration = (name, init) => ({
  type: "VariableDeclaration",
  kind: "let",
  declarations: [{ type: "VariableDeclarator", id: id(name), init }],
});
const functionNode = (name, params, body) => ({
  type: "FunctionDeclaration",
  id: id(name),
  params: params.map(id),
  body: { type: "BlockStatement", body },
});
const returnNode = (argument) => ({ type: "ReturnStatement", argument });

function closureCase(seed, index) {
  const rand = random(seed ^ 0xc105ed, index);
  const depth = 1 + rand(4);
  const writesCaptured = rand(2) === 0;
  const controlFlow = index % 8 === 7;
  const usesFinally = index % 16 === 7;
  let shadowed = false;
  const global = new Scope();
  for (const name of ["base", "make", "saved", "run"]) global.declare(name);
  const factoryScope = new Scope(global);
  factoryScope.declare("captured");
  const expression = (scope, remaining = 0) => {
    const names = scope.visible().filter((name) => !["make", "saved", "run"].includes(name));
    if (remaining && rand(2)) {
      return {
        type: "BinaryExpression",
        operator: ["+", "-", "*"][rand(3)],
        left: expression(scope, remaining - 1),
        right: literal(1 + rand(2)),
      };
    }
    return rand(4) ? id(names[rand(names.length)]) : literal(rand(8));
  };
  const buildLevel = (level, parent) => {
    const scope = new Scope(parent);
    const paramCount = 1 + rand(2);
    const params = Array.from({ length: paramCount }, (_, i) => `arg${level}_${i}`);
    for (const param of params) scope.declare(param);
    let local = `local${level}`;
    // Shadow a parent parameter without referring to it in the initializer.
    if (level > 0 && rand(2)) {
      local = `arg${level - 1}_0`;
      shadowed = true;
    }
    const init = { type: "BinaryExpression", operator: "+", left: id(params[0]), right: id("captured") };
    scope.declare(local);
    const body = [declaration(local, init)];
    if (writesCaptured && level === depth - 1) body.push(add("captured", local));
    if (usesFinally && level === depth - 1) {
      body.push({
        type: "TryStatement",
        block: { type: "BlockStatement", body: [add("captured", local)] },
        handler: null,
        finalizer: { type: "BlockStatement", body: [declaration("marker", literal(103)), record("marker")] },
      });
    }
    if (controlFlow && level === depth - 1) {
      const branch = (value, marker) => {
        const block = new Scope(scope);
        const shadow = params[0];
        block.declare(shadow);
        block.declare("marker");
        shadowed = true;
        return {
          type: "BlockStatement",
          body: [
            declaration(shadow, literal(value)),
            declaration("marker", literal(marker)),
            record("marker"),
            add("captured", shadow),
          ],
        };
      };
      body.push({
        type: "IfStatement",
        test: { type: "BinaryExpression", operator: ">", left: id(params[0]), right: id("captured") },
        consequent: branch(1, 101),
        alternate: branch(2, 102),
      });
      const loopScope = new Scope(scope);
      loopScope.declare("step");
      body.push({
        type: "ForStatement",
        init: declaration("step", literal(0)),
        test: { type: "BinaryExpression", operator: "<", left: id("step"), right: literal(1 + rand(3)) },
        update: { type: "UpdateExpression", operator: "++", prefix: false, argument: id("step") },
        body: { type: "BlockStatement", body: [record("step"), add("captured", "step")] },
      });
    }
    if (level + 1 < depth) {
      const child = buildLevel(level + 1, scope);
      body.push(
        child.node,
        returnNode(
          call(
            id(child.name),
            child.params.map(() => expression(scope, 2))
          )
        )
      );
    } else {
      body.push(
        returnNode({ type: "BinaryExpression", operator: "+", left: id("captured"), right: expression(scope, 2) })
      );
    }
    const name = `nested${level}`;
    return { name, params, node: functionNode(name, params, body) };
  };
  const nested = buildLevel(0, factoryScope);
  const make = functionNode(
    "make",
    [],
    [declaration("captured", literal(1 + rand(8))), nested.node, returnNode(id(nested.name))]
  );
  const run = functionNode(
    "run",
    [],
    [
      returnNode(
        call(
          id("saved"),
          nested.params.map(() => literal(1 + rand(5)))
        )
      ),
    ]
  );
  const statements = shuffle([declaration("base", literal(1 + rand(8))), make], rand);
  statements.push(declaration("saved", call(id("make"))), run, record("base"));
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      ...statements,
      {
        type: "ExportNamedDeclaration",
        specifiers: [{ type: "ExportSpecifier", local: id("run"), exported: id("run") }],
      },
    ],
  };
  const names = Array.from({ length: 1 + rand(3) }, (_, i) => `part${i}.mjs`);
  const entry = "entry.mjs";
  const modules = Object.fromEntries([...names, entry].map((name) => [name, { imports: [], statements: [] }]));
  modules[entry].imports = shuffle(names, rand);
  // Keep a regular stream of valid cross-module assignments while retaining
  // independent, often invalid random partitions for the other programs.
  statements.forEach((_, i) => {
    const owner =
      index % 16 === 11 ? (i === statements.length - 2 ? entry : names[0]) : [...names, entry][rand(names.length + 1)];
    modules[owner].statements.push(i);
  });
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry, modules },
    calls: Array.from({ length: 2 + rand(4) }, () => "run"),
    grammar: "closure",
    depth,
    shadowed,
    writesCaptured: writesCaptured || controlFlow || usesFinally,
    controlFlow,
    usesFinally,
    unsupported: true,
    cyclic: false,
    invalidAssignment: false,
  };
}

// The proof-supported closure grammar is generated separately from the wider
// closure grammar. Assignments vary the owners and evaluation order.
function modeledClosureCase(seed, index) {
  const rand = random(seed ^ 0xced, index);
  const names = Array.from({ length: 1 + rand(4) }, (_, i) => `unit${i}.mjs`);
  const observer = "observer.mjs",
    entry = "entry.mjs";
  const modules = Object.fromEntries(
    [...names, observer, entry].map((name) => [name, { imports: [], statements: [] }])
  );
  const owners = [0];
  for (let i = 1; i < 4; i++) owners.push(owners[i - 1] + rand(names.length - owners[i - 1]));
  const [seedOwner, factoryOwner, closureOwner, readerOwner] = owners.map((i) => names[i]);
  const dep = (owner, target) => {
    if (owner !== target && !modules[owner].imports.includes(target)) modules[owner].imports.push(target);
  };
  dep(factoryOwner, seedOwner);
  dep(closureOwner, factoryOwner);
  dep(readerOwner, closureOwner);
  dep(readerOwner, seedOwner);
  dep(observer, readerOwner);
  modules[entry].imports = shuffle([...names, observer], rand);
  const factory = functionNode(
    "make",
    [],
    [
      declaration("captured", id("seed")),
      returnNode({
        type: "ArrowFunctionExpression",
        params: [],
        async: false,
        body: { type: "UpdateExpression", operator: "++", prefix: true, argument: id("captured") },
      }),
    ]
  );
  const seedDecl = declaration("seed", literal(1 + rand(8)));
  const closureDecl = declaration("next", call(id("make")));
  closureDecl.kind = "const";
  const reader = functionNode("read", [], [returnNode(call(id("next")))]);
  const choose = functionNode(
    "choose",
    ["flag"],
    [
      {
        type: "IfStatement",
        test: id("flag"),
        consequent: {
          type: "BlockStatement",
          body: [returnNode({ type: "BinaryExpression", operator: "+", left: id("seed"), right: id("flag") })],
        },
        alternate: { type: "BlockStatement", body: [returnNode(literal(1))] },
      },
    ]
  );
  const first = shuffle(
    [
      [seedDecl, seedOwner],
      [factory, factoryOwner],
    ],
    rand
  );
  const unsafe = index % 32 === 19;
  const expectThrow = !unsafe;
  const thrownValue = 1 + rand(8);
  const items = [
    ...first,
    [closureDecl, closureOwner],
    [reader, readerOwner],
    [choose, readerOwner],
    ...(expectThrow
      ? [[functionNode("fail", [], [{ type: "ThrowStatement", argument: literal(thrownValue) }]), readerOwner]]
      : []),
    [expr(call(id("read"))), unsafe ? entry : readerOwner],
    [
      expr(
        call({ type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false }, [
          call(id("read")),
        ])
      ),
      observer,
    ],
  ];
  if (unsafe) dep(entry, readerOwner);
  items.forEach(([, owner], i) => modules[owner].statements.push(i));
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      ...items.map(([node]) => node),
      {
        type: "ExportNamedDeclaration",
        specifiers: ["read", "choose", ...(expectThrow ? ["fail"] : [])].map((name) => ({
          type: "ExportSpecifier",
          local: id(name),
          exported: id(name),
        })),
      },
    ],
  };
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry, modules },
    calls: [
      { name: "choose", args: [0] },
      { name: "choose", args: [1] },
      "read",
      ...(expectThrow ? ["fail", "read"] : ["read"]),
    ],
    expectThrow,
    thrownValue,
    grammar: "modeledClosure",
    crossModuleCapture: seedOwner !== factoryOwner,
    expectUnsafe: unsafe,
    cyclic: false,
    invalidAssignment: false,
    unsupported: false,
  };
}

// Generate a parameterized expression independently of the validator. The
// caller and captured module binding are placed in separate real modules.
function parameterCase(seed, index) {
  const rand = random(seed ^ 0x70617261, index);
  const initial = 1 + rand(8);
  const parameters = index % 64 === 12 ? ["base", "y"] : ["x", "y"];
  const thrownValue = ["generated", false, null, 1 + rand(8)][Math.floor(index / 32) % 4];
  const firstArgument = 1 + rand(8),
    secondArgument = 1 + rand(8),
    condition = 1 + rand(8);
  // Construct a bounded tree from currently visible numeric parameters and
  // literals. The associated value only chooses a bounded expected throw for
  // this test input; Node remains the differential oracle for both programs.
  const tree = (depth) => {
    if (depth === 0 || rand(4) === 0) {
      const choice = rand(3);
      if (choice === 0) return { ast: id(parameters[0]), value: secondArgument, depth: 0 };
      if (choice === 1) return { ast: id(parameters[1]), value: condition, depth: 0 };
      const value = rand(5);
      return { ast: literal(value), value, depth: 0 };
    }
    const left = tree(depth - 1),
      right = tree(depth - 1),
      operator = rand(2) ? "+" : "*";
    return {
      ast: { type: "BinaryExpression", operator, left: left.ast, right: right.ast },
      value: operator === "+" ? left.value + right.value : left.value * right.value,
      depth: 1 + Math.max(left.depth, right.depth),
    };
  };
  const generatedExpression = tree(2);
  const value = call(id("helper"), [generatedExpression.ast]);
  const observedThrow = typeof thrownValue === "number" ? generatedExpression.value + initial + condition : thrownValue;
  const expectedThrown =
    observedThrow === null ? { kind: "null" } : { kind: typeof observedThrow, value: observedThrow };
  const guard = parameters[0] === "x";
  const fallback = [
    declaration("base", literal(2)),
    expr({ type: "UpdateExpression", operator: "--", prefix: false, argument: id("base") }),
    returnNode({
      type: "BinaryExpression",
      operator: "-",
      left: { type: "BinaryExpression", operator: "+", left: id("base"), right: id("y") },
      right: literal(1),
    }),
  ];
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      variable("base", initial),
      functionNode(
        "helper",
        ["value"],
        [returnNode({ type: "BinaryExpression", operator: "+", left: id("value"), right: id("base") })]
      ),
      functionNode("calculate", parameters, [
        {
          type: "IfStatement",
          test: guard
            ? {
                type: "LogicalExpression",
                operator: "&&",
                left:
                  Math.floor(index / 64) % 5 === 0
                    ? { type: "BinaryExpression", operator: "<", left: literal(0), right: id("y") }
                    : Math.floor(index / 64) % 5 === 1
                      ? { type: "BinaryExpression", operator: "!==", left: id("y"), right: literal(0) }
                      : Math.floor(index / 64) % 5 === 2
                        ? { type: "BinaryExpression", operator: ">=", left: id("y"), right: literal(1) }
                        : Math.floor(index / 64) % 5 === 3
                          ? { type: "BinaryExpression", operator: ">", left: id("y"), right: literal(0) }
                          : { type: "BinaryExpression", operator: "<=", left: literal(1), right: id("y") },
                right: literal(true),
              }
            : id("y"),
          consequent: {
            type: "BlockStatement",
            body: [
              declaration("temp", value),
              add("temp", "y"),
              { type: "ThrowStatement", argument: typeof thrownValue === "number" ? id("temp") : literal(thrownValue) },
            ],
          },
          alternate: guard ? null : { type: "BlockStatement", body: fallback },
        },
        ...(guard ? fallback : []),
      ]),
      {
        type: "ExportNamedDeclaration",
        specifiers: [{ type: "ExportSpecifier", local: id("calculate"), exported: id("calculate") }],
      },
    ],
  };
  const helperInState = rand(2) === 0;
  const modules = {
    "state.mjs": { imports: [], statements: helperInState ? [0, 1] : [0] },
    "logic.mjs": { imports: ["state.mjs"], statements: helperInState ? [2] : [1, 2] },
    "entry.mjs": { imports: ["logic.mjs"], statements: [] },
  };
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry: "entry.mjs", modules },
    calls: [
      { name: "calculate", args: [firstArgument, 0] },
      { name: "calculate", args: [secondArgument, condition] },
    ],
    grammar: "parameter",
    expressionDepth: generatedExpression.depth,
    expectThrow: true,
    thrownValue,
    expectedThrown,
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
  };
}

// Vary captured initial values, calls and placement while constructing the
// program independently as ESTree. Each factory call allocates fresh cells.
function capturedCase(seed, index) {
  const rand = random(seed ^ 0xcafe, index);
  const count = 1 + rand(5);
  const unsafe = index % 128 === 92;
  const target = unsafe || rand(2) === 0 ? "value" : "start";
  const closureForm = Math.floor(index / 128) % 4;
  const closureBody = [add(target, "step"), returnNode(id(target))];
  const expressionClosure = {
    type: closureForm === 3 ? "FunctionExpression" : "ArrowFunctionExpression",
    id: null,
    params: [id("step")],
    async: false,
    generator: false,
    body: { type: "BlockStatement", body: closureBody },
  };
  const factory = functionNode(
    "make",
    ["start"],
    [
      declaration("value", { type: "BinaryExpression", operator: "+", left: id("start"), right: id("seed") }),
      ...(closureForm === 1
        ? [functionNode("increment", ["step"], closureBody), returnNode(id("increment"))]
        : closureForm >= 2
          ? [{ ...declaration("increment", expressionClosure), kind: "const" }, returnNode(id("increment"))]
          : [returnNode(expressionClosure)]),
    ]
  );
  const closures = Array.from({ length: count }, (_, i) => {
    const node = declaration(`closure${i}`, call(id("make"), [literal(1 + rand(8))]));
    node.kind = "const";
    return node;
  });
  const alias = declaration("alias", id("closure0"));
  alias.kind = "const";
  const alias2 = declaration("alias2", id("alias"));
  alias2.kind = "const";
  const definitions = [...closures, alias, alias2];
  const names = [...closures.map((_, i) => `closure${i}`), "alias", "alias2"];
  const renamePublic = index % 256 === 28;
  const publicNames = names.map((name) => (renamePublic && name === "closure0" ? "public_closure0" : name));
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      variable("seed", 1 + rand(8)),
      ...(unsafe ? [variable("amount", 1 + rand(3)), fn("bump", add("seed", "amount"))] : []),
      factory,
      ...(unsafe ? [expr(call(id("bump")))] : []),
      ...definitions,
      {
        type: "ExportNamedDeclaration",
        specifiers: [
          ...names.map((name, i) => ({ type: "ExportSpecifier", local: id(name), exported: id(publicNames[i]) })),
          ...(renamePublic
            ? [{ type: "ExportSpecifier", local: id("closure0"), exported: id("second_closure0") }]
            : []),
        ],
      },
    ],
  };
  const combined = rand(2) === 0;
  const modules = unsafe
    ? {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: definitions.map((_, i) => i + 5) },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      }
    : combined
      ? {
          "state.mjs": { imports: [], statements: [0, 1, ...definitions.map((_, i) => i + 2)] },
          "entry.mjs": { imports: ["state.mjs"], statements: [] },
        }
      : {
          "state.mjs": { imports: [], statements: [0, 1] },
          "closures.mjs": { imports: ["state.mjs"], statements: definitions.map((_, i) => i + 2) },
          "entry.mjs": { imports: ["closures.mjs"], statements: [] },
        };
  const callableNames = renamePublic ? [...publicNames, "second_closure0"] : publicNames;
  const calls = [...callableNames, ...callableNames.slice().reverse()].map((name) => ({ name, args: [1 + rand(5)] }));
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry: "entry.mjs", modules },
    calls,
    grammar: "captured",
    captureTarget: target,
    expectUnsafe: unsafe,
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
  };
}

// Allocation, aliasing and indexed access are generated independently of the
// proof frontend. These programs run in Node even when splitting is refused.

function throwExpressions(variant) {
  const negative = (value) => ({ type: "UnaryExpression", operator: "-", prefix: true, argument: literal(value) });
  const division = (left) => ({ type: "BinaryExpression", operator: "/", left, right: literal(0) });
  switch (variant) {
    case "bigint":
      return [
        { type: "Literal", value: null, bigint: "12345678901234567890" },
        { type: "Literal", value: null, bigint: "12345678901234567891" },
      ];
    case "null":
      return [literal(null), { type: "UnaryExpression", operator: "void", prefix: true, argument: literal(0) }];
    case "unicode":
      return [literal(unicodeThrow), literal(unicodeThrow.replace("\ud800", "\ud801"))];
    case "negative":
      return [negative(7), negative(8)];
    case "negativeZero":
      return [negative(0), literal(0)];
    case "nan":
      return [division(literal(0)), literal(null)];
    case "positiveInfinity":
      return [division(literal(1)), division(negative(1))];
    case "negativeInfinity":
      return [division(negative(1)), division(literal(1))];
    default:
      return [literal("generated-left"), literal("generated-right")];
  }
}

function objectCase(seed, index) {
  const rand = random(seed ^ 0x0b1ec7, index);
  const property = `field${rand(8)}`;
  const arraySize = 2 + rand(4);
  const values = shuffle(
    Array.from({ length: 8 }, (_, i) => i + 1),
    rand
  ).slice(0, arraySize);
  const firstIndex = rand(arraySize);
  const secondIndex = (firstIndex + 1 + rand(arraySize - 1)) % arraySize;
  const combined = rand(2) === 0;
  const computedProperty = rand(2) === 0;
  const mutationCount = 1 + rand(3);
  const member = (object, key, computed = false) => ({
    type: "MemberExpression",
    object: id(object),
    property: computed ? literal(key) : id(key),
    computed,
  });
  const addProperty = (element) =>
    expr({
      type: "AssignmentExpression",
      operator: "+=",
      left: member("alias", property, computedProperty),
      right: member("values", element, true),
    });
  const immutable = (name, init) => {
    const node = declaration(name, init);
    node.kind = "const";
    return node;
  };
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      variable("bias", 1 + rand(8), "const"),
      functionNode(
        "read",
        ["flag"],
        [
          immutable("box", {
            type: "ObjectExpression",
            properties: [
              {
                type: "Property",
                key: id(property),
                value: literal(1 + rand(8)),
                kind: "init",
                method: false,
                shorthand: false,
                computed: false,
              },
            ],
          }),
          immutable("alias", id("box")),
          immutable("values", { type: "ArrayExpression", elements: values.map(literal) }),
          immutable("valuesAlias", id("values")),
          expr({
            type: "AssignmentExpression",
            operator: "+=",
            left: member("valuesAlias", firstIndex, true),
            right: literal(8),
          }),
          ...Array.from({ length: mutationCount }, () => ({
            type: "IfStatement",
            test: id("flag"),
            consequent: { type: "BlockStatement", body: [addProperty(firstIndex)] },
            alternate: { type: "BlockStatement", body: [addProperty(secondIndex)] },
          })),
          ...(rand(2)
            ? [
                {
                  type: "IfStatement",
                  test: id("flag"),
                  consequent: { type: "BlockStatement", body: [addProperty(firstIndex)] },
                  alternate: null,
                },
              ]
            : []),
          returnNode({
            type: "BinaryExpression",
            operator: "+",
            left: member("box", property, computedProperty),
            right: id("bias"),
          }),
        ]
      ),
      {
        type: "ExportNamedDeclaration",
        specifiers: [{ type: "ExportSpecifier", local: id("read"), exported: id("read") }],
      },
    ],
  };
  return {
    ast,
    source: print(ast),
    spec: {
      kind: "bindings",
      entry: "entry.mjs",
      modules: combined
        ? { "entry.mjs": { imports: [], statements: [0, 1] } }
        : {
            "state.mjs": { imports: [], statements: [0] },
            "logic.mjs": { imports: ["state.mjs"], statements: [1] },
            "entry.mjs": { imports: ["logic.mjs"], statements: [] },
          },
    },
    calls: [
      { name: "read", args: [0] },
      { name: "read", args: [1] },
    ],
    grammar: "object",
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
  };
}

// Generate declarations and effects independently of their module placement.
// Mode changes how effects are distributed, not the program grammar.
export function makeCase(seed, index) {
  if (index % 64 === 20) return objectCase(seed, index);
  if (index % 64 === 28) return capturedCase(seed, index);
  if (index % 32 === 12) return parameterCase(seed, index);
  if (index % 16 === 3) return modeledClosureCase(seed, index);
  if (index % 4 === 3) return closureCase(seed, index);
  const rand = random(seed, index);
  const mode = index % 4;
  const count = 2 + rand(3);
  const state = "state.mjs";
  const others = Array.from({ length: 1 + rand(3) }, (_, i) => `part${i}.mjs`);
  const entry = "entry.mjs";
  const modules = Object.fromEntries([state, ...others, entry].map((name) => [name, { imports: [], statements: [] }]));
  for (const name of others) modules[name].imports = [state];
  modules[entry].imports = shuffle([state, ...others], rand);
  const cells = Array.from({ length: count }, (_, i) => `cell${i}`);
  const bumps = Array.from({ length: count }, (_, i) => `bump${i}`);
  const getters = Array.from({ length: count }, (_, i) => `get${i}`);
  const ranked = [state, ...others, entry];
  const owners = cells.map(() => (mode === 2 ? rand(ranked.length - 1) : 0));
  const dependency = (owner, target) => {
    if (owner !== target && !modules[owner].imports.includes(target)) modules[owner].imports.push(target);
  };
  const chooseOwner = (minimum) => (mode === 2 ? minimum + rand(ranked.length - minimum) : 1 + rand(others.length));
  const declarations = [];
  const useConstant = index % 16 === 0;
  for (let i = 0; i < count; i++) {
    const owner = ranked[owners[i]];
    const candidates = cells.map((_, j) => j).filter((j) => owners[j] <= owners[i]);
    const source = candidates[rand(candidates.length)];
    dependency(owner, ranked[owners[source]]);
    const readerOwner = ranked[chooseOwner(owners[i])];
    dependency(readerOwner, owner);
    declarations.push([variable(cells[i], 1 + rand(8)), owner]);
    declarations.push([fn(bumps[i], add(cells[i], useConstant && i === 0 ? "offset" : cells[source])), owner]);
    declarations.push([fn(getters[i], reader(cells[i])), readerOwner]);
  }
  if (useConstant) declarations.push([variable("offset", 1 + rand(8), "const"), state]);
  const statements = shuffle(declarations, rand);
  const calls = 1 + rand(3);
  const records = 1 + rand(3);
  // A call in entry executes after a record in an imported module. Its source
  // order is the opposite; the selected cell guarantees an observable witness.
  const selected = rand(count);
  const placeCall = (i, owner) => {
    dependency(owner, ranked[owners[i]]);
    statements.push([expr(call(id(bumps[i]))), owner]);
  };
  const placeRecord = (i, owner) => {
    dependency(owner, ranked[owners[i]]);
    statements.push([record(cells[i]), owner]);
  };
  placeCall(selected, mode === 1 ? entry : mode === 2 ? ranked[chooseOwner(owners[selected])] : state);
  for (let i = 1; i < calls; i++) {
    const cell = rand(count);
    placeCall(cell, mode === 2 ? ranked[chooseOwner(owners[cell])] : state);
  }
  placeRecord(selected, mode === 2 ? ranked[chooseOwner(owners[selected])] : others[0]);
  for (let i = 1; i < records; i++) {
    const cell = rand(count);
    placeRecord(cell, mode === 2 ? ranked[chooseOwner(owners[cell])] : others[rand(others.length)]);
  }
  const publicNames = shuffle([...bumps, ...getters], rand);
  const ast = { type: "Program", sourceType: "module", body: statements.map(([node]) => node) };
  ast.body.push({
    type: "ExportNamedDeclaration",
    specifiers: publicNames.map((name) => ({ type: "ExportSpecifier", local: id(name), exported: id(name) })),
  });
  statements.forEach(([, owner], i) => modules[owner].statements.push(i));
  const cyclic = index % 16 === 6;
  const invalidAssignment = index % 16 === 14;
  if (cyclic) modules[state].imports.push(entry);
  if (invalidAssignment) modules[entry].statements.push(0);
  const observations = [];
  for (let i = 0; i < 2 + rand(4); i++) observations.push(getters[rand(count)], bumps[rand(count)]);
  observations.push(...getters);
  return {
    ast,
    source: print(ast),
    spec: { kind: "bindings", entry, modules },
    calls: observations,
    cell0Owner: ranked[owners[0]],
    unsupported: mode === 3,
    cyclic,
    invalidAssignment,
    useConstant,
  };
}

// Assign an already-generated program independently of its grammar or its
// original placement. Imports are chosen separately, so both valid and invalid
// linking arrangements are useful inputs to the real splitter.
export function independentSplit(input, seed, index) {
  const rand = random(seed ^ 0x51f17, index);
  const count = 1 + rand(4);
  const names = Array.from({ length: count }, (_, i) => `independent${i}.mjs`);
  const entry = "entry.mjs";
  const modules = Object.fromEntries([...names, entry].map((name) => [name, { imports: [], statements: [] }]));
  const owners = [...names, entry];
  owners.forEach((name, moduleIndex) => {
    if ((index + moduleIndex) % 5 === 0) modules[name].aliasImports = true;
    if (input.grammar === "alias" && (index + moduleIndex) % 3 === 0) modules[name].renameLocalImports = true;
  });
  const statements = input.ast.body.filter((node) => node.type !== "ExportNamedDeclaration");
  // Include ordered partitions with complete earlier-module imports as well as
  // unconstrained random graphs. This samples plausible splits without deriving
  // dependencies from the split checker being tested.
  const plannedEarlyVarReorder = input.earlyVar && index % 28 === 14;
  const ordered = (index % 8 === 2 || (input.grammar === "alias" && index % 8 === 5)) && !plannedEarlyVarReorder;
  // Sample sorted ranks for alias programs so ordered splits do not tend to
  // place almost every statement in the entry module.
  const orderedRanks =
    ordered && input.grammar === "alias" ? statements.map(() => rand(owners.length)).sort((a, b) => a - b) : null;
  let rank = 0;
  statements.forEach((_, i) => {
    if (orderedRanks) rank = orderedRanks[i];
    else if (ordered) rank += rand(owners.length - rank);
    modules[owners[ordered ? rank : rand(owners.length)]].statements.push(i);
  });
  modules[entry].imports = shuffle(names, rand);
  for (let i = 1; i < names.length; i++)
    for (let j = 0; j < i; j++) if (ordered || rand(2)) modules[names[i]].imports.push(names[j]);
  const cyclic = index % 8 === 1;
  const invalidAssignment = index % 8 === 0 && statements.length > 0;
  if (cyclic) modules[names[0]].imports.push(entry);
  if (invalidAssignment) modules[entry].statements.push(0);
  if (plannedEarlyVarReorder) {
    for (const module of Object.values(modules)) {
      module.imports = [];
      module.statements = [];
    }
    const state = names[0];
    const effect = names[1] ?? entry;
    modules[state].statements = statements.slice(1).map((_, index) => index + 1);
    modules[effect].statements = [0];
    modules[effect].imports = [state];
    if (effect !== entry) modules[entry].imports = [effect, state];
  }
  return {
    ...input,
    spec: { kind: "bindings", entry, modules },
    independentSplit: true,
    plannedEarlyVarReorder,
    ordered,
    cyclic,
    invalidAssignment,
  };
}

// Composes bounded expressions and statement sequences using names visible at
// each point. The program is generated before any optional repartitioning.
export function makeLocalAliasCase(seed, index) {
  const rand = random(seed ^ 0x1a11a5, index);
  const bindings = Array.from({ length: 2 + rand(2) }, (_, i) => `value${i}`);
  const readerNames = Array.from({ length: bindings.length + rand(2) }, (_, i) => `read${i}`);
  const initial = rand(6);
  const updateBindings = index % 4 === 0 ? ["increment", "otherIncrement"] : [];
  const numericBindings = index % 4 === 1 ? ["numericLeft", "numericOther", "numericRight"] : [];
  const state = [
    ...bindings.map((name, i) => variable(name, initial + i)),
    ...updateBindings.map((name, i) => variable(name, initial + i + 1)),
    ...numericBindings.map((name, i) => variable(name, initial + i + 1)),
    ...(index % 2 ? [record(bindings[0])] : []),
  ];
  const observation = index % 4 === 3 ? [record(bindings[0])] : [];
  const localState = index % 4 === 0 ? [variable("total", rand(5))] : [];
  const readers = readerNames.map((name, i) => functionNode(name, [], [returnNode(id(bindings[i % bindings.length]))]));
  const updates = localState.length
    ? [
        functionNode(
          "bump",
          [],
          [expr({ type: "AssignmentExpression", operator: "+=", left: id("total"), right: id("increment") })]
        ),
        functionNode("current", [], [returnNode(id("total"))]),
      ]
    : [];
  const numericExpression = [
    {
      type: "BinaryExpression",
      operator: "+",
      left: { type: "BinaryExpression", operator: "*", left: id("numericLeft"), right: literal(2) },
      right: id("numericRight"),
    },
    { type: "BinaryExpression", operator: "-", left: id("numericRight"), right: id("numericLeft") },
    {
      type: "ConditionalExpression",
      test: { type: "BinaryExpression", operator: "<", left: id("numericLeft"), right: id("numericRight") },
      consequent: { type: "BinaryExpression", operator: "+", left: id("numericLeft"), right: literal(1) },
      alternate: id("numericRight"),
    },
    {
      type: "ConditionalExpression",
      test: { type: "LogicalExpression", operator: "||", left: id("numericLeft"), right: id("numericRight") },
      consequent: { type: "BinaryExpression", operator: "+", left: id("numericLeft"), right: literal(1) },
      alternate: id("numericRight"),
    },
    { type: "LogicalExpression", operator: "||", left: id("numericLeft"), right: id("numericRight") },
  ][Math.floor(index / 12) % 5];
  const numericParameters = index % 8 !== 5 ? [] : index % 16 === 13 ? ["numericOther"] : ["argument", "otherArgument"];
  const numericArguments = numericParameters.map((_, index) => index + 1);
  const numericBody = numericParameters.reduce(
    (body, parameter) => ({ type: "BinaryExpression", operator: "+", left: body, right: id(parameter) }),
    numericExpression
  );
  const numeric = numericBindings.length ? [functionNode("compute", numericParameters, [returnNode(numericBody)])] : [];
  const readerStart = state.length + observation.length + localState.length;
  const updateStart = readerStart + readers.length;
  const numericStart = updateStart + updates.length;
  const relay = index % 3 === 1;
  const splitReaders = index % 3 === 2;
  const provider = relay ? "relay.mjs" : "state.mjs";
  const modules = {
    "state.mjs": { imports: [], statements: state.map((_, i) => i) },
    ...(relay
      ? {
          "relay.mjs": {
            imports: ["state.mjs"],
            statements: [],
            reexports: { "state.mjs": [...bindings, ...updateBindings, ...numericBindings] },
            renameReexports: index % 4 === 1,
          },
        }
      : {}),
    "logic.mjs": {
      imports: [provider],
      statements: [
        ...observation.map((_, i) => state.length + i),
        ...localState.map((_, i) => state.length + observation.length + i),
        ...readers.flatMap((_, i) => (!splitReaders || i % 2 === 0 ? [readerStart + i] : [])),
        ...updates.map((_, i) => updateStart + i),
        ...numeric.map((_, i) => numericStart + i),
      ],
      renameLocalImports: true,
      aliasImports: index % 4 === 3,
    },
    ...(splitReaders
      ? {
          "other-logic.mjs": {
            imports: [provider],
            statements: readers.flatMap((_, i) => (i % 2 ? [readerStart + i] : [])),
            renameLocalImports: true,
          },
        }
      : {}),
    "entry.mjs": { imports: ["logic.mjs", ...(splitReaders ? ["other-logic.mjs"] : [])], statements: [] },
  };
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      ...state,
      ...observation,
      ...localState,
      ...readers,
      ...updates,
      ...numeric,
      {
        type: "ExportNamedDeclaration",
        specifiers: [
          ...readerNames,
          ...(updates.length ? ["bump", "current"] : []),
          ...(numeric.length ? ["compute"] : []),
        ].map((name) => ({
          type: "ExportSpecifier",
          local: id(name),
          exported: id(name),
        })),
      },
    ],
  };
  return {
    ast,
    source: print(ast),
    grammar: "alias",
    numericLeftIndex: bindings.length + updateBindings.length,
    numericArguments,
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
    expectThrow: false,
    spec: {
      kind: "bindings",
      entry: "entry.mjs",
      modules,
    },
    calls: [
      ...[...readerNames, ...(updates.length ? ["current", "bump", "current"] : [])].map((name) => ({
        name,
        args: [],
      })),
      ...(numeric.length ? [{ name: "compute", args: numericArguments }] : []),
    ],
  };
}

export function makeComposedCase(seed, index) {
  const rand = random(seed ^ 0xc04f053, index);
  const globalCount = 1 + rand(4);
  const globals = Array.from({ length: index % 64 === 59 ? Math.max(2, globalCount) : globalCount }, (_, i) =>
    i ? `global${i}` : "global"
  );
  let hasLocalVar = false;
  const makeFunction = (name) => {
    const scope = new Scope();
    scope.declare("parameter");
    for (const name of globals) scope.declare(name);
    const dataPrefix = [];
    if (rand(3) === 0) {
      const immutable = (name, initial) => {
        const result = declaration(name, initial);
        result.kind = "const";
        return result;
      };
      const member = (object, property, computed) => ({
        type: "MemberExpression",
        object: id(object),
        property: computed ? literal(property) : id(property),
        computed,
      });
      dataPrefix.push(
        immutable("box", {
          type: "ObjectExpression",
          properties: [
            {
              type: "Property",
              key: id("value"),
              value: literal(rand(5)),
              kind: "init",
              method: false,
              shorthand: false,
              computed: false,
            },
          ],
        }),
        immutable("alias", id("box")),
        immutable("array", { type: "ArrayExpression", elements: [literal(rand(5)), literal(rand(5))] }),
        expr({
          type: "AssignmentExpression",
          operator: "+=",
          left: member("alias", "value", false),
          right: member("array", rand(2), true),
        }),
        declaration("datum", member("box", "value", false))
      );
      scope.declare("datum");
    }
    const localPrefix = [];
    if (dataPrefix.length === 0 && index % 5 === 0) {
      const initial = declaration("initialVar", literal(rand(4)));
      initial.kind = "var";
      localPrefix.push(initial);
      scope.declare("initialVar");
      hasLocalVar = true;
    }
    const useLocalFunction = dataPrefix.length === 0 && rand(2) === 0;
    if (useLocalFunction) {
      localPrefix.push(
        declaration("current", literal(rand(4))),
        functionNode("nested", ["delta"], [add("current", "delta"), returnNode(id("current"))])
      );
      scope.declare("current");
    }
    let nextLocal = 0;
    const masks = [0, 1, 15, 2147483648, 4294967295, 4294967297];
    const bitmask = (operator) => {
      const choices =
        operator === "&"
          ? masks
          : [">>>", "<<", ">>"].includes(operator)
            ? [0, 1, 2, 32, 33, 4294967297]
            : [0, 1, 15, 4294967296, 4294967297];
      return choices[rand(choices.length)];
    };
    const expression = (visible, depth, forbidden = null) => {
      if (useLocalFunction && depth > 0 && rand(6) === 0) return call(id("nested"), [literal(rand(4))]);
      if (depth === 0 || rand(3) === 0) {
        const names = visible.visible().filter((name) => name !== forbidden);
        if (!names.length) return literal(rand(4));
        return rand(3) ? id(names[rand(names.length)]) : literal(rand(4));
      }
      if (rand(7) === 0) {
        const operator = ["&", "|", "^", ">>>", "<<", ">>"][rand(6)];
        return {
          type: "BinaryExpression",
          operator,
          left: expression(visible, depth - 1, forbidden),
          right: literal(bitmask(operator)),
        };
      }
      if (rand(7) === 0)
        return {
          type: "LogicalExpression",
          operator: rand(2) ? "&&" : "||",
          left: expression(visible, depth - 1, forbidden),
          right: expression(visible, depth - 1, forbidden),
        };
      if (rand(7) === 0)
        return {
          type: "ConditionalExpression",
          test: expression(visible, depth - 1, forbidden),
          consequent: expression(visible, depth - 1, forbidden),
          alternate: expression(visible, depth - 1, forbidden),
        };
      if (rand(6) === 0) {
        const divisor = 1 + rand(4);
        return {
          type: "BinaryExpression",
          operator: "/",
          left: {
            type: "BinaryExpression",
            operator: "*",
            left: expression(visible, depth - 1, forbidden),
            right: literal(divisor),
          },
          right: literal(divisor),
        };
      }
      const modulo = rand(5) === 0;
      return {
        type: "BinaryExpression",
        operator: modulo ? "%" : "+",
        left: expression(visible, depth - 1, forbidden),
        right: modulo ? literal(1 + rand(4)) : expression(visible, depth - 1, forbidden),
      };
    };
    const sequence = (visible, depth) => {
      const body = [];
      const count = 1 + rand(3);
      const mutableNames = [];
      for (let i = 0; i < count; i++) {
        const candidates =
          visible.parent
            ?.visible()
            .filter((name) => !visible.names.has(name) && (!useLocalFunction || name !== "current")) ?? [];
        const name =
          i === 0 && candidates.length && rand(4) === 0 ? candidates[rand(candidates.length)] : `local${nextLocal++}`;
        // A shadowing declaration's own binding is in the TDZ during its initializer.
        const decl = declaration(name, expression(visible, 2, name));
        const mutable = rand(2) === 0;
        decl.kind = mutable ? "let" : "const";
        if (mutable) mutableNames.push(name);
        body.push(decl);
        visible.declare(name);
        if (mutable && rand(2)) {
          const mode = rand(5);
          const bitOperator = ["&", "|", "^", ">>>", "<<", ">>"][rand(6)];
          body.push(
            expr({
              type: "AssignmentExpression",
              operator: mode === 0 ? "%=" : mode === 1 ? `${bitOperator}=` : "+=",
              left: id(name),
              right:
                mode === 0 ? literal(1 + rand(4)) : mode === 1 ? literal(bitmask(bitOperator)) : expression(visible, 1),
            })
          );
        }
        if (mutable && rand(5) === 0) {
          const divisor = 1 + rand(4);
          for (const operator of ["*=", "/="])
            body.push(expr({ type: "AssignmentExpression", operator, left: id(name), right: literal(divisor) }));
        }
      }
      if (mutableNames.length && rand(3) === 0) {
        const index = `loop${nextLocal++}`;
        const target = mutableNames[rand(mutableNames.length)];
        body.push({
          type: "ForStatement",
          init: variable(index, rand(4)),
          test: { type: "BinaryExpression", operator: rand(2) ? "<" : "<=", left: id(index), right: literal(rand(5)) },
          update: rand(2)
            ? { type: "UpdateExpression", operator: "++", prefix: false, argument: id(index) }
            : { type: "AssignmentExpression", operator: "+=", left: id(index), right: literal(1) },
          body: { type: "BlockStatement", body: [add(target, index)] },
        });
      }
      if (mutableNames.length && rand(4) === 0) {
        const target = mutableNames[rand(mutableNames.length)];
        body.push({
          type: "TryStatement",
          block: {
            type: "BlockStatement",
            body: [add(target, target), ...(rand(4) === 0 ? [{ type: "ThrowStatement", argument: literal(7) }] : [])],
          },
          handler: {
            type: "CatchClause",
            param: rand(2) ? id("caught") : null,
            body: { type: "BlockStatement", body: [add(target, target)] },
          },
          finalizer: rand(2) ? { type: "BlockStatement", body: [add(target, target)] } : null,
        });
      }
      let terminal;
      if (depth && rand(2)) {
        const operators = ["<", "<=", ">", ">=", "===", "!==", "==", "!="];
        terminal = {
          type: "IfStatement",
          test: {
            type: "BinaryExpression",
            operator: operators[rand(operators.length)],
            left: expression(visible, 1),
            right: expression(visible, 1),
          },
          consequent: { type: "BlockStatement", body: sequence(new Scope(visible), depth - 1) },
          alternate: { type: "BlockStatement", body: sequence(new Scope(visible), depth - 1) },
        };
      } else if (rand(4) === 0) {
        const thrown = [false, null, "generated", rand(8)][rand(4)];
        terminal = { type: "ThrowStatement", argument: literal(thrown) };
      } else terminal = returnNode(expression(visible, 2));
      if (rand(4) === 0) {
        terminal = {
          type: "TryStatement",
          block: { type: "BlockStatement", body: [terminal] },
          handler: {
            type: "CatchClause",
            param: rand(2) ? id("caught") : null,
            body: { type: "BlockStatement", body: [returnNode(expression(visible, 1))] },
          },
          finalizer: null,
        };
      }
      if (mutableNames.length && rand(4) === 0) {
        const target = mutableNames[rand(mutableNames.length)];
        terminal = {
          type: "TryStatement",
          block: { type: "BlockStatement", body: [terminal] },
          handler: null,
          finalizer: {
            type: "BlockStatement",
            body: [expr({ type: "UpdateExpression", operator: "++", prefix: false, argument: id(target) })],
          },
        };
      }
      body.push(terminal);
      return body;
    };
    return functionNode(name, ["parameter"], [...localPrefix, ...dataPrefix, ...sequence(scope, 2)]);
  };
  const functionNames = Array.from({ length: 1 + rand(3) }, (_, i) => (i ? `calculate${i}` : "calculate"));
  const functions = functionNames.map(makeFunction);
  const throwVariant = [
    "string",
    "bigint",
    "null",
    "unicode",
    "negative",
    "negativeZero",
    "nan",
    "positiveInfinity",
    "negativeInfinity",
  ][Math.floor(index / 64) % 9];
  if (index % 64 === 23) {
    const [thrown] = throwExpressions(throwVariant);
    functionNames.push("fuzzThrower");
    functions.push(
      functionNode(
        "fuzzThrower",
        ["parameter"],
        [
          {
            type: "IfStatement",
            test: id("parameter"),
            consequent: {
              type: "BlockStatement",
              body: [{ type: "ThrowStatement", argument: thrown }],
            },
            alternate: { type: "BlockStatement", body: [returnNode(literal(0))] },
          },
        ]
      )
    );
  }
  const crossModuleReader = index % 512 === 379;
  const overflowIntermediate = index % 1024 === 763;
  const missingArgument = index % 1024 === 571 || index % 1024 === 635;
  const unusedMissingArgument = index % 1024 === 635;
  const redirectedCallee = index % 512 === 443 || index % 512 === 507 || missingArgument;
  const ignoredExtraArgument = index % 512 === 507;
  if (index % 64 === 59) {
    if (redirectedCallee) {
      functionNames.unshift("fuzzRelayOther");
      functions.unshift(
        functionNode(
          "fuzzRelayOther",
          missingArgument ? ["parameter", "missing"] : ignoredExtraArgument ? [] : ["parameter"],
          [
            returnNode(
              missingArgument
                ? {
                    type: "BinaryExpression",
                    operator: "+",
                    left: id("parameter"),
                    right: unusedMissingArgument ? literal(2) : id("missing"),
                  }
                : ignoredExtraArgument
                  ? literal(2)
                  : { type: "BinaryExpression", operator: "+", left: id("parameter"), right: literal(2) }
            ),
          ]
        )
      );
      functionNames.unshift("fuzzRelayHelper");
      functions.unshift(
        functionNode(
          "fuzzRelayHelper",
          ["parameter"],
          [returnNode({ type: "BinaryExpression", operator: "+", left: id("parameter"), right: literal(1) })]
        )
      );
    }
    if (crossModuleReader) {
      functionNames.unshift("fuzzRelayHelper");
      functions.unshift(
        functionNode(
          "fuzzRelayHelper",
          ["parameter"],
          [
            declaration("global1", id("parameter")),
            returnNode({ type: "BinaryExpression", operator: "+", left: id("global"), right: id("global1") }),
          ]
        )
      );
    }
    functionNames.unshift("fuzzRelayRead");
    const initExpressionReader = index % 512 === 315;
    const expressionReader = (index % 128 === 123 && index % 256 !== 251) || redirectedCallee || index % 1024 === 699;
    const body = overflowIntermediate
      ? [
          returnNode({
            type: "BinaryExpression",
            operator: "-",
            left: { type: "BinaryExpression", operator: "+", left: id("global"), right: literal(2) },
            right: id("global"),
          }),
        ]
      : crossModuleReader || redirectedCallee
        ? [returnNode(call(id("fuzzRelayHelper"), [id("parameter")]))]
        : initExpressionReader
          ? [returnNode({ type: "BinaryExpression", operator: "+", left: id("global"), right: literal(1) })]
          : expressionReader
            ? [
                declaration("global1", id("parameter")),
                returnNode({ type: "BinaryExpression", operator: "+", left: id("global"), right: id("global1") }),
              ]
            : [returnNode(id("global"))];
    functions.unshift(functionNode("fuzzRelayRead", expressionReader ? ["parameter"] : [], body));
  }
  const publicFunctionNames = functionNames.map((name, i) =>
    index % 3 === 0 && i === 0 && name !== "fuzzRelayRead" ? `public_${name}` : name
  );
  const extraAlias = index % 6 === 0 ? "second_calculate" : null;
  const earlyVar = index % 14 === 0;
  const hasDiscardedLiteral = index % 13 === 3 && !earlyVar;
  const discardedForms = [
    ...["use strict", true, null, 42].map(literal),
    { type: "UnaryExpression", operator: "void", prefix: true, argument: literal(0) },
    { type: "UnaryExpression", operator: "!", prefix: true, argument: literal(true) },
    { type: "UnaryExpression", operator: "typeof", prefix: true, argument: literal(null) },
    { type: "UnaryExpression", operator: "void", prefix: true, argument: literal("ignored") },
  ];
  const prefix = earlyVar
    ? [record(globals[0])]
    : hasDiscardedLiteral
      ? [
          expr(discardedForms[Math.floor(index / 13) % discardedForms.length]),
          recordLiteral(
            ["start\ud800!\udc00🙂", 'quote "value"', "slash \\ path", "é 🙂\n\t\0", "👩‍💻"][Math.floor(index / 13) % 5]
          ),
          recordLiteral([0, true, false, null][Math.floor(index / 13) % 4]),
          recordUndefined(),
          recordNegative(1 + (index % 7)),
          recordNegative(0),
          recordSpecialNumber(0),
          recordSpecialNumber(1),
          recordSpecialNumber(-1),
          recordBigInteger("0"),
          recordBigInteger("0xff"),
          recordBigInteger("123456789012345678901234567890"),
          recordBigInteger("123456789012345678901234567890", true),
        ]
      : [];
  const observeRelayAtInit = !redirectedCallee && (index % 256 === 187 || index % 256 === 251 || index % 512 === 315);
  const observeRelayViaCall = index % 256 === 251 || index % 512 === 315 || index % 1024 === 699;
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      ...prefix,
      ...globals.map((name, i) =>
        variable(
          name,
          overflowIntermediate && i < 2
            ? Number.MAX_SAFE_INTEGER - 2 * i
            : index % 64 === 59 && i < 2
              ? i + 1
              : rand(4),
          (index + i) % 7 === 0 ? "var" : "let"
        )
      ),
      ...(observeRelayAtInit
        ? [
            observeRelayViaCall
              ? expr(
                  call(
                    { type: "MemberExpression", object: id("globalThis"), property: id("record"), computed: false },
                    [call(id("fuzzRelayRead"), index % 1024 === 699 ? [literal(1)] : [])]
                  )
                )
              : record("global"),
          ]
        : []),
      ...functions,
      {
        type: "ExportNamedDeclaration",
        specifiers: [
          ...functionNames.map((name, i) => ({
            type: "ExportSpecifier",
            local: id(name),
            exported: id(publicFunctionNames[i]),
          })),
          ...(extraAlias ? [{ type: "ExportSpecifier", local: id(functionNames[0]), exported: id(extraAlias) }] : []),
        ],
      },
    ],
  };
  const callNames = extraAlias ? [...publicFunctionNames, extraAlias] : publicFunctionNames;
  return {
    ast,
    source: print(ast),
    grammar: "composed",
    redirectedCallee,
    overflowIntermediate,
    throwVariant,
    hasDiscardedLiteral,
    hasTextObservation: hasDiscardedLiteral,
    hasPrimitiveObservation: hasDiscardedLiteral,
    hasUndefinedObservation: hasDiscardedLiteral,
    hasNegativeObservation: hasDiscardedLiteral,
    hasNegativeZeroObservation: hasDiscardedLiteral,
    hasSpecialNumberObservation: hasDiscardedLiteral,
    hasBigIntegerObservation: hasDiscardedLiteral,
    hasReexportRelay: index % 8 === 3,
    earlyVar,
    hasLocalVar,
    expectThrow: true,
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
    spec: {
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": {
          imports: [],
          statements: [
            ...Array.from({ length: prefix.length + globals.length }, (_, index) => index),
            ...(redirectedCallee ? [prefix.length + globals.length + 1, prefix.length + globals.length + 2] : []),
          ],
        },
        ...(index % 8 === 3
          ? {
              "relay.mjs": {
                imports: ["state.mjs"],
                statements: [],
                reexports: {
                  "state.mjs": [...globals, ...(redirectedCallee ? ["fuzzRelayHelper", "fuzzRelayOther"] : [])],
                },
                renameReexports: index % 32 === 27,
              },
            }
          : {}),
        ...(index % 16 === 11
          ? {
              "relay2.mjs": {
                imports: ["relay.mjs"],
                statements: [],
                reexports: {
                  "relay.mjs": [...globals, ...(redirectedCallee ? ["fuzzRelayHelper", "fuzzRelayOther"] : [])],
                },
                renameReexports: index % 32 === 27,
              },
            }
          : {}),
        ...(crossModuleReader
          ? { "helper.mjs": { imports: ["relay2.mjs"], statements: [prefix.length + globals.length + 1] } }
          : {}),
        "logic.mjs": {
          imports: [
            index % 16 === 11 ? "relay2.mjs" : index % 8 === 3 ? "relay.mjs" : "state.mjs",
            ...(crossModuleReader ? ["helper.mjs"] : []),
          ],
          aliasImports: index % 4 === 0 || index % 32 === 11,
          statements: [
            ...(observeRelayAtInit ? [prefix.length + globals.length] : []),
            ...functionNames.flatMap((_, position) =>
              (crossModuleReader && position === 1) || (redirectedCallee && (position === 1 || position === 2))
                ? []
                : [prefix.length + globals.length + Number(observeRelayAtInit) + position]
            ),
          ],
        },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    },
    calls: Array.from({ length: 1 + rand(4) }, () => ({
      name: callNames[rand(callNames.length)],
      args: [rand(5)],
    })),
  };
}

export function makeAsyncCase(seed, index) {
  const rand = random(seed ^ 0xa51c, index);
  const value = 1 + rand(5);
  const fn = functionNode(
    "calculate",
    ["x"],
    [
      expr({
        type: "AwaitExpression",
        argument: call({ type: "MemberExpression", object: id("Promise"), property: id("resolve"), computed: false }),
      }),
      record("x"),
      {
        type: "IfStatement",
        test: { type: "BinaryExpression", operator: "===", left: id("x"), right: literal(0) },
        consequent: { type: "BlockStatement", body: [{ type: "ThrowStatement", argument: literal(7) }] },
        alternate: null,
      },
      returnNode({ type: "BinaryExpression", operator: "+", left: id("x"), right: literal(1) }),
    ]
  );
  fn.async = true;
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      fn,
      {
        type: "ExportNamedDeclaration",
        specifiers: [{ type: "ExportSpecifier", local: id("calculate"), exported: id("calculate") }],
      },
    ],
  };
  return {
    ast,
    source: print(ast),
    grammar: "async",
    unsupported: true,
    cyclic: false,
    invalidAssignment: false,
    expectThrow: true,
    calls: [
      { name: "calculate", args: [value] },
      { name: "calculate", args: [0] },
    ],
    asyncFirstValue: value,
    spec: { kind: "bindings", entry: "entry.mjs", modules: { "entry.mjs": { imports: [], statements: [0] } } },
  };
}

export function execute(path, calls, { timeoutMs = 10000 } = {}) {
  const code = `
const events=[];
const send=(kind,value)=>process.stdout.write(JSON.stringify({kind,value})+'\\n');
let opaqueObservation=false;
const primitive=v=>{
  if(v===undefined)return {undefined:true};
  if(typeof v==='number' && (Number.isNaN(v)||Object.is(v,-0)||!Number.isFinite(v)))
    return {number:Number.isNaN(v)?'NaN':Object.is(v,-0)?'-0':String(v)};
  if(typeof v==='bigint')return {bigint:String(v)};
  if(v===null||typeof v==='number'||typeof v==='string'||typeof v==='boolean')return v;
  opaqueObservation=true;return {opaque:true};
};
globalThis.record=v=>{const value=primitive(v);events.push(value);send('event',value);};
let phase='import', index=-1, names=[];
const results=[];
const thrownValue=v=>{
  if(v===null)return {kind:'null'};
  if(v===undefined)return {kind:'undefined'};
  if(typeof v==='number')return {kind:'number',value:Number.isNaN(v)?'NaN':Object.is(v,-0)?'-0':Number.isFinite(v)?v:String(v)};
  if(typeof v==='string'||typeof v==='boolean')return {kind:typeof v,value:v};
  if(typeof v==='bigint')return {kind:'bigint',value:String(v)};
  try{if(v instanceof Error)return {kind:'opaque',name:errorName(v)};}catch{return {kind:'opaque'};}
  return {kind:'opaque'};
};
const errorName=e=>{try{const name=e?.name;return typeof name==='string'?name:null;}catch{return null;}};
try{
  const m=await import(${JSON.stringify(pathToFileURL(path).href)});
  names=Object.keys(m);send('exports',names);phase='call';
  for(const call of ${JSON.stringify(calls)}){index++;const name=typeof call==='string'?call:call.name;const args=typeof call==='string'?[]:call.args;const raw=m[name](...args);const v=raw instanceof Promise?await raw:raw;const normalized=primitive(v);const result=v===undefined?normalized:{value:normalized};results.push(result);send('result',result);}
}catch(e){
  const thrown=thrownValue(e);
  send('error',{name:errorName(e),phase,index,thrown});
}
send('complete',opaqueObservation);`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], {
    encoding: "utf8",
    timeout: timeoutMs,
  });
  const observed = { events: [], exports: [], results: [], error: null };
  let complete = false,
    opaque = false,
    invalid = false;
  const lines = child.stdout.split("\n");
  if (lines.at(-1) !== "") lines.pop();
  for (const line of lines) {
    if (!line) continue;
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      invalid = true;
      break;
    }
    if (event.kind === "event") observed.events.push(event.value);
    else if (event.kind === "exports") observed.exports = event.value;
    else if (event.kind === "result") observed.results.push(event.value);
    else if (event.kind === "error") observed.error = event.value;
    else if (event.kind === "complete") {
      complete = true;
      opaque = event.value;
    } else {
      invalid = true;
      break;
    }
  }
  if (child.error?.code === "ETIMEDOUT") return { ...observed, inconclusive: "timeout" };
  if (child.error?.code === "ENOBUFS") return { ...observed, inconclusive: "output limit" };
  if (child.status === 13 && child.stderr.includes("unsettled top-level await"))
    return { ...observed, inconclusive: "no completion" };
  if (child.error || child.status !== 0) throw new Error(`Node oracle failed: ${child.error ?? child.stderr}`);
  if (invalid) return { ...observed, inconclusive: "unexpected output" };
  if (!complete) return { ...observed, inconclusive: "no completion" };
  return opaque ? { ...observed, inconclusive: "opaque observation" } : observed;
}

// Reduce top-level statements, call sequence, and module graph while retaining a
// caller-supplied failure predicate. Candidates are rebuilt from AST and split
// together, so statement indices remain meaningful after removals.
export async function minimizeCase(input, fails) {
  let current = structuredClone(input);
  if (!(await fails(current))) throw new Error("initial case does not satisfy the failure predicate");
  let changed = true;
  while (changed) {
    changed = false;
    const statements = current.ast.body.filter((node) => node.type !== "ExportNamedDeclaration");
    for (let index = 0; index < statements.length; index++) {
      const candidate = structuredClone(current);
      let seen = 0;
      candidate.ast.body = candidate.ast.body.filter(
        (node) => node.type === "ExportNamedDeclaration" || seen++ !== index
      );
      for (const module of Object.values(candidate.spec.modules))
        module.statements = module.statements.filter((i) => i !== index).map((i) => (i > index ? i - 1 : i));
      candidate.source = print(candidate.ast);
      if (await fails(candidate)) {
        current = candidate;
        changed = true;
        break;
      }
    }
    if (changed) continue;
    // Replacing a binary expression by one of its children strictly reduces
    // its AST. The predicate still decides whether the whole case is valid
    // and reproduces the particular failure being minimized.
    const binaryPaths = [];
    const visit = (value, path) => {
      if (!value || typeof value !== "object") return;
      if (value.type === "BinaryExpression") binaryPaths.push(path);
      for (const [key, child] of Object.entries(value)) {
        if (Array.isArray(child)) child.forEach((element, index) => visit(element, [...path, key, index]));
        else if (child && typeof child === "object") visit(child, [...path, key]);
      }
    };
    visit(current.ast, []);
    for (const path of binaryPaths) {
      for (const side of ["left", "right"]) {
        const candidate = structuredClone(current);
        const parent = path.slice(0, -1).reduce((node, key) => node[key], candidate.ast);
        const key = path.at(-1);
        parent[key] = parent[key][side];
        candidate.source = print(candidate.ast);
        if (await fails(candidate)) {
          current = candidate;
          changed = true;
          break;
        }
      }
      if (changed) break;
    }
    if (changed) continue;
    for (let index = 0; index < current.calls.length; index++) {
      const candidate = structuredClone(current);
      candidate.calls.splice(index, 1);
      if (await fails(candidate)) {
        current = candidate;
        changed = true;
        break;
      }
    }
    if (changed) continue;
    for (const name of Object.keys(current.spec.modules)) {
      if (name === current.spec.entry || current.spec.modules[name].statements.length) continue;
      const candidate = structuredClone(current);
      delete candidate.spec.modules[name];
      for (const module of Object.values(candidate.spec.modules)) {
        module.imports = module.imports.filter((dep) => dep !== name);
        if (module.reexports) delete module.reexports[name];
      }
      if (await fails(candidate)) {
        current = candidate;
        changed = true;
        break;
      }
    }
    if (changed) continue;
    for (const [name, module] of Object.entries(current.spec.modules)) {
      for (let index = 0; index < module.imports.length; index++) {
        const candidate = structuredClone(current);
        const [removed] = candidate.spec.modules[name].imports.splice(index, 1);
        if (candidate.spec.modules[name].reexports) delete candidate.spec.modules[name].reexports[removed];
        if (await fails(candidate)) {
          current = candidate;
          changed = true;
          break;
        }
      }
      if (changed) break;
    }
    if (changed) continue;
    for (const [name, module] of Object.entries(current.spec.modules)) {
      for (const [dependency, bindings] of Object.entries(module.reexports ?? {})) {
        for (let index = 0; index < bindings.length; index++) {
          const candidate = structuredClone(current);
          const remaining = candidate.spec.modules[name].reexports[dependency];
          remaining.splice(index, 1);
          if (!remaining.length) delete candidate.spec.modules[name].reexports[dependency];
          if (await fails(candidate)) {
            current = candidate;
            changed = true;
            break;
          }
        }
        if (changed) break;
      }
      if (changed) break;
    }
  }
  return current;
}

async function mutateOutput(out, input, kind) {
  const modules = Object.keys(input.spec.modules);
  const files = [
    "localAliasRedirect",
    "localAliasInitRedirect",
    "localAliasUpdateRedirect",
    "localAliasNumericRedirect",
  ].includes(kind)
    ? ["logic.mjs"]
    : kind === "renamedReexport"
      ? ["relay.mjs"]
      : kind === "throwValue"
        ? ["logic.mjs"]
        : kind === "composedEffect"
          ? ["state.mjs"]
          : kind === "binding" || kind === "effect"
            ? [input.cell0Owner]
            : kind === "function"
              ? modules
              : [input.spec.entry];
  for (const name of files) {
    const file = join(out, name);
    const original = await readFile(file, "utf8");
    let changed = original;
    if (kind === "binding") changed = original.replace(/let cell0 = (\d+);/, (_, n) => `let cell0 = ${Number(n) + 1};`);
    else if (kind === "localAliasRedirect")
      changed = original.replace(/\bvalue0 as (__split_local_+\d+)/, "value1 as $1");
    else if (kind === "localAliasUpdateRedirect")
      changed = original.replace(/\bincrement as (__split_local_+\d+)/, "otherIncrement as $1");
    else if (kind === "localAliasNumericRedirect") {
      const index = input.numericLeftIndex;
      changed = original.replace(
        new RegExp(`(__split_reexport_+\\d+_)${index} as (__split_local_+${index})\\b`),
        (_, prefix, local) => `${prefix}${index + 1} as ${local}`
      );
    } else if (kind === "localAliasInitRedirect")
      changed = original.replace(
        /(__split_alias_+)0 as (__split_local_+0)\b/,
        (_, prefix, local) => `${prefix}1 as ${local}`
      );
    else if (kind === "renamedReexport")
      changed = input.redirectedCallee
        ? original.replace(
            /export \{ fuzzRelayHelper as (__split_reexport_+\d+_\d+) \}/,
            "export { fuzzRelayOther as $1 }"
          )
        : original.replace(/export \{ global as (__split_reexport_+\d+_\d+) \}/, "export { global1 as $1 }");
    else if (kind === "throwValue") {
      const start = original.indexOf("function fuzzThrower(");
      if (start >= 0) {
        const prefix = original.slice(0, start),
          body = original.slice(start);
        const [before, after] = throwExpressions(input.throwVariant);
        const mutated = body.replace(`throw ${print(before)}`, `throw ${print(after)}`);
        changed = prefix + mutated;
      }
    } else if (kind === "composedEffect") {
      const discarded = print(input.ast.body[0]);
      if (original.includes(discarded)) {
        const moved = original.replace(discarded, "");
        changed = moved.replace(
          /(?:let|var|const) global = \d+;/,
          (declaration) => `${declaration}\nglobalThis.record(global);`
        );
        if (changed === moved) changed = original;
      }
    } else if (kind === "effect")
      changed = original.replace(/let cell0 = (\d+);/, (declaration) => `${declaration}\nglobalThis.record(cell0);`);
    else if (kind === "function")
      changed = original.replace(/return (cell\d+);/, (_, binding) => `return ${binding} + 1;`);
    else if (kind === "export")
      changed = original.replace(
        /^export \{ (\w+) \} from ("[^"\n]+");$/m,
        (line, binding, path) => `${line}\nexport { ${binding} as fuzz_${binding} } from ${path};`
      );
    else if (kind === "import") {
      const imports = [...original.matchAll(/^import (?:\{[^\n]+\} from )?"\.\/[^"\n]+";$/gm)];
      if (imports.length >= 2) {
        const first = imports[0],
          second = imports[1];
        changed =
          original.slice(0, first.index) +
          second[0] +
          original.slice(first.index + first[0].length, second.index) +
          first[0] +
          original.slice(second.index + second[0].length);
      }
    }
    if (changed !== original) {
      await writeFile(file, changed);
      return;
    }
  }
  throw new Error(`mutation failed: ${kind}`);
}

function reproduceUnsafeWitness(source, entry, verdict) {
  const calls = verdict.calls ?? [];
  const before = execute(source, calls);
  const after = execute(entry, calls);
  const witness = (trace) => ({
    events: trace.events,
    exports: trace.exports,
    ...(calls.length ? { results: trace.results.map((v) => (v.undefined ? null : v.value)) } : {}),
    ...(calls.length && trace.error ? { thrown: trace.error.thrown } : {}),
  });
  const invalid = (trace) =>
    trace.inconclusive ||
    (trace.error && (!calls.length || trace.error.phase !== "call" || trace.error.thrown.kind === "opaque"));
  if (
    invalid(before) ||
    invalid(after) ||
    JSON.stringify(before) === JSON.stringify(after) ||
    JSON.stringify(verdict.original) !== JSON.stringify(witness(before)) ||
    JSON.stringify(verdict.output) !== JSON.stringify(witness(after))
  )
    throw new Error("unsafe witness does not match Node");
  return { calls, before, after };
}

async function minimizeDifference(
  input,
  root,
  mutation = null,
  expectedVerdict = "proved under assumptions",
  maxAttempts = Infinity
) {
  const scratch = join(root, "shrink-attempt");
  let attempts = 0;
  let initialCheck = true;
  const fails = async (candidate) => {
    if (initialCheck) initialCheck = false;
    else {
      if (attempts >= maxAttempts) return false;
      attempts++;
    }
    await rm(scratch, { recursive: true, force: true });
    await mkdir(scratch, { recursive: true });
    const source = join(scratch, "source.mjs"),
      spec = join(scratch, "spec.json"),
      out = join(scratch, "out");
    await writeFile(source, candidate.source);
    await writeFile(spec, JSON.stringify(candidate.spec));
    try {
      await generateBindings(source, spec, out);
      if (mutation) await mutateOutput(out, candidate, mutation);
      const before = execute(source, candidate.calls),
        after = execute(join(out, candidate.spec.entry), candidate.calls);
      if (
        before.inconclusive ||
        after.inconclusive ||
        before.error?.thrown?.kind === "opaque" ||
        after.error?.thrown?.kind === "opaque" ||
        JSON.stringify(before) === JSON.stringify(after)
      )
        return false;
      return (await validateBindings(source, spec, out)).result === expectedVerdict;
    } catch {
      return false;
    }
  };
  const minimized = await minimizeCase(input, fails);
  const destination = join(root, "minimized");
  await rm(destination, { recursive: true, force: true });
  await mkdir(destination, { recursive: true });
  await writeFile(join(destination, "source.mjs"), minimized.source);
  await writeFile(join(destination, "spec.json"), JSON.stringify(minimized.spec, null, 2) + "\n");
  await writeFile(join(destination, "calls.json"), JSON.stringify(minimized.calls, null, 2) + "\n");
  const minimizedSource = join(destination, "source.mjs"),
    minimizedSpec = join(destination, "spec.json"),
    minimizedOut = join(destination, "out");
  await generateBindings(minimizedSource, minimizedSpec, minimizedOut);
  if (mutation) await mutateOutput(minimizedOut, minimized, mutation);
  await writeFile(
    join(destination, "node.json"),
    JSON.stringify(
      {
        before: execute(minimizedSource, minimized.calls),
        after: execute(join(minimizedOut, minimized.spec.entry), minimized.calls),
        nodeVersion: process.version,
      },
      null,
      2
    ) + "\n"
  );
  const finalVerdict = await validateBindings(minimizedSource, minimizedSpec, minimizedOut);
  if (finalVerdict.result !== expectedVerdict) throw new Error(`minimized case lost its ${expectedVerdict} verdict`);
  if (expectedVerdict === "unsafe")
    await writeFile(
      join(destination, "node-witness.json"),
      JSON.stringify(
        reproduceUnsafeWitness(minimizedSource, join(minimizedOut, minimized.spec.entry), finalVerdict),
        null,
        2
      ) + "\n"
    );
  await writeFile(join(destination, "verdict.json"), JSON.stringify(finalVerdict, null, 2) + "\n");
  await writeFile(join(destination, "reduction.json"), JSON.stringify({ attempts, maxAttempts }, null, 2) + "\n");
  await rm(scratch, { recursive: true, force: true });
}

// Every generated case is reproducible independently from seed and index.
// Results and all emitted modules are retained on an invariant failure.
export async function runFuzz({
  seed,
  start = 0,
  cases,
  outputDir,
  validateEvery = 1,
  shard = 0,
  shards = 1,
  splitMode = "pattern",
  grammarMode = "pattern",
  mutationMode = "enabled",
  unsafeShrinkAttempts = 0,
}) {
  if (!Number.isSafeInteger(unsafeShrinkAttempts) || unsafeShrinkAttempts < 0)
    throw new Error("unsafe shrink attempts must be a nonnegative integer");
  if (!["enabled", "disabled"].includes(mutationMode)) throw new Error("unknown mutation mode");
  if (!["pattern", "independent"].includes(splitMode)) throw new Error("unknown split mode");
  if (!["pattern", "composed", "async", "alias"].includes(grammarMode)) throw new Error("unknown grammar mode");
  if (!Number.isSafeInteger(shards) || shards < 1 || !Number.isSafeInteger(shard) || shard < 0 || shard >= shards)
    throw new Error("shard must be an integer between 0 and shards - 1");
  await mkdir(outputDir, { recursive: true });
  const leanVersionResult = spawnSync(process.env.JS_MODULE_PROOF_LEAN ?? "lean", ["--version"], {
    encoding: "utf8",
    timeout: 5000,
  });
  const leanVersion = leanVersionResult.status === 0 ? leanVersionResult.stdout.trim() : null;
  const summary = {
    seed,
    start,
    shard,
    shards,
    cases: 0,
    splitMode,
    grammarMode,
    mutationMode,
    unsafeShrinkAttempts,
    asyncSourceExecuted: 0,
    asyncRefused: 0,
    nodeVersion: process.version,
    leanVersion,
    independentRefused: 0,
    independentOrdered: 0,
    independentOrderedRefused: 0,
    independentRequested: { acyclic: 0, cyclic: 0, invalidAssignment: 0 },
    independentRefusedByCategory: { acyclic: 0, cyclic: 0, invalidAssignment: 0 },
    // Count cases containing each construct, rather than occurrences: a deeply
    // nested case should not hide a grammar production that rarely appears.
    astNodeCases: {},
    moduleCountCases: {},
    importCountCases: {},
    overflowIntermediateCases: 0,
    renamedExportCases: 0,
    multiplePublicNameCases: 0,
    importAliasRequestedCases: 0,
    importAliasGeneratedCases: 0,
    importAliasComparedCases: 0,
    importAliasProvedCases: 0,
    localImportAliasRequestedCases: 0,
    localImportAliasGeneratedCases: 0,
    localImportAliasComparedCases: 0,
    localImportAliasProvedCases: 0,
    parameterizedAliasCases: 0,
    multipleParameterAliasCases: 0,
    multipleParameterAliasComparedCases: 0,
    multipleParameterAliasProvedCases: 0,
    parameterizedAliasComparedCases: 0,
    parameterizedAliasWithLocalImportComparedCases: 0,
    parameterizedAliasFunctionImportComparedCases: 0,
    parameterizedAliasProvedCases: 0,
    reexportRelayCases: 0,
    reexportRelayGeneratedCases: 0,
    reexportRelayComparedCases: 0,
    reexportRelayProvedCases: 0,
    renamedReexportRequestedCases: 0,
    renamedReexportGeneratedCases: 0,
    renamedReexportComparedCases: 0,
    renamedReexportProvedCases: 0,
    discardedLiteralCases: 0,
    discardedLiteralComparedCases: 0,
    discardedLiteralProvedCases: 0,
    textObservationCases: 0,
    textObservationComparedCases: 0,
    textObservationProvedCases: 0,
    primitiveObservationCases: 0,
    primitiveObservationComparedCases: 0,
    primitiveObservationProvedCases: 0,
    undefinedObservationCases: 0,
    undefinedObservationComparedCases: 0,
    undefinedObservationProvedCases: 0,
    negativeObservationCases: 0,
    negativeObservationComparedCases: 0,
    negativeObservationProvedCases: 0,
    negativeZeroObservationCases: 0,
    negativeZeroObservationComparedCases: 0,
    negativeZeroObservationProvedCases: 0,
    specialNumberObservationCases: 0,
    specialNumberObservationComparedCases: 0,
    specialNumberObservationProvedCases: 0,
    bigIntegerObservationCases: 0,
    bigIntegerObservationComparedCases: 0,
    bigIntegerObservationProvedCases: 0,
    varCases: 0,
    varGeneratedCases: 0,
    varComparedCases: 0,
    varProvedCases: 0,
    localVarCases: 0,
    localVarComparedCases: 0,
    localVarProvedCases: 0,
    earlyVarCases: 0,
    plannedEarlyVarReorderCases: 0,
    earlyVarComparedCases: 0,
    earlyVarDifferentCases: 0,
    earlyVarProvedCases: 0,
    earlyVarUnsafeCases: 0,
    constantCases: 0,
    objectCases: 0,
    objectSourceExecuted: 0,
    objectRefused: 0,
    objectCompared: 0,
    objectProved: 0,
    capturedCases: 0,
    capturedCompared: 0,
    capturedProved: 0,
    capturedUnsafe: 0,
    parameterCases: 0,
    parameterExpressionDepths: {},
    parameterCompared: 0,
    parameterProved: 0,
    modeledClosureCases: 0,
    modeledClosureCrossModule: 0,
    modeledClosureCompared: 0,
    modeledClosureProved: 0,
    modeledClosureUnsafe: 0,
    modeledClosureThrows: 0,
    modeledClosureBranches: 0,
    closureCases: 0,
    closureSourceExecuted: 0,
    closureRefused: 0,
    closureGenerated: 0,
    closureCompared: 0,
    closureDepths: {},
    closureShadowed: 0,
    closureWrites: 0,
    controlFlowCases: 0,
    finallyCases: 0,
    finallySourceExecuted: 0,
    branchThen: 0,
    branchElse: 0,
    generated: 0,
    refused: 0,
    refusalReasons: {},
    unsupportedCaseRefused: 0,
    invalidAssignmentCaseRefused: 0,
    compared: 0,
    inconclusive: 0,
    inconclusiveCases: [],
    equal: 0,
    different: 0,
    mutated: 0,
    mutationKinds: {
      binding: 0,
      effect: 0,
      function: 0,
      export: 0,
      import: 0,
      composedEffect: 0,
      throwValue: 0,
      renamedReexport: 0,
      localAliasRedirect: 0,
      localAliasInitRedirect: 0,
      localAliasUpdateRedirect: 0,
      localAliasNumericRedirect: 0,
    },
    splitCompared: 0,
    splitDifferent: 0,
    mutatedDifferent: 0,
    validated: 0,
    mutatedVerdicts: { proved: 0, unsafe: 0, unknown: 0 },
    unmodifiedVerdicts: { proved: 0, unsafe: 0, unknown: 0 },
    proved: 0,
    unsafe: 0,
    unknown: 0,
    unknownCases: [],
    unvalidated: 0,
    unexplainedDifferences: [],
    certifiedWitnesses: [],
  };
  for (let index = start; index < start + cases; index++) {
    if (index % shards !== shard) continue;
    summary.cases++;
    const root = await mkdtemp(join(outputDir, `case-${index}-`));
    const artifactPath = relative(outputDir, root);
    const source = join(root, "source.mjs");
    const spec = join(root, "spec.json");
    const out = join(root, "out");
    let keep = true;
    let hasGeneratedImportAlias = false;
    let hasGeneratedLocalImportAlias = false;
    let hasGeneratedParameterizedAliasUse = false;
    let hasGeneratedRenamedReexport = false;
    let hasVar = false;
    let phase = "generate-ast";
    try {
      const generated =
        grammarMode === "composed"
          ? makeComposedCase(seed, index)
          : grammarMode === "async"
            ? makeAsyncCase(seed, index)
            : grammarMode === "alias"
              ? makeLocalAliasCase(seed, index)
              : makeCase(seed, index);
      const input = splitMode === "independent" ? independentSplit(generated, seed, index) : generated;
      const plannedMutation =
        input.grammar === "alias" && !input.independentSplit && index % 12 === 1
          ? "localAliasNumericRedirect"
          : input.grammar === "alias" && !input.independentSplit && index % 12 === 3
            ? "localAliasInitRedirect"
            : input.grammar === "alias" && !input.independentSplit && index % 12 === 4
              ? "localAliasUpdateRedirect"
              : input.grammar === "alias" && !input.independentSplit && index % 6 === 2
                ? "localAliasRedirect"
                : input.grammar === "composed" && !input.independentSplit && index % 64 === 59
                  ? "renamedReexport"
                  : input.grammar === "composed" && !input.independentSplit && index % 64 === 23
                    ? "throwValue"
                    : input.grammar === "composed" &&
                        !input.independentSplit &&
                        input.hasDiscardedLiteral &&
                        index % 52 === 16
                      ? "composedEffect"
                      : !["composed", "async", "alias"].includes(input.grammar) &&
                          !input.independentSplit &&
                          index % 8 === 2
                        ? ["binding", "effect", "function", "export", "import"][Math.floor(index / 8) % 5]
                        : null;
      const mutation = mutationMode === "disabled" ? null : plannedMutation;
      if (plannedMutation === "composedEffect") input.calls = [];
      if (plannedMutation === "throwValue") input.calls = [{ name: "fuzzThrower", args: [1] }];
      if (plannedMutation === "localAliasRedirect") input.calls = [{ name: "read0", args: [] }];
      if (plannedMutation === "localAliasInitRedirect") input.calls = [];
      if (plannedMutation === "localAliasUpdateRedirect")
        input.calls = ["current", "bump", "current"].map((name) => ({ name, args: [] }));
      if (plannedMutation === "localAliasNumericRedirect")
        input.calls = [{ name: "compute", args: input.numericArguments }];
      if (plannedMutation === "renamedReexport")
        input.calls =
          !input.redirectedCallee && (index % 256 === 187 || index % 256 === 251 || index % 512 === 315)
            ? []
            : [{ name: "fuzzRelayRead", args: index % 128 === 123 || input.redirectedCallee ? [0] : [] }];
      const independentCategory = input.invalidAssignment ? "invalidAssignment" : input.cyclic ? "cyclic" : "acyclic";
      if (input.independentSplit) {
        summary.independentRequested[independentCategory]++;
        if (input.ordered) summary.independentOrdered++;
      }
      const nodeTypes = new Set();
      const visit = (value) => {
        if (!value || typeof value !== "object") return;
        if (typeof value.type === "string") nodeTypes.add(value.type);
        if (value.type === "VariableDeclaration" && value.kind === "var") hasVar = true;
        for (const child of Object.values(value)) {
          if (Array.isArray(child)) child.forEach(visit);
          else visit(child);
        }
      };
      visit(input.ast);
      if (input.overflowIntermediate) summary.overflowIntermediateCases++;
      if (hasVar) summary.varCases++;
      if (input.hasDiscardedLiteral) summary.discardedLiteralCases++;
      if (input.hasTextObservation) summary.textObservationCases++;
      if (input.hasPrimitiveObservation) summary.primitiveObservationCases++;
      if (input.hasUndefinedObservation) summary.undefinedObservationCases++;
      if (input.hasNegativeObservation) summary.negativeObservationCases++;
      if (input.hasNegativeZeroObservation) summary.negativeZeroObservationCases++;
      if (input.hasSpecialNumberObservation) summary.specialNumberObservationCases++;
      if (input.hasBigIntegerObservation) summary.bigIntegerObservationCases++;
      if (input.hasReexportRelay && !input.independentSplit) summary.reexportRelayCases++;
      if (input.hasLocalVar) summary.localVarCases++;
      if (input.earlyVar) summary.earlyVarCases++;
      if (input.plannedEarlyVarReorder) summary.plannedEarlyVarReorderCases++;
      for (const type of nodeTypes) summary.astNodeCases[type] = (summary.astNodeCases[type] ?? 0) + 1;
      const exports = input.ast.body.flatMap((node) =>
        node.type === "ExportNamedDeclaration" ? (node.specifiers ?? []) : []
      );
      if (exports.some((item) => item.local.name !== item.exported.name)) summary.renamedExportCases++;
      if (new Set(exports.map((item) => item.local.name)).size < exports.length) summary.multiplePublicNameCases++;
      const modules = Object.values(input.spec.modules);
      if (modules.some((module) => module.aliasImports)) summary.importAliasRequestedCases++;
      if (modules.some((module) => module.renameLocalImports)) summary.localImportAliasRequestedCases++;
      if (input.grammar === "alias" && input.numericArguments.length) summary.parameterizedAliasCases++;
      if (input.grammar === "alias" && input.numericArguments.length > 1) summary.multipleParameterAliasCases++;
      if (modules.some((module) => module.renameReexports)) summary.renamedReexportRequestedCases++;
      const importCount = modules.reduce((count, module) => count + module.imports.length, 0);
      summary.moduleCountCases[modules.length] = (summary.moduleCountCases[modules.length] ?? 0) + 1;
      summary.importCountCases[importCount] = (summary.importCountCases[importCount] ?? 0) + 1;
      phase = "write-input";
      await writeFile(source, input.source);
      await writeFile(spec, JSON.stringify(input.spec, null, 2) + "\n");
      await writeFile(
        join(root, "case.json"),
        JSON.stringify(
          {
            seed,
            index,
            calls: input.calls,
            grammar: input.grammar,
            unsupported: input.unsupported,
            invalidAssignment: input.invalidAssignment,
            cyclic: input.cyclic,
            plannedMutation: plannedMutation !== null,
            plannedMutationKind: plannedMutation,
            mutation,
            mutationMode,
            unsafeShrinkAttempts,
            splitMode,
            grammarMode,
            ...(input.independentSplit ? { splitCategory: independentCategory } : {}),
            nodeVersion: process.version,
            leanVersion,
          },
          null,
          2
        ) + "\n"
      );
      if (input.useConstant) summary.constantCases++;
      if (input.grammar === "async") {
        phase = "source-execution";
        const observed = execute(source, input.calls);
        await writeFile(join(root, "node-source.json"), JSON.stringify(observed, null, 2) + "\n");
        if (observed.inconclusive) {
          summary.inconclusive++;
          summary.inconclusiveCases.push({ index, reason: observed.inconclusive, artifacts: root, artifactPath });
          continue;
        }
        if (
          JSON.stringify(observed.events) !== JSON.stringify([input.asyncFirstValue, 0]) ||
          observed.results[0]?.value !== input.asyncFirstValue + 1 ||
          observed.error?.thrown?.value !== 7
        )
          throw new Error(`generated asynchronous program did not finish as expected: ${JSON.stringify(observed)}`);
        summary.asyncSourceExecuted++;
      }
      if (input.grammar === "object") {
        phase = "source-execution";
        summary.objectCases++;
        const observed = execute(source, input.calls);
        await writeFile(join(root, "node-source.json"), JSON.stringify(observed, null, 2) + "\n");
        if (observed.inconclusive) {
          summary.inconclusive++;
          summary.inconclusiveCases.push({ index, reason: observed.inconclusive, artifacts: root, artifactPath });
          continue;
        }
        if (
          observed.error ||
          observed.results.length !== input.calls.length ||
          observed.results.some((r) => !Number.isSafeInteger(r.value)) ||
          observed.results[0].value === observed.results[1].value
        )
          throw new Error(`generated object program did not execute within its bounds: ${JSON.stringify(observed)}`);
        summary.objectSourceExecuted++;
      }
      if (input.grammar === "captured") summary.capturedCases++;
      if (input.grammar === "parameter") {
        summary.parameterCases++;
        summary.parameterExpressionDepths[input.expressionDepth] =
          (summary.parameterExpressionDepths[input.expressionDepth] ?? 0) + 1;
      }
      if (input.grammar === "modeledClosure") {
        summary.modeledClosureCases++;
        if (input.crossModuleCapture) summary.modeledClosureCrossModule++;
      }
      if (input.grammar === "closure") {
        phase = "source-execution";
        summary.closureCases++;
        if (input.usesFinally) summary.finallyCases++;
        summary.closureDepths[input.depth] = (summary.closureDepths[input.depth] ?? 0) + 1;
        if (input.shadowed) summary.closureShadowed++;
        if (input.writesCaptured) summary.closureWrites++;
        const observed = execute(source, input.calls);
        await writeFile(join(root, "node-source.json"), JSON.stringify(observed, null, 2) + "\n");
        if (observed.inconclusive || observed.error?.thrown?.kind === "opaque") {
          summary.inconclusive++;
          summary.inconclusiveCases.push({
            index,
            reason: observed.inconclusive ?? "opaque thrown value",
            artifacts: root,
            artifactPath,
          });
          continue;
        }
        if (
          observed.error ||
          observed.results.length !== input.calls.length ||
          observed.results.some((r) => !Number.isSafeInteger(r.value))
        )
          throw new Error(`generated closure did not execute within its bounds: ${JSON.stringify(observed)}`);
        summary.closureSourceExecuted++;
        if (input.usesFinally) {
          if (observed.events.filter((event) => event === 103).length !== input.calls.length)
            throw new Error("generated finally block did not run once per call");
          summary.finallySourceExecuted++;
        }
        if (input.controlFlow) {
          summary.controlFlowCases++;
          if (observed.events.includes(101)) summary.branchThen++;
          if (observed.events.includes(102)) summary.branchElse++;
          if ((!observed.events.includes(101) && !observed.events.includes(102)) || !observed.events.includes(0))
            throw new Error("generated control flow did not run");
        }
      }
      phase = "split-generation";
      try {
        await generateBindings(source, spec, out);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
        summary.refused++;
        const reason = error instanceof Error ? error.message : String(error);
        summary.refusalReasons[reason] = (summary.refusalReasons[reason] ?? 0) + 1;
        if (input.independentSplit) {
          summary.independentRefused++;
          if (input.ordered) summary.independentOrderedRefused++;
          summary.independentRefusedByCategory[independentCategory]++;
        } else if (input.invalidAssignment) summary.invalidAssignmentCaseRefused++;
        else if (input.unsupported) summary.unsupportedCaseRefused++;
        if (input.grammar === "closure") summary.closureRefused++;
        if (input.grammar === "object") summary.objectRefused++;
        if (input.grammar === "async") summary.asyncRefused++;
        if (!input.independentSplit && !input.unsupported && !input.invalidAssignment)
          throw new Error(`supported generated case was refused: ${error}`);
        keep = false;
        continue;
      }
      if (input.invalidAssignment) throw new Error("invalid assignment unexpectedly accepted");
      summary.generated++;
      if (input.hasReexportRelay && !input.independentSplit) summary.reexportRelayGeneratedCases++;
      if (hasVar) summary.varGeneratedCases++;
      if (modules.some((module) => module.aliasImports || module.renameLocalImports || module.renameReexports)) {
        const emitted = await Promise.all(
          Object.keys(input.spec.modules).map((name) => readFile(join(out, name), "utf8"))
        );
        hasGeneratedImportAlias = emitted.some((text) => /__split_alias_+\d+ as [A-Za-z_$][\w$]*/.test(text));
        if (hasGeneratedImportAlias) summary.importAliasGeneratedCases++;
        hasGeneratedLocalImportAlias = emitted.some((text) => /\bas __split_local_+\d+/.test(text));
        if (hasGeneratedLocalImportAlias) summary.localImportAliasGeneratedCases++;
        if (input.grammar === "alias" && input.numericArguments.length)
          hasGeneratedParameterizedAliasUse = emitted.some((text) =>
            /function compute\([^)]*\)\s*\{[^}]*\b__split_local_+\d+\b/.test(text)
          );
        hasGeneratedRenamedReexport = emitted.some((text) => /export \{ [^}]* as __split_reexport_+\d+_\d+/.test(text));
        if (hasGeneratedRenamedReexport) summary.renamedReexportGeneratedCases++;
      }
      if (input.grammar === "closure") summary.closureGenerated++;
      const mutated = mutation !== null;
      if (mutated) {
        phase = "output-mutation";
        await mutateOutput(out, input, mutation);
        summary.mutated++;
        summary.mutationKinds[mutation]++;
      }
      phase = "node-comparison";
      const before = execute(source, input.calls);
      const after = execute(join(out, input.spec.entry), input.calls);
      if (
        before.inconclusive ||
        after.inconclusive ||
        before.error?.thrown?.kind === "opaque" ||
        after.error?.thrown?.kind === "opaque"
      ) {
        summary.inconclusive++;
        await writeFile(join(root, "node.json"), JSON.stringify({ before, after }, null, 2) + "\n");
        summary.inconclusiveCases.push({
          index,
          reason: before.inconclusive ?? after.inconclusive ?? "opaque thrown value",
          artifacts: root,
          artifactPath,
        });
        continue;
      }
      if (input.earlyVar && JSON.stringify(before.events[0]) !== JSON.stringify({ undefined: true }))
        throw new Error("generated early var did not record undefined");
      const equal = JSON.stringify(before) === JSON.stringify(after);
      await writeFile(join(root, "node.json"), JSON.stringify({ before, after }, null, 2) + "\n");
      summary.compared++;
      if (input.hasDiscardedLiteral) summary.discardedLiteralComparedCases++;
      if (input.hasTextObservation) summary.textObservationComparedCases++;
      if (input.hasPrimitiveObservation) summary.primitiveObservationComparedCases++;
      if (input.hasUndefinedObservation) summary.undefinedObservationComparedCases++;
      if (input.hasNegativeObservation) summary.negativeObservationComparedCases++;
      if (input.hasNegativeZeroObservation) summary.negativeZeroObservationComparedCases++;
      if (input.hasSpecialNumberObservation) summary.specialNumberObservationComparedCases++;
      if (input.hasBigIntegerObservation) summary.bigIntegerObservationComparedCases++;
      if (input.hasReexportRelay && !input.independentSplit) summary.reexportRelayComparedCases++;
      if (hasVar) summary.varComparedCases++;
      if (input.hasLocalVar) summary.localVarComparedCases++;
      if (input.earlyVar) {
        summary.earlyVarComparedCases++;
        if (!equal) summary.earlyVarDifferentCases++;
      }
      if (hasGeneratedImportAlias) summary.importAliasComparedCases++;
      if (hasGeneratedLocalImportAlias) summary.localImportAliasComparedCases++;
      if (input.grammar === "alias" && input.numericArguments.length) {
        summary.parameterizedAliasComparedCases++;
        if (input.numericArguments.length > 1) summary.multipleParameterAliasComparedCases++;
        if (hasGeneratedLocalImportAlias) summary.parameterizedAliasWithLocalImportComparedCases++;
        if (hasGeneratedParameterizedAliasUse) summary.parameterizedAliasFunctionImportComparedCases++;
      }
      if (hasGeneratedRenamedReexport) summary.renamedReexportComparedCases++;
      if (input.grammar === "parameter") {
        summary.parameterCompared++;
        if (
          JSON.stringify(before.error?.thrown) !== JSON.stringify(input.expectedThrown) ||
          before.error.index !== 1 ||
          before.results.length !== 1
        )
          throw new Error("generated parameter branch did not preserve the primitive throw and return prefix");
      }
      if (input.grammar === "object") summary.objectCompared++;
      if (input.grammar === "captured") {
        summary.capturedCompared++;
        if (!input.independentSplit && equal === input.expectUnsafe)
          throw new Error("captured split differed from intended Node behavior");
      }
      if (input.grammar === "closure") summary.closureCompared++;
      if (input.grammar === "modeledClosure") {
        summary.modeledClosureCompared++;
        if (before.results[0]?.value !== 1 || !(before.results[1]?.value > 1))
          throw new Error("generated closure branch did not execute both outcomes");
        summary.modeledClosureBranches++;
        if (
          !input.independentSplit &&
          (equal === input.expectUnsafe || (!input.expectThrow && (before.error || after.error)))
        )
          throw new Error("closure split differed from its intended Node behavior");
        if (input.expectThrow) {
          if (
            before.error?.thrown?.kind !== "number" ||
            before.error.thrown.value !== input.thrownValue ||
            before.error.index !== 3 ||
            before.results.length !== 3
          )
            throw new Error("generated throw did not preserve the trace prefix");
          summary.modeledClosureThrows++;
        }
      }
      summary[equal ? "equal" : "different"]++;
      if (!mutated) summary.splitCompared++;
      if (!equal) summary[mutated ? "mutatedDifferent" : "splitDifferent"]++;
      let verdictName = "unvalidated";
      if (random(seed ^ 0xa5a5a5a5, index)(validateEvery) === 0) {
        phase = "validation";
        let verdict;
        try {
          verdict = await validateBindings(source, spec, out);
        } catch (error) {
          if (!String(error).includes("unknown:")) throw error;
          verdict = { result: "unknown", reason: String(error) };
        }
        summary.validated++;
        verdictName = verdict.result;
        const verdictKey = verdict.result === "proved under assumptions" ? "proved" : verdict.result;
        const verdicts = mutated ? summary.mutatedVerdicts : summary.unmodifiedVerdicts;
        if (Object.hasOwn(verdicts, verdictKey)) verdicts[verdictKey]++;
        await writeFile(join(root, "verdict.json"), JSON.stringify(verdict, null, 2) + "\n");
        if (input.grammar === "captured" && !input.independentSplit) {
          const expected = input.expectUnsafe ? "unsafe" : "proved under assumptions";
          if (verdict.result !== expected)
            throw new Error(`captured validation expected ${expected}, got ${verdict.result}: ${verdict.reason ?? ""}`);
          if (!input.expectUnsafe && !verdict.certificate?.includes("binding_captured_observations"))
            throw new Error("captured validation omitted relational observation certificate");
          if (input.expectUnsafe) summary.capturedUnsafe++;
        }
        if (input.grammar === "modeledClosure" && !input.independentSplit) {
          const expected = input.expectUnsafe ? "unsafe" : "proved under assumptions";
          if (verdict.result !== expected)
            throw new Error(`closure validation expected ${expected}, got ${verdict.result}: ${verdict.reason ?? ""}`);
          if (input.expectUnsafe) summary.modeledClosureUnsafe++;
          else summary.modeledClosureProved++;
        }
        if (verdict.result === "proved under assumptions") {
          summary.proved++;
          if (input.hasDiscardedLiteral) summary.discardedLiteralProvedCases++;
          if (input.hasTextObservation) summary.textObservationProvedCases++;
          if (input.hasPrimitiveObservation) summary.primitiveObservationProvedCases++;
          if (input.hasUndefinedObservation) summary.undefinedObservationProvedCases++;
          if (input.hasNegativeObservation) summary.negativeObservationProvedCases++;
          if (input.hasNegativeZeroObservation) summary.negativeZeroObservationProvedCases++;
          if (input.hasSpecialNumberObservation) summary.specialNumberObservationProvedCases++;
          if (input.hasBigIntegerObservation) summary.bigIntegerObservationProvedCases++;
          if (input.hasReexportRelay && !input.independentSplit) summary.reexportRelayProvedCases++;
          if (hasVar) summary.varProvedCases++;
          if (input.hasLocalVar) summary.localVarProvedCases++;
          if (input.earlyVar) summary.earlyVarProvedCases++;
          if (hasGeneratedImportAlias) summary.importAliasProvedCases++;
          if (hasGeneratedLocalImportAlias) summary.localImportAliasProvedCases++;
          if (input.grammar === "alias" && input.numericArguments.length) summary.parameterizedAliasProvedCases++;
          if (input.grammar === "alias" && input.numericArguments.length > 1)
            summary.multipleParameterAliasProvedCases++;
          if (hasGeneratedRenamedReexport) summary.renamedReexportProvedCases++;
          if (input.grammar === "object") summary.objectProved++;
          if (input.grammar === "captured") summary.capturedProved++;
          if (input.grammar === "parameter") summary.parameterProved++;
          if (!equal || ((before.error || after.error) && !input.expectThrow)) {
            if (!equal) await minimizeDifference(input, root, mutation);
            throw new Error("proved case differs or fails in Node");
          }
        } else if (verdict.result === "unsafe") {
          summary.unsafe++;
          if (input.earlyVar) summary.earlyVarUnsafeCases++;
          const {
            calls: witnessCalls,
            before: witnessBefore,
            after: witnessAfter,
          } = reproduceUnsafeWitness(source, join(out, input.spec.entry), verdict);
          await writeFile(
            join(root, "node-witness.json"),
            JSON.stringify({ calls: witnessCalls, before: witnessBefore, after: witnessAfter }, null, 2) + "\n"
          );
          if (unsafeShrinkAttempts > 0)
            await minimizeDifference({ ...input, calls: witnessCalls }, root, mutation, "unsafe", unsafeShrinkAttempts);
          summary.certifiedWitnesses.push({
            index,
            mutated,
            calls: witnessCalls,
            artifactPath,
            ...(unsafeShrinkAttempts > 0 ? { minimizedPath: `${artifactPath}/minimized` } : {}),
          });
        } else if (verdict.result === "unknown") {
          summary.unknown++;
          summary.unknownCases.push({
            index,
            reason: verdict.reason,
            detail: String(verdict.detail ?? "").slice(0, 3000),
          });
        } else throw new Error(`unexpected validator result: ${verdict.result}`);
      } else summary.unvalidated++;
      keep = verdictName === "unsafe" || (!equal && ["unknown", "unvalidated"].includes(verdictName));
      if (keep && verdictName !== "unsafe")
        summary.unexplainedDifferences.push({ index, mutated, verdict: verdictName, artifacts: root, artifactPath });
    } catch (error) {
      await writeFile(
        join(root, "failure.json"),
        JSON.stringify(
          {
            seed,
            index,
            phase,
            nodeVersion: process.version,
            leanVersion,
            grammarMode,
            splitMode,
            error: String(error),
            stack: error?.stack,
          },
          null,
          2
        ) + "\n"
      ).catch(() => {});
      throw new Error(`seed=${seed} index=${index}; artifacts: ${root}\n${error.stack ?? error}`);
    } finally {
      if (!keep) await rm(root, { recursive: true, force: true });
    }
  }
  return summary;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  try {
    const args = process.argv.slice(2);
    if (args.length % 2) throw new Error("options require values");
    const opts = Object.fromEntries(Array.from({ length: args.length / 2 }, (_, i) => [args[2 * i], args[2 * i + 1]]));
    if (
      Object.keys(opts).some(
        (key) =>
          ![
            "--seed",
            "--start",
            "--cases",
            "--output-dir",
            "--validate-every",
            "--shard",
            "--shards",
            "--splits",
            "--grammar",
            "--mutations",
            "--shrink-unsafe-attempts",
          ].includes(key)
      )
    )
      throw new Error("unknown option");
    const seed = Number(opts["--seed"] ?? 1),
      start = Number(opts["--start"] ?? 0),
      cases = Number(opts["--cases"] ?? 100),
      validateEvery = Number(opts["--validate-every"] ?? 1),
      shard = Number(opts["--shard"] ?? 0),
      shards = Number(opts["--shards"] ?? 1),
      unsafeShrinkAttempts = Number(opts["--shrink-unsafe-attempts"] ?? 0);
    if (
      ![seed, start, cases, validateEvery, shard, shards, unsafeShrinkAttempts].every(Number.isSafeInteger) ||
      seed < 0 ||
      seed > 0xffffffff ||
      start < 0 ||
      cases < 1 ||
      validateEvery < 1 ||
      unsafeShrinkAttempts < 0 ||
      shards < 1 ||
      shard < 0 ||
      shard >= shards ||
      !opts["--output-dir"]
    )
      throw new Error(
        "usage: fuzz.mjs --output-dir DIR [--seed UINT32] [--start N] [--cases N] [--validate-every N] [--shard N --shards N] [--splits pattern|independent] [--grammar pattern|composed|async|alias] [--mutations enabled|disabled] [--shrink-unsafe-attempts N]"
      );
    console.log(
      JSON.stringify(
        await runFuzz({
          seed,
          start,
          cases,
          validateEvery,
          shard,
          shards,
          splitMode: opts["--splits"] ?? "pattern",
          grammarMode: opts["--grammar"] ?? "pattern",
          mutationMode: opts["--mutations"] ?? "enabled",
          unsafeShrinkAttempts,
          outputDir: resolve(opts["--output-dir"]),
        }),
        null,
        2
      )
    );
  } catch (error) {
    console.error(error.stack ?? String(error));
    process.exitCode = 1;
  }
}
