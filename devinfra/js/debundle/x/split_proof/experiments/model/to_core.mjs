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
import { parseModule } from "../translation/scope_metadata.mjs";

export class Refused extends Error {
  constructor(node, why) {
    super(`${node.type}: ${why}`);
    this.nodeType = node.type;
  }
}

const refuse = (node, why) => {
  throw new Refused(node, why);
};

export const UNARY = { "-": "neg", "+": "pos", "!": "not", "~": "bitNot", typeof: "typeof", void: "void" };
export const BINARY = {
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
export const LOGICAL = { "&&": "and", "||": "or", "??": "nullish" };
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

// Flat, nonempty var object patterns can use ordinary CoreJS property reads.
// Empty patterns still require an explicit null/undefined check.
const flatObjectVar = (kind, pattern) =>
  kind === "var" &&
  pattern.type === "ObjectPattern" &&
  pattern.properties.length > 0 &&
  pattern.properties.every((p) => p.type === "Property" && !p.computed && p.value.type === "Identifier");

/** JavaScript binding names, including declarations handled by the opaque fallback. */
function bindingNames(pattern) {
  switch (pattern.type) {
    case "Identifier":
      return [pattern.name];
    case "ObjectPattern":
      return pattern.properties.flatMap((p) => bindingNames(p.type === "RestElement" ? p.argument : p.value));
    case "ArrayPattern":
      return pattern.elements.flatMap((p) => (p ? bindingNames(p) : []));
    case "RestElement":
      return bindingNames(pattern.argument);
    case "AssignmentPattern":
      return bindingNames(pattern.left);
    default:
      return refuse(pattern, "a binding pattern");
  }
}

/** The `var` names of statements: they belong to the function around them, through every block. */
function varNames(statements) {
  const names = [];
  const visit = (s) => {
    if (!s) return;
    switch (s.type) {
      case "VariableDeclaration":
        if (s.kind === "var") for (const d of s.declarations) names.push(...bindingNames(d.id));
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
      for (const d of s.declarations) names.push(...bindingNames(d.id));
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
 *
 * `opaque`, eslint-scope's module, turns a top-level statement outside the fragment into an opaque statement, which
 * the model's world runs: it carries the names the statement declares and the other names of the module scope or of
 * the host that it mentions, both from eslint-scope. Then `opaque` in the result lists each such statement with why,
 * and a name the program does not declare is the host's in every statement. Refused all the same: a top-level `await`
 * (the order of evaluation would change), a direct `eval` (its names are not known from the text), and what depends
 * on the file the code is in: `import.meta`, `import()`. With `census`, such a statement does not end the translation:
 * it is listed in `refused`, and the result is not a program to run. `external` takes the module's `import`s for the host's: the
 * names they bind are not declared, and the result has no imports. Each `import` still takes an identity, with no
 * statement, as an `export { … }` list does, so that identities count the file's statements as `lowersplit` does.
 */
export function toCore(source, { opaque = null, ...options } = {}) {
  return coreFromParsed(parseModule(source, parse, opaque), options);
}

export function coreFromParsed(
  { ast, scopes: resolvedScopes, units },
  { allowHost = false, ids = null, idBase = 0, external = false, census = false, opaque = true } = {}
) {
  const scopeManager = opaque ? resolvedScopes : null;
  const opaqueUnits = [];
  const opaqueBodies = [];
  let activeUnit = null;
  let functionParts = new Map();
  const refused = [];
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

  function func(node, name, kind, allowOpaqueBody = false) {
    const start = out.length;
    const bodyStart = opaqueBodies.length;
    try {
      return modeledFunc(node, name, kind);
    } catch (e) {
      if (!(e instanceof Refused) || !scopeManager || !allowOpaqueBody || node.async || node.generator) throw e;
      // These assumptions concern correspondence, even though the body is deferred.
      checkOpaque(node);
      const visit = (n) => {
        if (!n || typeof n !== "object") return;
        if (n.type === "Super") refuse(n, "super needs a method home object");
        if (n.type === "MetaProperty" && n.meta.name === "new") refuse(n, "opaque invocation lacks new.target");
        for (const value of Object.values(n)) {
          if (Array.isArray(value)) value.forEach(visit);
          else if (value && typeof value === "object") visit(value);
        }
      };
      visit(node);
      const scope = scopeManager.acquire(node, true);
      if (!scope || activeUnit === null || !functionParts.has(node)) throw e;
      const free = [
        ...new Set(
          [...scope.through].sort((a, b) => a.identifier.start - b.identifier.start).map((ref) => ref.identifier.name)
        ),
      ];
      out.length = start;
      opaqueBodies.length = bodyStart;
      const part = functionParts.get(node);
      const self = node.type === "FunctionExpression" && kind === "normal" && node.id !== null;
      emit("opaqueFunc", name, kind, self, paramLength(node.params), activeUnit, part, free);
      opaqueBodies.push({ id: activeUnit, part, why: e.message });
    }
  }

  function modeledFunc(node, name, kind) {
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
    if (node.superClass && node.body.body.some((m) => m.kind === "constructor" || m.type === "PropertyDefinition"))
      refuse(node, "a derived class with an explicit constructor or fields");
    const self = node.id !== null;
    within(self ? [node.id.name] : [], () => {
      const ctor = node.body.body.find((m) => m.type === "MethodDefinition" && m.kind === "constructor");
      if (node.superClass) expr(node.superClass);
      else if (ctor) func(ctor.value, name, "classCtor", true);
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
            func(member.value, k, "method", true);
            emit("method", member.static);
          } else {
            func(member.value, `${member.kind} ${k}`, "method", true);
            emit(member.kind === "get" ? "classGetter" : "classSetter", member.static);
          }
        }
      }
      emit(node.superClass ? "derivedClass" : "class", name, self, count);
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
      if (!declared(node.name) && (scopeManager || node.name in globalThis))
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
        else if (scopeManager && !HOST_GLOBALS.includes(node.name))
          refuse(node, `the name ${node.name}, which the program does not declare`);
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
      if (flatObjectVar(node.kind, d.id)) {
        // A getter or an earlier target can rebind the RHS name. Cache the value once
        // in a block-local cell whose name cannot coincide with any JS identifier.
        const object = "*destructure*";
        expr(d.init);
        emit("decl", "const", object);
        for (const p of d.id.properties) {
          emit("var", object);
          key(p.key, false);
          emit("get");
          emit("decl", "var", p.value.name);
        }
        emit("block", 1 + d.id.properties.length);
        continue;
      }
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
  /** The next numbered statement of the file, which `emitStatement` writes: at most one statement of the model. */
  function unit(id, node, emitStatement) {
    activeUnit = id;
    // Assign every lexical function a part before lowering. Fallback and statement reordering cannot renumber it.
    const functions = [];
    const visit = (n) => {
      if (!n || typeof n !== "object") return;
      if (["FunctionExpression", "FunctionDeclaration", "ArrowFunctionExpression"].includes(n.type)) functions.push(n);
      for (const value of Object.values(n)) {
        if (Array.isArray(value)) value.forEach(visit);
        else if (value && typeof value === "object") visit(value);
      }
    };
    visit(node);
    functionParts = new Map(functions.sort((a, b) => a.start - b.start).map((f, i) => [f, i + 1]));
    const bodyStart = opaqueBodies.length;
    const start = out.length;
    let count;
    try {
      count = emitStatement();
    } catch (e) {
      if (!(e instanceof Refused) || !scopeManager) throw e;
      out.length = start;
      opaqueBodies.length = bodyStart;
      try {
        checkOpaque(node);
      } catch (r) {
        if (!(r instanceof Refused) || !census) throw r;
        refused.push({ id, why: r.message });
      }
      opaqueStatement(node, id);
      opaqueUnits.push({ id, why: e.message });
      count = 1;
    }
    if (count > 1) refuse(node, "a top-level statement that is several statements of the model");
    if (count === 1) {
      if (id === undefined) refuse(node, "a statement without an identity");
      unitIds.push(id);
      spans.push([start, out.length]);
    }
  }

  /** A top-level statement or declaration of the module. */
  function topLevel(node, id) {
    switch (node.type) {
      case "VariableDeclaration":
        if (node.kind === "using" || node.kind === "await using") refuse(node, "a top-level using");
        return unit(id, node.declarations[0], () => declaration(node));
      case "ExportNamedDeclaration":
        if (node.source) refuse(node, "an export from another module");
        if (node.attributes?.length) refuse(node, "an import attribute");
        if (node.declaration) {
          const d = node.declaration;
          topLevel(d, id);
          const own =
            d.type === "VariableDeclaration" ? d.declarations.flatMap((x) => bindingNames(x.id)) : [d.id.name];
          for (const name of own) exports.push([name, name]);
        } else {
          for (const s of node.specifiers) exports.push([moduleName(s.exported), moduleName(s.local)]);
          unit(id, node, () => 0);
        }
        return;
      case "ExportDefaultDeclaration": {
        const d = node.declaration;
        if (d.type === "FunctionDeclaration") {
          // a declared function is bound under its name; one without a name under `default`, which is its `name`
          const name = d.id?.name ?? "default";
          unit(id, node, () => {
            func(d, name, "normal");
            emit("funDecl");
            return 1;
          });
          exports.push(["default", name]);
        } else {
          const isClass = d.type === "ClassDeclaration";
          const name = isClass && d.id ? d.id.name : "*default*";
          unit(id, node, () => {
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
        return unit(id, node, () => statement(node));
    }
  }

  /** The module scope's declarations and the references to it or to the host, each sorted by position. */
  const moduleScope = scopeManager?.scopes.find((scope) => scope.type === "module");
  const defs = [];
  const refs = [];
  if (scopeManager) {
    for (const v of moduleScope.variables)
      for (const def of v.defs)
        if (!(external && def.type === "ImportBinding")) defs.push({ at: def.name.start, v, def });
    for (const scope of scopeManager.scopes)
      for (const ref of scope.references) {
        const where = ref.resolved?.scope.type;
        if (where === undefined || where === "module" || where === "global")
          refs.push({ at: ref.identifier.start, name: ref.identifier.name });
      }
    defs.sort((a, b) => a.at - b.at);
    refs.sort((a, b) => a.at - b.at);
  }
  /** The entries of a sorted list that lie in `[start, end)`. */
  const within_ = (list, { start, end }) => {
    let lo = 0;
    let hi = list.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (list[mid].at < start) lo = mid + 1;
      else hi = mid;
    }
    const found = [];
    for (let i = lo; i < list.length && list[i].at < end; i++) found.push(list[i]);
    return found;
  };
  const paramLength = (params) => {
    const k = params.findIndex((p) => p.type === "AssignmentPattern" || p.type === "RestElement");
    return k < 0 ? params.length : k;
  };
  /** What the model cannot leave to the world: refused in an opaque statement as anywhere. */
  function checkOpaque(root) {
    const visit = (node, inFunction) => {
      if (!node || typeof node.type !== "string") return;
      if (node.type === "MetaProperty" && node.meta.name === "import") refuse(node, "import.meta");
      if (node.type === "ImportExpression") refuse(node, "import(), which resolves against the file it is in");
      if (!inFunction && (node.type === "AwaitExpression" || (node.type === "ForOfStatement" && node.await)))
        refuse(node, "a top-level await");
      if (!inFunction && node.type === "VariableDeclaration" && (node.kind === "using" || node.kind === "await using"))
        refuse(node, "a top-level using");
      if (node.type === "CallExpression" && node.callee.type === "Identifier" && node.callee.name === "eval")
        refuse(node, "a direct eval");
      const fn = inFunction || /Function/.test(node.type) || node.type === "StaticBlock";
      for (const key of Object.keys(node)) {
        const child = node[key];
        if (Array.isArray(child)) for (const c of child) visit(c, fn);
        else if (child && typeof child === "object") visit(child, fn);
      }
    };
    visit(root, false);
  }
  /** The top-level statement or declarator `node` as an opaque statement with the identity `id`. */
  function opaqueStatement(node, id) {
    const decls = [];
    const funs = [];
    const seen = new Set();
    for (const { v, def } of within_(defs, node)) {
      if (seen.has(v.name)) continue;
      seen.add(v.name);
      if (def.type === "FunctionName")
        funs.push([v.name, paramLength(def.node.params), !(def.node.async || def.node.generator)]);
      else if (def.type === "ClassName") decls.push([v.name, "let"]);
      else if (def.type === "Variable") decls.push([v.name, def.parent.kind === "let" ? "let" : def.parent.kind]);
      else refuse(node, `a declaration of kind ${def.type}`);
    }
    if (node.type === "ExportDefaultDeclaration") {
      const d = node.declaration;
      if (d.type === "FunctionDeclaration" && !d.id)
        funs.push(["default", paramLength(d.params), !(d.async || d.generator)]);
      else if (d.type === "ClassDeclaration" && !d.id) decls.push(["*default*", "let"]);
      else if (d.type !== "FunctionDeclaration" && d.type !== "ClassDeclaration") decls.push(["*default*", "const"]);
      for (const [x] of [...decls, ...funs]) seen.add(x);
    }
    const free = [];
    for (const { name } of within_(refs, node))
      if (!seen.has(name)) {
        seen.add(name);
        free.push(name);
      }
    emit("opaque", id, decls, funs, free, closures(node, decls, funs));
  }
  /**
   * What the opaque statement `node` makes when it runs, if it only makes functions, else `null` (the world runs it).
   * A function declaration makes nothing when it runs: its function is made when the program is linked. A declaration
   * whose initializer is a function or an arrow, whatever its parameters and body, makes `[name, length, constructor]`.
   */
  function closures(node, decls, funs) {
    const declared = node.type === "ExportDefaultDeclaration" ? node.declaration : node;
    if (declared.type === "FunctionDeclaration") return decls.length === 0 ? [] : null;
    if (node.type !== "VariableDeclarator" || node.id.type !== "Identifier" || decls.length !== 1 || funs.length !== 0)
      return null;
    const f = node.init;
    if (f?.type === "ArrowFunctionExpression") return [[node.id.name, paramLength(f.params), false]];
    if (f?.type === "FunctionExpression")
      return [[f.id?.name ?? node.id.name, paramLength(f.params), !(f.async || f.generator)]];
    return null;
  }

  for (const node of ast.body)
    if (node.type === "ImportDeclaration" && !external)
      for (const s of node.specifiers) importedNames.add(s.local.name);
  const moduleNames = scopeManager
    ? defs.map(({ v }) => v.name)
    : [...varNames(ast.body), ...lexicalNames(ast.body), ...importedNames];
  // Bundle units keep their source ordinals, including gaps for external imports.
  // Linked programs number only the body, since the linker handles their imports.
  let linkedImports = 0;
  within(moduleNames, () =>
    units.forEach(({ node }, ordinal) => {
      if (node.type === "ImportDeclaration") {
        if (!external) {
          importDeclaration(node);
          linkedImports++;
        }
        return;
      }
      const index = ordinal - linkedImports;
      topLevel(node, ids === null ? idBase + index : ids[index]);
    })
  );
  return {
    tokens: out,
    units: unitIds.length,
    ids: unitIds,
    spans,
    imports,
    exports,
    numbered: units.length - linkedImports,
    opaque: opaqueUnits,
    opaqueBodies,
    refused,
  };
}

/**
 * The program that starts at the file `entry`, with every module it reaches: `{ entry, modules }` as `Decode.lean`
 * reads it. A module's name is its path from the directory of the entry. `ids[name]` gives the identities of the
 * statements of that module; without it the statements are numbered on through the modules, in the order they are read.
 */
export function toCoreProgram(entry, { allowHost = false, ids = {}, opaque = null } = {}) {
  const root = path.dirname(entry);
  const modules = [];
  const seen = new Set();
  let idBase = 0;
  const load = (name) => {
    if (seen.has(name)) return;
    seen.add(name);
    const source = readFileSync(path.join(root, name), "utf8");
    const translated = toCore(source, { allowHost, ids: ids[name] ?? null, idBase, opaque });
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
