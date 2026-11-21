// The front end of the CoreJS model (`lean/Experiments/CoreJS`): acorn's tree for a module, as the model's syntax.
//
//   node to_core.mjs [--allow-host] program.mjs     one line of JSON on stdout: the program, with the modules it imports
//
// The statements of a module are a JSON array of tokens `[tag, operand…]` in postfix order: the parts of a tree come
// before its token (`Decode.lean` reads it with a stack). A program is `{entry, modules}`, each module with its
// `import`s, its exports, its tokens and the identity of each of its statements.
//
// Modules: `import { a as b } from "./m.mjs"`, `import d from`, a bare `import`, `export { a as b }`, `export` before a
// declaration and `export default`. Refused: `import * as`, `export … from`, import attributes, a name written as a
// string, and a module that is not a relative path.
//
// Identities: the top-level statements of a file are numbered in order, a declaration list counting once for each
// name, an `import` not at all. `lowering/dump_core.mjs` numbers the statements of a bundle without imports the same
// way, and `ids` gives a file other numbers: the ones its statements had in the bundle they were taken from.
//
// Anything outside the fragment is refused with the type of the node. So is a name the program does not declare and
// the host defines (`JSON`, `Error`, …), unless the model has a rule for it (`HOST_GLOBALS`): the model says nothing
// about what it holds. `allowHost` lets those through as host values, to test that the model then stops with
// "unknown".
//
// `**` is refused: the specification leaves its result to the implementation, and the C library's `pow` gives another
// last bit than V8's for some operands.
//
// The names of functions and classes (`fn.name`) are decided here, as the specification's NamedEvaluation does, and a
// program that would compute one at run time (a function under a computed key) is refused.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "acorn";

export class Refused extends Error {
  constructor(node, why) {
    super(`${node.type}: ${why}`);
    this.nodeType = node.type;
  }
}

const refuse = (node, why) => {
  throw new Refused(node, why);
};

const UNARY = { "-": "neg", "+": "pos", "!": "not", "~": "bitNot", typeof: "typeof", void: "void" };
const BINARY = {
  "+": "add",
  "-": "sub",
  "*": "mul",
  "/": "div",
  "%": "mod",
  "&": "bitAnd",
  "|": "bitOr",
  "^": "bitXor",
  "<<": "shl",
  ">>": "shr",
  ">>>": "ushr",
  "==": "eq",
  "!=": "ne",
  "===": "strictEq",
  "!==": "strictNe",
  "<": "lt",
  "<=": "le",
  ">": "gt",
  ">=": "ge",
};
const LOGICAL = { "&&": "and", "||": "or", "??": "nullish" };
const PRIMS = {
  console: { log: "log" },
  Object: {
    defineProperty: "defineProperty",
    create: "create",
    getPrototypeOf: "getPrototypeOf",
    setPrototypeOf: "setPrototypeOf",
  },
};

/** The globals of the host that the model has a rule for (`hostFunctions` and `hostNamespaces` of `Value.lean`). */
export const HOST_GLOBALS = ["Boolean", "Math", "Number", "String", "isFinite", "isNaN", "parseFloat", "parseInt"];

const view = new DataView(new ArrayBuffer(8));
/** The bits of a number, as 16 hexadecimal digits. */
export const bits = (x) => {
  view.setFloat64(0, x);
  return view.getBigUint64(0).toString(16).padStart(16, "0");
};

/** The `var` names of statements: they belong to the function around them, through every block. */
function varNames(statements) {
  const names = [];
  const visit = (s) => {
    if (!s) return;
    switch (s.type) {
      case "VariableDeclaration":
        if (s.kind === "var") for (const d of s.declarations) if (d.id.type === "Identifier") names.push(d.id.name);
        break;
      case "ExportNamedDeclaration":
      case "ExportDefaultDeclaration":
        visit(s.declaration);
        break;
      case "IfStatement":
        visit(s.consequent);
        visit(s.alternate);
        break;
      case "BlockStatement":
        s.body.forEach(visit);
        break;
      case "ForStatement":
        visit(s.init);
        visit(s.body);
        break;
      case "WhileStatement":
        visit(s.body);
        break;
      case "TryStatement":
        visit(s.block);
        visit(s.handler?.body);
        visit(s.finalizer);
        break;
    }
  };
  statements.forEach(visit);
  return names;
}

/** The names a statement list declares in its own scope: `let`, `const`, functions and classes. */
function lexicalNames(statements) {
  const names = [];
  for (const statement of statements) {
    const exported =
      (statement.type === "ExportNamedDeclaration" || statement.type === "ExportDefaultDeclaration") &&
      statement.declaration;
    const s = exported || statement;
    if (s.type === "VariableDeclaration" && s.kind !== "var") {
      for (const d of s.declarations) if (d.id.type === "Identifier") names.push(d.id.name);
    } else if ((s.type === "FunctionDeclaration" || s.type === "ClassDeclaration") && s.id) names.push(s.id.name);
  }
  return names;
}

/** The name an `import` or `export` gives; one written as a string is refused. */
const moduleName = (node) => (node.type === "Identifier" ? node.name : refuse(node, "a name written as a string"));

/**
 * The module `source` as `{ tokens, units, ids, spans, imports, exports, numbered }`: the tokens, how many statements
 * of the model the top level became, the identity of each and where its tokens begin and end,
 * `{ source, names: [[exported, local]] }` for each `import`, `[public, local]` for each export, and how many
 * statements of the file were numbered. The `k`-th numbered statement has the identity `ids[k]` when `ids` is given,
 * else `idBase + k`.
 * Throws `Refused` outside the fragment, and acorn's error on a syntax error.
 */
export function toCore(source, { allowHost = false, ids = null, idBase = 0 } = {}) {
  const ast = parse(source, { ecmaVersion: "latest", sourceType: "module" });
  const out = [];
  const emit = (...token) => out.push(token);
  const scopes = [];
  const declared = (name) => scopes.some((scope) => scope.has(name));
  const imports = [];
  const exports = [];
  const importedNames = new Set();
  /** Does `name` mean a binding that an `import` made? The module's scope is the outermost one. */
  const isImport = (name) => importedNames.has(name) && !scopes.slice(1).some((scope) => scope.has(name));
  const within = (names, body) => {
    scopes.push(new Set(names));
    try {
      return body();
    } finally {
      scopes.pop();
    }
  };

  const string = (node, s) => {
    if (/[\ud800-\udfff]/.test(s)) refuse(node, "a string outside the Basic Multilingual Plane");
    emit("str", s);
  };
  /** The name of a property written without brackets. */
  const staticKey = (node) => {
    if (node.type === "Identifier") return node.name;
    if (node.type === "Literal" && (typeof node.value === "string" || typeof node.value === "number"))
      return String(node.value);
    return refuse(node, "a property key");
  };
  const key = (node, computed) => (computed ? expr(node) : string(node, staticKey(node)));
  const isAnonymousFunction = (node) =>
    (node.type === "FunctionExpression" ||
      node.type === "ArrowFunctionExpression" ||
      node.type === "ClassExpression") &&
    !node.id;

  function func(node, name, kind) {
    if (node.async || node.generator) refuse(node, "an async function or a generator");
    const params = node.params.map((p) =>
      p.type === "Identifier" ? p.name : refuse(p, "a parameter that is not a name")
    );
    const self = node.type === "FunctionExpression" && kind === "normal" && node.id !== null;
    within(self ? [node.id.name] : [], () => {
      if (node.body.type !== "BlockStatement") {
        within(params, () => expr(node.body));
        emit("ret");
        emit("func", name, kind, self, params, 1);
      } else {
        const body = node.body.body;
        const count = within([...params, ...varNames(body), ...lexicalNames(body)], () => statements(body));
        emit("func", name, kind, self, params, count);
      }
    });
  }

  function classTree(node, name) {
    if (node.superClass) refuse(node, "a class with extends");
    const self = node.id !== null;
    within(self ? [node.id.name] : [], () => {
      const ctor = node.body.body.find((m) => m.type === "MethodDefinition" && m.kind === "constructor");
      if (ctor) func(ctor.value, name, "classCtor");
      else emit("func", name, "classCtor", false, [], 0);
      let count = 0;
      for (const member of node.body.body) {
        if (member === ctor) continue;
        if (member.type === "StaticBlock") refuse(member, "a static block");
        if (member.key.type === "PrivateIdentifier") refuse(member, "a private name");
        count++;
        if (member.type === "PropertyDefinition") {
          if (member.computed && member.value && isAnonymousFunction(member.value))
            refuse(member, "a function named by a computed key");
          key(member.key, member.computed);
          if (member.value) named(member.value, member.computed ? "" : staticKey(member.key));
          else emit("undef");
          emit("field", member.static);
        } else {
          if (member.computed) refuse(member, "a method named by a computed key");
          const k = staticKey(member.key);
          key(member.key, false);
          if (member.kind === "method") {
            func(member.value, k, "method");
            emit("method", member.static);
          } else {
            func(member.value, `${member.kind} ${k}`, "method");
            emit(member.kind === "get" ? "classGetter" : "classSetter", member.static);
          }
        }
      }
      emit("class", name, self, count);
    });
  }

  /** An expression that gives an anonymous function or class the name `name`. */
  function named(node, name) {
    if (!isAnonymousFunction(node)) return expr(node);
    if (node.type === "ClassExpression") return classTree(node, name);
    func(node, name, node.type === "ArrowFunctionExpression" ? "arrow" : "normal");
    emit("funcExpr");
  }

  function target(node) {
    if (node.type === "Identifier") {
      if (!declared(node.name) && node.name in globalThis)
        refuse(node, `an assignment to the host global ${node.name}`);
      emit(isImport(node.name) ? "targetFrozen" : "targetVar", node.name);
    } else if (node.type === "MemberExpression") {
      member(node);
      emit("targetMember");
    } else refuse(node, "an assignment target");
  }

  /** The object and the key of a property access. */
  function member(node) {
    if (node.optional) refuse(node, "an optional chain");
    if (node.object.type === "Super") refuse(node, "super");
    if (node.property.type === "PrivateIdentifier") refuse(node, "a private name");
    expr(node.object);
    key(node.property, node.computed);
  }

  function args(nodes) {
    for (const a of nodes) {
      if (a.type === "SpreadElement") refuse(a, "a spread argument");
      expr(a);
    }
    return nodes.length;
  }

  /** The model's primitive that a call of `host.name(…)` is, if `host` is not a name of the program. */
  const primOf = (callee) =>
    callee.type === "MemberExpression" &&
    !callee.computed &&
    !callee.optional &&
    callee.object.type === "Identifier" &&
    !declared(callee.object.name) &&
    callee.property.type === "Identifier"
      ? PRIMS[callee.object.name]?.[callee.property.name]
      : undefined;

  function expr(node) {
    switch (node.type) {
      case "Literal":
        if (node.regex || node.bigint) refuse(node, "a regular expression or a BigInt");
        if (node.value === null) emit("null");
        else if (typeof node.value === "boolean") emit("bool", node.value);
        else if (typeof node.value === "number") emit("num", bits(node.value));
        else string(node, node.value);
        break;
      case "TemplateLiteral":
        string(node, node.quasis[0].value.cooked);
        node.expressions.forEach((e, i) => {
          expr(e);
          emit("unary", "toString");
          emit("binary", "add");
          string(node, node.quasis[i + 1].value.cooked);
          emit("binary", "add");
        });
        break;
      case "Identifier":
        if (declared(node.name)) emit("var", node.name);
        else if (node.name === "undefined") emit("undef");
        else if (node.name === "NaN" || node.name === "Infinity") emit("num", bits(globalThis[node.name]));
        else if (node.name === "arguments") refuse(node, "arguments");
        else if (!(node.name in globalThis)) emit("var", node.name);
        else if (allowHost || HOST_GLOBALS.includes(node.name)) emit("host", node.name);
        else refuse(node, `the host global ${node.name}`);
        break;
      case "ThisExpression":
        emit("this");
        break;
      case "FunctionExpression":
        func(node, node.id?.name ?? "", "normal");
        emit("funcExpr");
        break;
      case "ArrowFunctionExpression":
        func(node, "", "arrow");
        emit("funcExpr");
        break;
      case "ClassExpression":
        classTree(node, node.id?.name ?? "");
        break;
      case "ObjectExpression":
        for (const p of node.properties) {
          if (p.type === "SpreadElement") refuse(p, "a spread property");
          if (p.kind !== "init") {
            if (p.computed) refuse(p, "an accessor named by a computed key");
            key(p.key, false);
            func(p.value, `${p.kind} ${staticKey(p.key)}`, "method");
            emit(p.kind === "get" ? "getter" : "setter");
          } else if (!p.computed && !p.shorthand && !p.method && staticKey(p.key) === "__proto__") {
            expr(p.value);
            emit("proto");
          } else {
            if (p.computed && (p.method || isAnonymousFunction(p.value)))
              refuse(p, "a function named by a computed key");
            key(p.key, p.computed);
            if (p.method) {
              func(p.value, staticKey(p.key), "method");
              emit("funcExpr");
            } else named(p.value, p.computed ? "" : staticKey(p.key));
            emit("init");
          }
        }
        emit("obj", node.properties.length);
        break;
      case "ArrayExpression":
        for (const element of node.elements) {
          if (element === null) refuse(node, "an array with a hole");
          if (element.type === "SpreadElement") refuse(element, "a spread element");
          expr(element);
        }
        emit("arr", node.elements.length);
        break;
      case "MemberExpression":
        member(node);
        emit("get");
        break;
      case "CallExpression": {
        if (node.optional) refuse(node, "an optional call");
        const callee = node.callee;
        const prim = primOf(callee);
        if (prim) emit("prim", prim, args(node.arguments));
        else if (callee.type === "Identifier" && callee.name === "String" && !declared("String")) {
          if (node.arguments.length !== 1 || node.arguments[0].type === "SpreadElement")
            refuse(node, "String with other than one argument");
          expr(node.arguments[0]);
          emit("unary", "toString");
        } else if (callee.type === "MemberExpression") {
          member(callee);
          emit("callMethod", args(node.arguments));
        } else if (callee.type === "Super" || callee.type === "Import") refuse(node, "a super call or an import");
        else {
          expr(callee);
          emit("call", args(node.arguments));
        }
        break;
      }
      case "NewExpression":
        expr(node.callee);
        emit("new", args(node.arguments));
        break;
      case "UnaryExpression":
        if (node.operator === "delete") {
          if (node.argument.type !== "MemberExpression") refuse(node, "delete of something that is not a property");
          member(node.argument);
          emit("delete");
        } else {
          expr(node.argument);
          emit("unary", UNARY[node.operator] ?? refuse(node, `the operator ${node.operator}`));
        }
        break;
      case "BinaryExpression":
        if (node.left.type === "PrivateIdentifier") refuse(node, "a private name");
        expr(node.left);
        expr(node.right);
        if (node.operator === "in") emit("hasProp");
        else emit("binary", BINARY[node.operator] ?? refuse(node, `the operator ${node.operator}`));
        break;
      case "LogicalExpression":
        expr(node.left);
        expr(node.right);
        emit("logical", LOGICAL[node.operator]);
        break;
      case "ConditionalExpression":
        expr(node.test);
        expr(node.consequent);
        expr(node.alternate);
        emit("cond");
        break;
      case "SequenceExpression":
        node.expressions.forEach((e, i) => {
          expr(e);
          if (i > 0) emit("seq");
        });
        break;
      case "AssignmentExpression": {
        target(node.left);
        const name = node.left.type === "Identifier" ? node.left.name : null;
        const operator = node.operator.slice(0, -1);
        if (operator === "") {
          if (name === null) expr(node.right);
          else named(node.right, name);
          emit("assign");
        } else if (operator in LOGICAL) {
          if (name === null) expr(node.right);
          else named(node.right, name);
          emit("assignLogical", LOGICAL[operator]);
        } else {
          expr(node.right);
          emit("assignOp", BINARY[operator] ?? refuse(node, `the operator ${node.operator}`));
        }
        break;
      }
      case "UpdateExpression":
        target(node.argument);
        emit("update", node.operator === "++", node.prefix);
        break;
      default:
        refuse(node, "outside the fragment");
    }
  }

  /** One declaration statement for each name of a declaration list; the count. */
  function declaration(node) {
    for (const d of node.declarations) {
      if (d.id.type !== "Identifier") refuse(d.id, "a destructuring pattern");
      if (d.init) named(d.init, d.id.name);
      else if (node.kind === "var") {
        emit("varDecl", d.id.name);
        continue;
      } else emit("undef");
      emit("decl", node.kind, d.id.name);
    }
    return node.declarations.length;
  }

  /** The statement `node`, as exactly one statement of the model. */
  function single(node) {
    const count = statement(node);
    if (count !== 1) emit("block", count);
  }

  function block(node) {
    emit(
      "block",
      within(lexicalNames(node.body), () => statements(node.body))
    );
  }

  const statements = (nodes) => nodes.reduce((count, node) => count + statement(node), 0);

  /** The statement `node`; the count of the model's statements it became. */
  function statement(node) {
    switch (node.type) {
      case "ExpressionStatement":
        expr(node.expression);
        emit("expr");
        return 1;
      case "VariableDeclaration":
        return declaration(node);
      case "FunctionDeclaration":
        func(node, node.id.name, "normal");
        emit("funDecl");
        return 1;
      case "ClassDeclaration":
        classTree(node, node.id.name);
        emit("decl", "let", node.id.name);
        return 1;
      case "ReturnStatement":
        if (node.argument) expr(node.argument);
        else emit("undef");
        emit("ret");
        return 1;
      case "IfStatement":
        expr(node.test);
        single(node.consequent);
        if (node.alternate) single(node.alternate);
        else emit("block", 0);
        emit("ite");
        return 1;
      case "BlockStatement":
        block(node);
        return 1;
      case "ForStatement": {
        const init = node.init;
        const isDeclaration = init?.type === "VariableDeclaration";
        if (isDeclaration && init.declarations.length !== 1) refuse(init, "a for loop that declares several names");
        within(isDeclaration && init.kind !== "var" ? lexicalNames([init]) : [], () => {
          if (isDeclaration) single(init);
          else if (init) {
            expr(init);
            emit("expr");
          } else emit("block", 0);
          if (node.test) expr(node.test);
          else emit("bool", true);
          if (node.update) expr(node.update);
          else emit("undef");
          single(node.body);
        });
        emit("loop");
        return 1;
      }
      case "WhileStatement":
        emit("block", 0);
        expr(node.test);
        emit("undef");
        single(node.body);
        emit("loop");
        return 1;
      case "BreakStatement":
      case "ContinueStatement":
        if (node.label) refuse(node, "a label");
        emit(node.type === "BreakStatement" ? "brk" : "cont");
        return 1;
      case "ThrowStatement":
        expr(node.argument);
        emit("throw");
        return 1;
      case "TryStatement":
        block(node.block);
        if (node.handler) {
          const param = node.handler.param;
          if (param && param.type !== "Identifier") refuse(param, "a destructuring pattern");
          within(param ? [param.name] : [], () => block(node.handler.body));
          emit("tryCatch", param ? param.name : null);
        }
        if (node.finalizer) {
          block(node.finalizer);
          emit("tryFinally");
        }
        return 1;
      case "EmptyStatement":
      case "DebuggerStatement":
        return 0;
      default:
        return refuse(node, "outside the fragment");
    }
  }

  function importDeclaration(node) {
    if (node.attributes?.length) refuse(node, "an import attribute");
    if (!/^\.\.?\//.test(node.source.value)) refuse(node, "an import of a module that is not a relative path");
    const names = node.specifiers.map((specifier) => {
      if (specifier.type === "ImportNamespaceSpecifier") refuse(specifier, "a namespace import");
      const exported = specifier.type === "ImportDefaultSpecifier" ? "default" : moduleName(specifier.imported);
      return [exported, specifier.local.name];
    });
    imports.push({ source: node.source.value, names });
  }

  const unitIds = [];
  const spans = [];
  let numbered = 0;
  /** The next numbered statement of the file, which `emitStatement` writes: at most one statement of the model. */
  function unit(node, emitStatement) {
    const id = ids === null ? idBase + numbered : ids[numbered];
    numbered++;
    const start = out.length;
    const count = emitStatement();
    if (count > 1) refuse(node, "a top-level statement that is several statements of the model");
    if (count === 1) {
      if (id === undefined) refuse(node, "a statement without an identity");
      unitIds.push(id);
      spans.push([start, out.length]);
    }
  }

  /** A top-level statement or declaration of the module. */
  function topLevel(node) {
    switch (node.type) {
      case "ImportDeclaration":
        return importDeclaration(node);
      case "VariableDeclaration":
        for (const d of node.declarations) unit(d, () => declaration({ ...node, declarations: [d] }));
        return;
      case "ExportNamedDeclaration":
        if (node.source) refuse(node, "an export from another module");
        if (node.attributes?.length) refuse(node, "an import attribute");
        if (node.declaration) {
          const d = node.declaration;
          topLevel(d);
          const own = d.type === "VariableDeclaration" ? d.declarations.map((x) => x.id.name) : [d.id.name];
          for (const name of own) exports.push([name, name]);
        } else {
          for (const s of node.specifiers) exports.push([moduleName(s.exported), moduleName(s.local)]);
          unit(node, () => 0);
        }
        return;
      case "ExportDefaultDeclaration": {
        const d = node.declaration;
        if (d.type === "FunctionDeclaration") {
          // a declared function is bound under its name; one without a name under `default`, which is its `name`
          const name = d.id?.name ?? "default";
          unit(node, () => {
            func(d, name, "normal");
            emit("funDecl");
            return 1;
          });
          exports.push(["default", name]);
        } else {
          const isClass = d.type === "ClassDeclaration";
          const name = isClass && d.id ? d.id.name : "*default*";
          unit(node, () => {
            if (isClass) classTree(d, d.id?.name ?? "default");
            else named(d, "default");
            emit("decl", isClass ? "let" : "const", name);
            return 1;
          });
          exports.push(["default", name]);
        }
        return;
      }
      default:
        return unit(node, () => statement(node));
    }
  }

  for (const node of ast.body)
    if (node.type === "ImportDeclaration") for (const s of node.specifiers) importedNames.add(s.local.name);
  within([...varNames(ast.body), ...lexicalNames(ast.body), ...importedNames], () => ast.body.forEach(topLevel));
  return { tokens: out, units: unitIds.length, ids: unitIds, spans, imports, exports, numbered };
}

/**
 * The program that starts at the file `entry`, with every module it reaches: `{ entry, modules }` as `Decode.lean`
 * reads it. A module's name is its path from the directory of the entry. `ids[name]` gives the identities of the
 * statements of that module; without it the statements are numbered on through the modules, in the order they are read.
 */
export function toCoreProgram(entry, { allowHost = false, ids = {} } = {}) {
  const root = path.dirname(entry);
  const modules = [];
  const seen = new Set();
  let idBase = 0;
  const load = (name) => {
    if (seen.has(name)) return;
    seen.add(name);
    const source = readFileSync(path.join(root, name), "utf8");
    const translated = toCore(source, { allowHost, ids: ids[name] ?? null, idBase });
    idBase += translated.numbered;
    const imports = translated.imports.map((i) => ({
      source: path.posix.join(path.posix.dirname(name), i.source),
      names: i.names,
    }));
    modules.push({ ...translated, name, imports });
    for (const i of imports) load(i.source);
  };
  load(path.basename(entry));
  return { entry: path.basename(entry), modules };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const rest = process.argv.slice(2).filter((a) => a !== "--allow-host");
  console.log(JSON.stringify(toCoreProgram(rest[0], { allowHost: process.argv.includes("--allow-host") })));
}
