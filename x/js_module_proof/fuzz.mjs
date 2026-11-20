import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
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
      return `${print(node.object)}.${print(node.property)}`;
    case "ExportNamedDeclaration":
      return `export { ${node.specifiers.map(print).join(", ")} };`;
    case "ExportSpecifier":
      return print(node.local);
    case "IfStatement":
      return `if (${print(node.test)}) ${print(node.consequent)}${node.alternate ? ` else ${print(node.alternate)}` : ""}`;
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
    writesCaptured: writesCaptured || controlFlow,
    controlFlow,
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
  const multiply = Math.floor(index / 32) % 2 === 1;
  const left = {
    type: "BinaryExpression",
    operator: multiply ? "*" : "+",
    left: id(parameters[0]),
    right: id(parameters[1]),
  };
  const value = call(id("helper"), [left]);
  const thrownValue = ["generated", false, null, 1 + rand(8)][Math.floor(index / 32) % 4];
  const firstArgument = 1 + rand(8),
    secondArgument = 1 + rand(8),
    condition = 1 + rand(8);
  const observedThrow =
    typeof thrownValue === "number"
      ? (multiply ? secondArgument * condition : secondArgument + condition) + initial + condition
      : thrownValue;
  const expectedThrown =
    observedThrow === null ? { kind: "null" } : { kind: typeof observedThrow, value: observedThrow };
  const guard = parameters[0] === "x";
  const fallback = [
    declaration("base", literal(2)),
    returnNode({ type: "BinaryExpression", operator: "+", left: id("base"), right: id("y") }),
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
                left: { type: "BinaryExpression", operator: "<", left: literal(0), right: id("y") },
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
  const modules = {
    "state.mjs": { imports: [], statements: [0, 1] },
    "logic.mjs": { imports: ["state.mjs"], statements: [2] },
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
  const count = 2 + rand(2);
  const unsafe = index % 128 === 92;
  const target = unsafe || rand(2) === 0 ? "value" : "start";
  const factory = functionNode(
    "make",
    ["start"],
    [
      declaration("value", { type: "BinaryExpression", operator: "+", left: id("start"), right: id("seed") }),
      returnNode({
        type: "ArrowFunctionExpression",
        params: [id("step")],
        async: false,
        body: { type: "BlockStatement", body: [add(target, "step"), returnNode(id(target))] },
      }),
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

// Generate declarations and effects independently of their module placement.
// Mode changes how effects are distributed, not the program grammar.
export function makeCase(seed, index) {
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

async function minimizeProvedDifference(input, root) {
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
export async function runFuzz({ seed, start = 0, cases, outputDir, validateEvery = 1, shard = 0, shards = 1 }) {
  if (!Number.isSafeInteger(shards) || shards < 1 || !Number.isSafeInteger(shard) || shard < 0 || shard >= shards)
    throw new Error("shard must be an integer between 0 and shards - 1");
  await mkdir(outputDir, { recursive: true });
  const summary = {
    seed,
    start,
    shard,
    shards,
    cases: 0,
    constantCases: 0,
    capturedCases: 0,
    capturedCompared: 0,
    capturedProved: 0,
    capturedUnsafe: 0,
    parameterCases: 0,
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
    branchThen: 0,
    branchElse: 0,
    generated: 0,
    refused: 0,
    compared: 0,
    inconclusive: 0,
    inconclusiveCases: [],
    equal: 0,
    different: 0,
    mutated: 0,
    splitCompared: 0,
    splitDifferent: 0,
    mutatedDifferent: 0,
    validated: 0,
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
    const source = join(root, "source.mjs");
    const spec = join(root, "spec.json");
    const out = join(root, "out");
    const input = makeCase(seed, index);
    let keep = true;
    try {
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
            nodeVersion: process.version,
          },
          null,
          2
        ) + "\n"
      );
      if (input.useConstant) summary.constantCases++;
      if (input.grammar === "captured") summary.capturedCases++;
      if (input.grammar === "parameter") summary.parameterCases++;
      if (input.grammar === "modeledClosure") {
        summary.modeledClosureCases++;
        if (input.crossModuleCapture) summary.modeledClosureCrossModule++;
      }
      if (input.grammar === "closure") {
        summary.closureCases++;
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
        if (input.controlFlow) {
          summary.controlFlowCases++;
          if (observed.events.includes(101)) summary.branchThen++;
          if (observed.events.includes(102)) summary.branchElse++;
          if ((!observed.events.includes(101) && !observed.events.includes(102)) || !observed.events.includes(0))
            throw new Error("generated control flow did not run");
        }
      }
      try {
        await generateBindings(source, spec, out);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
        summary.refused++;
        if (input.grammar === "closure") summary.closureRefused++;
        if (!input.unsupported && !input.invalidAssignment)
          throw new Error(`supported generated case was refused: ${error}`);
        keep = false;
        continue;
      }
      if (input.invalidAssignment) throw new Error("invalid assignment unexpectedly accepted");
      summary.generated++;
      if (input.grammar === "closure") summary.closureGenerated++;
      const mutated = index % 8 === 2;
      if (mutated) {
        const file = join(out, input.cell0Owner);
        const original = await readFile(file, "utf8");
        const changed = original.replace(/let cell0 = (\d+);/, (_, n) => `let cell0 = ${Number(n) + 1};`);
        if (changed === original) throw new Error("mutation failed");
        await writeFile(file, changed);
        summary.mutated++;
      }
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
      if (input.grammar === "captured") {
        summary.capturedCompared++;
        if (equal === input.expectUnsafe) throw new Error("captured split differed from intended Node behavior");
      }
      if (input.grammar === "closure") summary.closureCompared++;
      if (input.grammar === "modeledClosure") {
        summary.modeledClosureCompared++;
        if (before.results[0]?.value !== 1 || !(before.results[1]?.value > 1))
          throw new Error("generated closure branch did not execute both outcomes");
        summary.modeledClosureBranches++;
        if (equal === input.expectUnsafe || (!input.expectThrow && (before.error || after.error)))
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
        const verdict = await validateBindings(source, spec, out);
        summary.validated++;
        verdictName = verdict.result;
        await writeFile(join(root, "verdict.json"), JSON.stringify(verdict, null, 2) + "\n");
        if (input.grammar === "captured") {
          const expected = input.expectUnsafe ? "unsafe" : "proved under assumptions";
          if (verdict.result !== expected)
            throw new Error(`captured validation expected ${expected}, got ${verdict.result}: ${verdict.reason ?? ""}`);
          if (input.expectUnsafe) summary.capturedUnsafe++;
        }
        if (input.grammar === "modeledClosure") {
          const expected = input.expectUnsafe ? "unsafe" : "proved under assumptions";
          if (verdict.result !== expected)
            throw new Error(`closure validation expected ${expected}, got ${verdict.result}: ${verdict.reason ?? ""}`);
          if (input.expectUnsafe) summary.modeledClosureUnsafe++;
          else summary.modeledClosureProved++;
        }
        if (input.cyclic && verdict.result !== "unknown") throw new Error("cycle unexpectedly accepted");
        if (verdict.result === "proved under assumptions") {
          summary.proved++;
          if (input.grammar === "captured") summary.capturedProved++;
          if (input.grammar === "parameter") summary.parameterProved++;
          if (!equal || ((before.error || after.error) && !input.expectThrow)) {
            if (!equal && !mutated) await minimizeProvedDifference(input, root);
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
      if (keep) summary.unexplainedDifferences.push({ index, mutated, verdict: verdictName, artifacts: root });
    } catch (error) {
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
          !["--seed", "--start", "--cases", "--output-dir", "--validate-every", "--shard", "--shards"].includes(key)
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
        "usage: fuzz.mjs --output-dir DIR [--seed UINT32] [--start N] [--cases N] [--validate-every N] [--shard N --shards N]"
      );
    console.log(
      JSON.stringify(
        await runFuzz({ seed, start, cases, validateEvery, shard, shards, outputDir: resolve(opts["--output-dir"]) }),
        null,
        2
      )
    );
  } catch (error) {
    console.error(error.stack ?? String(error));
    process.exitCode = 1;
  }
}
