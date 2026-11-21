import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { generateBindings, validateBindings } from "./bindings.mjs";

// Construct ESTree nodes first, then print the subset of JavaScript used here.
// Adding grammar productions does not require changing the splitter's parser.
const id = (name) => ({ type: "Identifier", name });
const literal = (value) => ({ type: "Literal", value });
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

function print(node) {
  switch (node.type) {
    case "Program":
      return node.body.map(print).join("\n") + "\n";
    case "Identifier":
      return node.name;
    case "Literal":
      return JSON.stringify(node.value);
    case "VariableDeclaration":
      return `${node.kind} ${node.declarations.map(print).join(", ")};`;
    case "VariableDeclarator":
      return `${print(node.id)} = ${print(node.init)}`;
    case "FunctionDeclaration":
      return `function ${print(node.id)}(${node.params.map(print).join(", ")}) ${print(node.body)}`;
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
      return print(node.local);
    case "IfStatement":
      return `if (${print(node.test)}) ${print(node.consequent)}${node.alternate ? ` else ${print(node.alternate)}` : ""}`;
    case "TryStatement":
      return `try ${print(node.block)} finally ${print(node.finalizer)}`;
    case "ForStatement":
      return `for (${print(node.init).slice(0, -1)}; ${print(node.test)}; ${print(node.update)}) ${print(node.body)}`;
    case "UpdateExpression":
      return node.prefix ? `${node.operator}${print(node.argument)}` : `${print(node.argument)}${node.operator}`;
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
  const namedClosure = Math.floor(index / 128) % 2 === 1;
  const closureBody = [add(target, "step"), returnNode(id(target))];
  const factory = functionNode(
    "make",
    ["start"],
    [
      declaration("value", { type: "BinaryExpression", operator: "+", left: id("start"), right: id("seed") }),
      ...(namedClosure
        ? [functionNode("increment", ["step"], closureBody), returnNode(id("increment"))]
        : [
            returnNode({
              type: "ArrowFunctionExpression",
              params: [id("step")],
              async: false,
              body: { type: "BlockStatement", body: closureBody },
            }),
          ]),
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
        specifiers: names.map((name) => ({ type: "ExportSpecifier", local: id(name), exported: id(name) })),
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
  const calls = [...names, ...names.slice().reverse()].map((name) => ({ name, args: [1 + rand(5)] }));
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
  const statements = input.ast.body.filter((node) => node.type !== "ExportNamedDeclaration");
  statements.forEach((_, i) => modules[owners[rand(owners.length)]].statements.push(i));
  modules[entry].imports = shuffle(names, rand);
  for (let i = 1; i < names.length; i++)
    for (let j = 0; j < i; j++) if (rand(2)) modules[names[i]].imports.push(names[j]);
  const cyclic = index % 8 === 1;
  const invalidAssignment = index % 8 === 0 && statements.length > 0;
  if (cyclic) modules[names[0]].imports.push(entry);
  if (invalidAssignment) modules[entry].statements.push(0);
  return { ...input, spec: { kind: "bindings", entry, modules }, independentSplit: true, cyclic, invalidAssignment };
}

// Composes bounded expressions and statement sequences using names visible at
// each point. The program is generated before any optional repartitioning.
export function makeComposedCase(seed, index) {
  const rand = random(seed ^ 0xc04f053, index);
  const scope = new Scope();
  scope.declare("parameter");
  scope.declare("global");
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
  const useLocalFunction = dataPrefix.length === 0 && rand(2) === 0;
  if (useLocalFunction) {
    localPrefix.push(
      declaration("current", literal(rand(4))),
      functionNode("nested", ["delta"], [add("current", "delta"), returnNode(id("current"))])
    );
    scope.declare("current");
  }
  let nextLocal = 0;
  const expression = (visible, depth, forbidden = null) => {
    if (useLocalFunction && depth > 0 && rand(6) === 0) return call(id("nested"), [literal(rand(4))]);
    if (depth === 0 || rand(3) === 0) {
      const names = visible.visible().filter((name) => name !== forbidden);
      if (!names.length) return literal(rand(4));
      return rand(3) ? id(names[rand(names.length)]) : literal(rand(4));
    }
    return {
      type: "BinaryExpression",
      operator: "+",
      left: expression(visible, depth - 1, forbidden),
      right: expression(visible, depth - 1, forbidden),
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
      if (mutable && rand(2))
        body.push(
          expr({ type: "AssignmentExpression", operator: "+=", left: id(name), right: expression(visible, 1) })
        );
    }
    if (mutableNames.length && rand(3) === 0) {
      const index = `loop${nextLocal++}`;
      const target = mutableNames[rand(mutableNames.length)];
      body.push({
        type: "ForStatement",
        init: variable(index, 0),
        test: { type: "BinaryExpression", operator: "<", left: id(index), right: literal(rand(5)) },
        update: { type: "UpdateExpression", operator: "++", prefix: false, argument: id(index) },
        body: { type: "BlockStatement", body: [add(target, index)] },
      });
    }
    let terminal;
    if (depth && rand(2)) {
      const operators = ["<", "<=", ">", ">=", "===", "!=="];
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
  const ast = {
    type: "Program",
    sourceType: "module",
    body: [
      variable("global", rand(4)),
      functionNode("calculate", ["parameter"], [...localPrefix, ...dataPrefix, ...sequence(scope, 2)]),
      {
        type: "ExportNamedDeclaration",
        specifiers: [{ type: "ExportSpecifier", local: id("calculate"), exported: id("calculate") }],
      },
    ],
  };
  return {
    ast,
    source: print(ast),
    grammar: "composed",
    expectThrow: true,
    unsupported: false,
    cyclic: false,
    invalidAssignment: false,
    spec: {
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    },
    calls: Array.from({ length: 1 + rand(4) }, () => ({ name: "calculate", args: [rand(5)] })),
  };
}

export function execute(path, calls, { timeoutMs = 10000 } = {}) {
  const code = `
const events=[];
globalThis.record=v=>events.push(v);
let phase='import', index=-1, names=[];
const results=[];
const thrownValue=v=>{
  if(v===null)return {kind:'null'};
  if(v===undefined)return {kind:'undefined'};
  if(typeof v==='number')return {kind:'number',value:Number.isNaN(v)?'NaN':Object.is(v,-0)?'-0':Number.isFinite(v)?v:String(v)};
  if(typeof v==='string'||typeof v==='boolean')return {kind:typeof v,value:v};
  if(typeof v==='bigint')return {kind:'bigint',value:String(v)};
  if(v instanceof Error)return {kind:'opaque',name:v.name};
  return {kind:'opaque'};
};
try{
  const m=await import(${JSON.stringify(pathToFileURL(path).href)});
  names=Object.keys(m);phase='call';
  for(const call of ${JSON.stringify(calls)}){index++;const name=typeof call==='string'?call:call.name;const args=typeof call==='string'?[]:call.args;const v=m[name](...args);results.push(v===undefined?{undefined:true}:{value:v});}
  console.log(JSON.stringify({events,exports:names,results,error:null}));
}catch(e){
  const thrown=thrownValue(e);
  console.log(JSON.stringify({events,exports:names,results,error:{name:e?.name??null,phase,index,thrown}}));
}`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", code], {
    encoding: "utf8",
    timeout: timeoutMs,
  });
  if (child.error?.code === "ETIMEDOUT") return { inconclusive: "timeout" };
  if (child.error || child.status !== 0) throw new Error(`Node oracle failed: ${child.error ?? child.stderr}`);
  return JSON.parse(child.stdout);
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
      for (const module of Object.values(candidate.spec.modules))
        module.imports = module.imports.filter((dep) => dep !== name);
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
        candidate.spec.modules[name].imports.splice(index, 1);
        if (await fails(candidate)) {
          current = candidate;
          changed = true;
          break;
        }
      }
      if (changed) break;
    }
  }
  return current;
}

async function mutateOutput(out, input, kind) {
  const modules = Object.keys(input.spec.modules);
  const files =
    kind === "binding" || kind === "effect" ? [input.cell0Owner] : kind === "function" ? modules : [input.spec.entry];
  for (const name of files) {
    const file = join(out, name);
    const original = await readFile(file, "utf8");
    let changed = original;
    if (kind === "binding") changed = original.replace(/let cell0 = (\d+);/, (_, n) => `let cell0 = ${Number(n) + 1};`);
    else if (kind === "effect")
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

async function minimizeProvedDifference(input, root, mutation = null) {
  const scratch = join(root, "shrink-attempt");
  const fails = async (candidate) => {
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
      return (await validateBindings(source, spec, out)).result === "proved under assumptions";
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
  await writeFile(
    join(destination, "verdict.json"),
    JSON.stringify(await validateBindings(minimizedSource, minimizedSpec, minimizedOut), null, 2) + "\n"
  );
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
}) {
  if (!["pattern", "independent"].includes(splitMode)) throw new Error("unknown split mode");
  if (!["pattern", "composed"].includes(grammarMode)) throw new Error("unknown grammar mode");
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
    nodeVersion: process.version,
    leanVersion,
    independentRefused: 0,
    independentRequested: { acyclic: 0, cyclic: 0, invalidAssignment: 0 },
    independentRefusedByCategory: { acyclic: 0, cyclic: 0, invalidAssignment: 0 },
    // Count cases containing each construct, rather than occurrences: a deeply
    // nested case should not hide a grammar production that rarely appears.
    astNodeCases: {},
    moduleCountCases: {},
    importCountCases: {},
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
    unsupportedCaseRefused: 0,
    invalidAssignmentCaseRefused: 0,
    compared: 0,
    inconclusive: 0,
    inconclusiveCases: [],
    equal: 0,
    different: 0,
    mutated: 0,
    mutationKinds: { binding: 0, effect: 0, function: 0, export: 0, import: 0 },
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
    let phase = "generate-ast";
    try {
      const generated = grammarMode === "composed" ? makeComposedCase(seed, index) : makeCase(seed, index);
      const input = splitMode === "independent" ? independentSplit(generated, seed, index) : generated;
      const mutation =
        input.grammar !== "composed" && !input.independentSplit && index % 8 === 2
          ? ["binding", "effect", "function", "export", "import"][Math.floor(index / 8) % 5]
          : null;
      const independentCategory = input.invalidAssignment ? "invalidAssignment" : input.cyclic ? "cyclic" : "acyclic";
      if (input.independentSplit) summary.independentRequested[independentCategory]++;
      const nodeTypes = new Set();
      const visit = (value) => {
        if (!value || typeof value !== "object") return;
        if (typeof value.type === "string") nodeTypes.add(value.type);
        for (const child of Object.values(value)) {
          if (Array.isArray(child)) child.forEach(visit);
          else visit(child);
        }
      };
      visit(input.ast);
      for (const type of nodeTypes) summary.astNodeCases[type] = (summary.astNodeCases[type] ?? 0) + 1;
      const modules = Object.values(input.spec.modules);
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
            plannedMutation: mutation !== null,
            mutation,
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
        if (input.independentSplit) {
          summary.independentRefused++;
          summary.independentRefusedByCategory[independentCategory]++;
        } else if (input.invalidAssignment) summary.invalidAssignmentCaseRefused++;
        else if (input.unsupported) summary.unsupportedCaseRefused++;
        if (input.grammar === "closure") summary.closureRefused++;
        if (input.grammar === "object") summary.objectRefused++;
        if (!input.independentSplit && !input.unsupported && !input.invalidAssignment)
          throw new Error(`supported generated case was refused: ${error}`);
        keep = false;
        continue;
      }
      if (input.invalidAssignment) throw new Error("invalid assignment unexpectedly accepted");
      summary.generated++;
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
      const equal = JSON.stringify(before) === JSON.stringify(after);
      await writeFile(join(root, "node.json"), JSON.stringify({ before, after }, null, 2) + "\n");
      summary.compared++;
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
        if (input.cyclic && verdict.result !== "unknown") throw new Error("cycle unexpectedly accepted");
        if (verdict.result === "proved under assumptions") {
          summary.proved++;
          if (input.grammar === "object") summary.objectProved++;
          if (input.grammar === "captured") summary.capturedProved++;
          if (input.grammar === "parameter") summary.parameterProved++;
          if (!equal || ((before.error || after.error) && !input.expectThrow)) {
            if (!equal) await minimizeProvedDifference(input, root, mutation);
            throw new Error("proved case differs or fails in Node");
          }
        } else if (verdict.result === "unsafe") {
          summary.unsafe++;
          const witnessCalls = verdict.calls ?? [];
          const witnessBefore = witnessCalls.length ? execute(source, witnessCalls) : before;
          const witnessAfter = witnessCalls.length ? execute(join(out, input.spec.entry), witnessCalls) : after;
          const witness = (trace) => ({
            events: trace.events,
            exports: trace.exports,
            ...(witnessCalls.length ? { results: trace.results.map((v) => (v.undefined ? null : v.value)) } : {}),
          });
          if (
            witnessBefore.error ||
            witnessAfter.error ||
            JSON.stringify(witnessBefore) === JSON.stringify(witnessAfter) ||
            JSON.stringify(verdict.original) !== JSON.stringify(witness(witnessBefore)) ||
            JSON.stringify(verdict.output) !== JSON.stringify(witness(witnessAfter))
          )
            throw new Error("unsafe witness does not match Node");
        } else if (verdict.result === "unknown") {
          summary.unknown++;
          summary.unknownCases.push({
            index,
            reason: verdict.reason,
            detail: String(verdict.detail ?? "").slice(0, 3000),
          });
        } else throw new Error(`unexpected validator result: ${verdict.result}`);
      } else summary.unvalidated++;
      keep = !equal && (verdictName === "unknown" || verdictName === "unvalidated");
      if (keep)
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
          ].includes(key)
      )
    )
      throw new Error("unknown option");
    const seed = Number(opts["--seed"] ?? 1),
      start = Number(opts["--start"] ?? 0),
      cases = Number(opts["--cases"] ?? 100),
      validateEvery = Number(opts["--validate-every"] ?? 1),
      shard = Number(opts["--shard"] ?? 0),
      shards = Number(opts["--shards"] ?? 1);
    if (
      ![seed, start, cases, validateEvery, shard, shards].every(Number.isSafeInteger) ||
      seed < 0 ||
      seed > 0xffffffff ||
      start < 0 ||
      cases < 1 ||
      validateEvery < 1 ||
      shards < 1 ||
      shard < 0 ||
      shard >= shards ||
      !opts["--output-dir"]
    )
      throw new Error(
        "usage: fuzz.mjs --output-dir DIR [--seed UINT32] [--start N] [--cases N] [--validate-every N] [--shard N --shards N] [--splits pattern|independent] [--grammar pattern|composed]"
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
