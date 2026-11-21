import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const { parse } = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn");
const parseModule = (text) => parse(text, { ecmaVersion: "latest", sourceType: "module" }).body;
const check = (condition, reason) => {
  if (!condition) throw new Error(`unknown: ${reason}`);
};
const identifier = (node) => {
  check(node?.type === "Identifier", "expected an identifier");
  return node.name;
};
const list = (values) => `[${values.join(", ")}]`;
const str = JSON.stringify;

// Conservative lexical analysis for generating candidate modules before the
// corresponding execution semantics are supported by the proof checker. Each
// block predeclares its lexical bindings, including declarations that are not
// initialized yet, so a shadowing name never becomes a spurious import.
function scopedFunction(node) {
  check(node.type === "FunctionDeclaration" && !node.async && !node.generator, "unsupported function");
  const name = identifier(node.id);
  check(name !== "globalThis" && name !== "eval", "unsupported function name");
  const reads = new Set(),
    writes = new Set();
  const addDeclarations = (body, frame) => {
    for (const stmt of body) {
      if (stmt.type === "VariableDeclaration") {
        check(stmt.kind === "let" || stmt.kind === "const", "unsupported declaration kind");
        for (const decl of stmt.declarations) frame.add(identifier(decl.id));
      } else if (stmt.type === "FunctionDeclaration") frame.add(identifier(stmt.id));
    }
  };
  const visitFunction = (fn, scopes) => {
    check(!fn.async && !fn.generator, "unsupported function kind");
    const frame = new Set(fn.params.map(identifier));
    if (fn.id) frame.add(identifier(fn.id));
    if (fn.body.type === "BlockStatement") {
      addDeclarations(fn.body.body, frame);
      for (const stmt of fn.body.body) visit(stmt, [frame, ...scopes]);
    } else visit(fn.body, [frame, ...scopes]);
  };
  const reference = (id, scopes, write = false) => {
    const value = identifier(id);
    check(!["globalThis", "eval", "arguments"].includes(value), "unsupported dynamic or host reference");
    if (!scopes.some((scope) => scope.has(value))) {
      reads.add(value);
      if (write) writes.add(value);
    }
  };
  const visit = (item, scopes) => {
    check(item && typeof item === "object", "unsupported expression");
    switch (item.type) {
      case "Identifier":
        reference(item, scopes);
        break;
      case "Literal":
        check(
          typeof item.value === "number" && Number.isSafeInteger(item.value) && item.value >= 0,
          "unsupported literal"
        );
        break;
      case "BlockStatement": {
        const frame = new Set();
        addDeclarations(item.body, frame);
        for (const stmt of item.body) visit(stmt, [frame, ...scopes]);
        break;
      }
      case "VariableDeclaration":
        check(item.kind === "let" || item.kind === "const", "unsupported declaration kind");
        for (const decl of item.declarations) {
          identifier(decl.id);
          if (decl.init) visit(decl.init, scopes);
        }
        break;
      case "FunctionDeclaration":
      case "FunctionExpression":
      case "ArrowFunctionExpression":
        visitFunction(item, scopes);
        break;
      case "ReturnStatement":
        if (item.argument) visit(item.argument, scopes);
        break;
      case "ThrowStatement":
        visit(item.argument, scopes);
        break;
      case "ExpressionStatement":
        visit(item.expression, scopes);
        break;
      case "BinaryExpression":
      case "LogicalExpression":
        visit(item.left, scopes);
        visit(item.right, scopes);
        break;
      case "ConditionalExpression":
        visit(item.test, scopes);
        visit(item.consequent, scopes);
        visit(item.alternate, scopes);
        break;
      case "IfStatement":
        visit(item.test, scopes);
        visit(item.consequent, scopes);
        if (item.alternate) visit(item.alternate, scopes);
        break;
      case "CallExpression":
        check(!item.optional && item.callee.type !== "Super", "unsupported call");
        visit(item.callee, scopes);
        for (const arg of item.arguments) visit(arg, scopes);
        break;
      case "AssignmentExpression":
        reference(item.left, scopes, true);
        visit(item.right, scopes);
        break;
      case "UpdateExpression":
        reference(item.argument, scopes, true);
        break;
      case "EmptyStatement":
        break;
      default:
        check(false, `unsupported scoped syntax: ${item.type}`);
    }
  };
  visitFunction(node, []);
  return { kind: "scopedFunction", name, references: [...reads], writes: [...writes] };
}

// A compositional expression grammar for functions with numeric parameters.
// This is also emitted into the Lean AST; the generator's free-name list is
// only used to construct imports, while Lean recomputes it for validation.
function expressionFunction(node, functions, stack = [], budget = { remaining: 500 }) {
  check(node.type === "FunctionDeclaration" && !node.async && !node.generator, "unsupported function");
  const name = identifier(node.id);
  check(!stack.includes(name), "recursive calls are unsupported");
  const parameters = node.params.map(identifier);
  const localNames = new Set(parameters);
  const scanLocals = (item) => {
    if (!item || typeof item !== "object") return;
    if (item.type === "VariableDeclarator") localNames.add(identifier(item.id));
    if (item.type === "FunctionDeclaration" && item !== node) localNames.add(identifier(item.id));
    for (const child of Object.values(item)) {
      if (Array.isArray(child)) child.forEach(scanLocals);
      else if (child && typeof child === "object") scanLocals(child);
    }
  };
  scanLocals(node.body);
  check(name !== "globalThis" && !parameters.includes("globalThis"), "unsupported globalThis binding");
  check(new Set(parameters).size === parameters.length, "duplicate parameters");
  check(node.body.body.length > 0, "expected a returning function body");
  const expression = (node) => {
    check(--budget.remaining >= 0, "expression expansion limit exceeded");
    if (node?.type === "Literal") {
      check(Number.isSafeInteger(node.value) && node.value >= 0, "expected a nonnegative safe integer");
      return { kind: "literal", value: node.value };
    }
    if (node?.type === "Identifier") {
      const value = identifier(node);
      check(!["globalThis", "eval", "arguments"].includes(value), "unsupported dynamic reference");
      return { kind: "name", name: value };
    }
    if (node?.type === "BinaryExpression" && ["+", "*", "-"].includes(node.operator))
      return {
        kind: node.operator === "+" ? "add" : node.operator === "*" ? "multiply" : "subtract",
        left: expression(node.left),
        right: expression(node.right),
      };
    if (node?.type === "CallExpression" && !node.optional) {
      const target = identifier(node.callee);
      check(!localNames.has(target), "calls through local bindings are unsupported");
      const declaration = functions?.get(target);
      check(declaration, "unknown function call target");
      const callee = expressionFunction(declaration, functions, [...stack, name], budget);
      const args = node.arguments.map(expression);
      check(args.length === callee.parameters.length, "call argument count differs from parameters");
      return { kind: "invoke", name: target, parameters: callee.parameters, arguments: args, body: callee.expression };
    }
    if (node?.type === "ConditionalExpression")
      return {
        kind: "conditional",
        test: condition(node.test),
        consequent: expression(node.consequent),
        alternative: expression(node.alternate),
      };
    check(false, "unsupported numeric expression");
  };
  // Comparisons and logical operators are accepted only in a condition. The
  // Lean expression yields 0 or 1 for truthiness; JavaScript boolean values
  // are never exposed as numeric function results by this lowering.
  const condition = (node) => {
    check(--budget.remaining >= 0, "expression expansion limit exceeded");
    if (node?.type === "Literal" && typeof node.value === "boolean")
      return { kind: "literal", value: node.value ? 1 : 0 };
    if (node?.type === "BinaryExpression" && ["===", "<"].includes(node.operator))
      return {
        kind: node.operator === "===" ? "equal" : "less",
        left: expression(node.left),
        right: expression(node.right),
      };
    if (node?.type === "BinaryExpression" && ["!==", ">="].includes(node.operator))
      return {
        kind: "conditional",
        test: {
          kind: node.operator === "!==" ? "equal" : "less",
          left: expression(node.left),
          right: expression(node.right),
        },
        consequent: { kind: "literal", value: 0 },
        alternative: { kind: "literal", value: 1 },
      };
    if (node?.type === "UnaryExpression" && node.operator === "!")
      return {
        kind: "conditional",
        test: condition(node.argument),
        consequent: { kind: "literal", value: 0 },
        alternative: { kind: "literal", value: 1 },
      };
    if (node?.type === "LogicalExpression" && ["&&", "||"].includes(node.operator))
      return {
        kind: "conditional",
        test: condition(node.left),
        consequent: node.operator === "&&" ? condition(node.right) : { kind: "literal", value: 1 },
        alternative: node.operator === "&&" ? { kind: "literal", value: 0 } : condition(node.right),
      };
    return expression(node);
  };
  const sequence = (statements) => {
    check(--budget.remaining >= 0, "expression expansion limit exceeded");
    check(statements.length > 0, "expected a returning statement");
    const [first, ...rest] = statements;
    if (first.type === "VariableDeclaration") {
      check(
        (first.kind === "let" || first.kind === "const") && first.declarations.length === 1 && rest.length > 0,
        "unsupported local declaration"
      );
      const declaration = first.declarations[0];
      const name = identifier(declaration.id);
      check(name !== "globalThis", "unsupported local binding");
      return {
        kind: "local",
        name,
        mutable: first.kind === "let",
        initial: expression(declaration.init),
        body: sequence(rest),
      };
    }
    if (first.type === "ExpressionStatement" && rest.length > 0) {
      const assignment = first.expression;
      let name, value;
      if (assignment?.type === "AssignmentExpression" && ["=", "+=", "*=", "-="].includes(assignment.operator)) {
        name = identifier(assignment.left);
        value = expression(assignment.right);
        if (assignment.operator !== "=")
          value = {
            kind: assignment.operator === "+=" ? "add" : assignment.operator === "*=" ? "multiply" : "subtract",
            left: { kind: "name", name },
            right: value,
          };
      } else if (assignment?.type === "UpdateExpression" && ["++", "--"].includes(assignment.operator)) {
        name = identifier(assignment.argument);
        value = {
          kind: assignment.operator === "++" ? "add" : "subtract",
          left: { kind: "name", name },
          right: { kind: "literal", value: 1 },
        };
      } else check(false, "unsupported mutation");
      check(!["globalThis", "eval", "arguments"].includes(name), "unsupported assignment target");
      return { kind: "assign", name, value, body: sequence(rest) };
    }
    if (first.type === "IfStatement" && !first.alternate && rest.length > 0)
      return {
        kind: "guard",
        test: condition(first.test),
        consequent: returning(first.consequent),
        alternative: sequence(rest),
      };
    check(rest.length === 0, "unsupported statements after return");
    return returning(first);
  };
  const returning = (statement) => {
    if (statement?.type === "ReturnStatement") return expression(statement.argument);
    if (statement?.type === "ThrowStatement") {
      const argument = statement.argument;
      if (
        argument?.type === "Literal" &&
        (argument.value === null || typeof argument.value === "boolean" || typeof argument.value === "string")
      ) {
        check(typeof argument.value !== "string" || /^[\x20-\x7e]*$/.test(argument.value), "unsupported thrown string");
        return { kind: "raisePrimitive", value: argument.value };
      }
      if (
        argument?.type === "UnaryExpression" &&
        argument.operator === "void" &&
        argument.argument?.type === "Literal" &&
        argument.argument.value === 0
      )
        return { kind: "raisePrimitive", value: undefined };
      return { kind: "raise", value: expression(argument) };
    }
    if (statement?.type === "BlockStatement") return sequence(statement.body);
    if (statement?.type === "IfStatement" && statement.alternate)
      return {
        kind: "conditional",
        test: condition(statement.test),
        consequent: returning(statement.consequent),
        alternative: returning(statement.alternate),
      };
    check(false, "unsupported function body");
  };
  const body = sequence(node.body.body);
  const writes = new Set();
  const free = (item, bound) => {
    switch (item.kind) {
      case "literal":
        return [];
      case "name":
        return bound.includes(item.name) ? [] : [item.name];
      case "add":
      case "multiply":
      case "subtract":
      case "equal":
      case "less":
        return [...free(item.left, bound), ...free(item.right, bound)];
      case "conditional":
      case "guard":
        return [...free(item.test, bound), ...free(item.consequent, bound), ...free(item.alternative, bound)];
      case "local":
        return [...free(item.initial, bound), ...free(item.body, [item.name, ...bound])];
      case "assign":
        if (!bound.includes(item.name)) writes.add(item.name);
        return [
          ...(bound.includes(item.name) ? [] : [item.name]),
          ...free(item.value, bound),
          ...free(item.body, bound),
        ];
      case "raise":
        return free(item.value, bound);
      case "raisePrimitive":
        return [];
      case "invoke":
        return [item.name, ...item.arguments.flatMap((argument) => free(argument, bound))];
    }
    throw new Error("unknown numeric expression");
  };
  return {
    kind: "expressionFunction",
    name,
    parameters,
    expression: body,
    references: [...new Set(free(body, parameters))],
    writes: [...writes],
  };
}

// A factory captures its parameters and initialized lexical declarations.
// The returned function is lowered through the same expression grammar used for
// ordinary functions; cell keys are checked for freshness in Lean.
function capturedFactory(node, functions) {
  check(node.type === "FunctionDeclaration" && !node.async && !node.generator, "unsupported factory");
  const name = identifier(node.id);
  const parameters = node.params.map(identifier);
  const statements = node.body.body;
  const last = statements.at(-1);
  check(last?.type === "ReturnStatement", "expected returned function");
  let closure = last.argument;
  let functionName = null;
  let declarations = statements.slice(0, -1);
  if (closure?.type === "Identifier") {
    functionName = identifier(closure);
    const matches = declarations.filter(
      (statement) => statement.type === "FunctionDeclaration" && statement.id?.name === functionName
    );
    check(matches.length === 1, "expected one returned local function declaration");
    closure = matches[0];
    declarations = declarations.filter((statement) => statement !== closure);
  }
  check(
    ["ArrowFunctionExpression", "FunctionDeclaration"].includes(closure?.type) && !closure.async && !closure.generator,
    "unsupported returned function"
  );
  const closureParameters = closure.params.map(identifier);
  const visibleFunctions = new Map(functions);
  if (functionName !== null) visibleFunctions.delete(functionName);
  const locals = [];
  const refs = [];
  const synthetic = (idName, params, body) => ({
    type: "FunctionDeclaration",
    id: { type: "Identifier", name: idName },
    params: params.map((name) => ({ type: "Identifier", name })),
    body:
      body.type === "BlockStatement"
        ? body
        : { type: "BlockStatement", body: [{ type: "ReturnStatement", argument: body }] },
  });
  for (const statement of declarations) {
    check(
      statement.type === "VariableDeclaration" &&
        ["let", "const"].includes(statement.kind) &&
        statement.declarations.length === 1,
      "unsupported factory local"
    );
    const declaration = statement.declarations[0];
    const localName = identifier(declaration.id);
    check(declaration.init, "factory locals must be initialized");
    const initial = expressionFunction(
      synthetic(
        `factory_init_${locals.length}`,
        [...parameters, ...locals.map((local) => local.name)],
        declaration.init
      ),
      visibleFunctions,
      [name]
    );
    locals.push({ name: localName, mutable: statement.kind === "let", initial: initial.expression });
    refs.push(...initial.references);
  }
  const captured = [...parameters, ...locals.map((local) => local.name)];
  check(
    new Set(captured).size === captured.length &&
      new Set(closureParameters).size === closureParameters.length &&
      (functionName === null || (!captured.includes(functionName) && !closureParameters.includes(functionName))),
    "duplicate lexical bindings"
  );
  const closureBody = expressionFunction(
    synthetic("factory_closure", closureParameters, closure.body),
    visibleFunctions,
    [name]
  );
  check(
    functionName === null || ![...refs, ...closureBody.references].includes(functionName),
    "local function identity or recursion is unsupported"
  );
  refs.push(...closureBody.references.filter((reference) => !captured.includes(reference)));
  return {
    kind: "capturedFactory",
    name,
    parameters,
    locals,
    closureParameters,
    body: closureBody.expression,
    references: [...new Set(refs)],
    writes: closureBody.writes.filter((target) => !captured.includes(target)),
  };
}

function capturedClosure(node, functions) {
  check(
    node.type === "VariableDeclaration" && node.kind === "const" && node.declarations.length === 1,
    "expected const closure binding"
  );
  const declaration = node.declarations[0];
  const name = identifier(declaration.id);
  const call = declaration.init;
  check(call?.type === "CallExpression" && !call.optional, "expected factory call");
  const factory = identifier(call.callee);
  const definition = functions?.get(factory);
  check(definition, "unknown factory");
  const shape = capturedFactory(definition, functions);
  const args = call.arguments.map((argument) => {
    check(
      argument.type === "Literal" && Number.isSafeInteger(argument.value) && argument.value >= 0,
      "expected numeric factory argument"
    );
    return argument.value;
  });
  check(args.length === shape.parameters.length, "factory argument count differs from parameters");
  const keys = [...shape.parameters, ...shape.locals.map((local) => local.name)].map((_, index) => `#${name}:${index}`);
  return { kind: "capturedClosure", name, factory, arguments: args, keys };
}

function candidateStatement(node, functions) {
  try {
    const recognized = statement(node);
    if (recognized.kind === "closure" && functions?.has(recognized.factory)) {
      try {
        return capturedClosure(node, functions);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
      }
    }
    if (recognized.kind === "wrapper" && functions?.has(recognized.target)) {
      try {
        return expressionFunction(node, functions);
      } catch (error) {
        if (!String(error).includes("unknown:")) throw error;
      }
    }
    return recognized;
  } catch (error) {
    if (!String(error).includes("unknown:")) throw error;
    if (node.type === "VariableDeclaration") {
      const declaration = node.declarations[0];
      if (node.kind === "const" && node.declarations.length === 1 && declaration.init?.type === "Identifier") {
        const name = identifier(declaration.id);
        check(name !== "globalThis", "globalThis cannot be locally declared");
        return { kind: "capturedAlias", name, target: identifier(declaration.init) };
      }
      return capturedClosure(node, functions);
    }
    if (node.type !== "FunctionDeclaration") throw error;
    try {
      return expressionFunction(node, functions);
    } catch (expressionError) {
      if (!String(expressionError).includes("unknown:")) throw expressionError;
      try {
        return capturedFactory(node, functions);
      } catch (factoryError) {
        if (!String(factoryError).includes("unknown:")) throw factoryError;
        return scopedFunction(node);
      }
    }
  }
}

// This is a syntax-directed conversion to the Lean AST, shared by source and
// output extraction. It does not compare output against generator text.
function statement(node) {
  if (node.type === "VariableDeclaration") {
    check(node.declarations.length === 1, "expected a single declaration");
    const d = node.declarations[0];
    const name = identifier(d.id);
    if (
      (node.kind === "let" || node.kind === "const") &&
      d.init?.type === "CallExpression" &&
      !d.init.optional &&
      d.init.arguments.length === 0
    ) {
      check(name !== "globalThis", "globalThis cannot be locally declared");
      return { kind: "closure", name, factory: identifier(d.init.callee) };
    }
    check(node.kind === "let" || node.kind === "const", "expected a lexical declaration");
    check(
      name !== "globalThis" && d.init?.type === "Literal" && Number.isSafeInteger(d.init.value) && d.init.value >= 0,
      "expected a nonnegative safe integer initializer"
    );
    return { kind: node.kind === "const" ? "constant" : "variable", name, value: d.init.value };
  }
  if (node.type === "FunctionDeclaration") {
    check(!node.async && !node.generator && node.params.length === 0, "expected a simple function");
    const name = identifier(node.id);
    check(name !== "globalThis", "globalThis cannot be locally declared");
    if (node.body.body.length === 2) {
      const [localDecl, ret] = node.body.body;
      check(
        localDecl.type === "VariableDeclaration" && localDecl.kind === "let" && localDecl.declarations.length === 1,
        "expected a captured let"
      );
      const local = identifier(localDecl.declarations[0].id);
      const source = identifier(localDecl.declarations[0].init);
      const arrow = ret.type === "ReturnStatement" && ret.argument;
      check(
        local !== source &&
          local !== "globalThis" &&
          arrow?.type === "ArrowFunctionExpression" &&
          !arrow.async &&
          arrow.params.length === 0 &&
          arrow.body.type === "UpdateExpression" &&
          arrow.body.operator === "++" &&
          arrow.body.prefix &&
          identifier(arrow.body.argument) === local,
        "expected a captured increment closure"
      );
      return { kind: "factory", name, local, source };
    }
    check(node.body.body.length === 1, "expected a simple function");
    const stmt = node.body.body[0];
    if (stmt.type === "ThrowStatement") {
      check(
        stmt.argument?.type === "Literal" && Number.isSafeInteger(stmt.argument.value) && stmt.argument.value >= 0,
        "expected a nonnegative numeric throw"
      );
      return { kind: "thrower", name, value: stmt.argument.value };
    }
    if (stmt.type === "ReturnStatement") {
      if (stmt.argument?.type === "CallExpression" && !stmt.argument.optional && stmt.argument.arguments.length === 0)
        return { kind: "wrapper", name, target: identifier(stmt.argument.callee) };
      return { kind: "reader", name, source: identifier(stmt.argument) };
    }
    const expr = stmt.type === "ExpressionStatement" && stmt.expression;
    check(expr?.type === "AssignmentExpression" && expr.operator === "+=", "expected an addition assignment");
    return { kind: "function", name, target: identifier(expr.left), source: identifier(expr.right) };
  }
  const expr = node.type === "ExpressionStatement" && node.expression;
  check(expr?.type === "CallExpression" && !expr.optional, "expected a supported statement");
  if (expr.callee.type === "Identifier") {
    check(expr.arguments.length === 0, "function calls must have no arguments");
    return { kind: "call", name: identifier(expr.callee) };
  }
  const member = expr.callee;
  check(
    member.type === "MemberExpression" &&
      !member.computed &&
      !member.optional &&
      identifier(member.object) === "globalThis" &&
      identifier(member.property) === "record" &&
      expr.arguments.length === 1,
    "expected globalThis.record(binding)"
  );
  const arg = expr.arguments[0];
  if (arg?.type === "CallExpression" && !arg.optional && arg.arguments.length === 0)
    return { kind: "recordCall", name: identifier(arg.callee) };
  return { kind: "record", name: identifier(arg) };
}

function exportNames(node) {
  check(
    node.specifiers.every((s) => s.type === "ExportSpecifier" && identifier(s.local) === identifier(s.exported)),
    "export aliases are unsupported"
  );
  return node.specifiers.map((s) => identifier(s.local));
}

function functionDeclarations(bodies) {
  const functions = new Map();
  for (const body of bodies)
    for (const node of body) {
      const declaration = node.type === "ExportNamedDeclaration" && node.declaration ? node.declaration : node;
      if (declaration.type !== "FunctionDeclaration") continue;
      const name = identifier(declaration.id);
      check(!functions.has(name), "duplicate function declaration across modules");
      functions.set(name, declaration);
    }
  return functions;
}

function sourceAst(text) {
  const statements = [];
  const exports = [];
  const body = parseModule(text);
  const functions = functionDeclarations([body]);
  for (const node of body) {
    if (node.type === "ExportNamedDeclaration" && !node.declaration && !node.source) exports.push(...exportNames(node));
    else if (node.type === "ExportNamedDeclaration" && node.declaration && !node.source) {
      const item = candidateStatement(node.declaration, functions);
      check(declarationName(item), "expected an exported declaration");
      exports.push(declarationName(item));
      statements.push({ ast: item, source: text.slice(node.declaration.start, node.declaration.end) });
    } else statements.push({ ast: candidateStatement(node, functions), source: text.slice(node.start, node.end) });
  }
  check(
    statements.length > 0 && statements.length <= 20 && exports.length > 0,
    "expected up to twenty statements and public exports"
  );
  return { statements, exports };
}

function readSpec(spec, count) {
  check(
    spec?.kind === "bindings" && spec.modules && typeof spec.modules === "object" && !Array.isArray(spec.modules),
    "expected a bindings assignment"
  );
  const names = Object.keys(spec.modules);
  check(
    names.length > 0 &&
      names.length <= 12 &&
      names.includes(spec.entry) &&
      names.every((name) => /^[A-Za-z][A-Za-z0-9_-]*\.mjs$/.test(name)),
    "invalid module names"
  );
  const assigned = [];
  for (const name of names) {
    const module = spec.modules[name];
    check(
      module && Array.isArray(module.imports) && Array.isArray(module.statements),
      "expected imports and statement indices"
    );
    check(
      module.imports.every((dep) => names.includes(dep)) &&
        module.statements.every((i, n, a) => Number.isInteger(i) && i >= 0 && i < count && (!n || a[n - 1] < i)),
      "invalid imports or statement indices"
    );
    assigned.push(...module.statements);
  }
  check(assigned.length === count && new Set(assigned).size === count, "assign every statement once");
  return names;
}

const declarationName = (item) =>
  [
    "variable",
    "constant",
    "function",
    "reader",
    "thrower",
    "factory",
    "closure",
    "wrapper",
    "scopedFunction",
    "expressionFunction",
    "capturedFactory",
    "capturedClosure",
    "capturedAlias",
  ].includes(item.kind)
    ? item.name
    : null;
function references(item) {
  if (["scopedFunction", "expressionFunction", "capturedFactory"].includes(item.kind)) return item.references;
  if (item.kind === "function") return [item.target, item.source];
  if (item.kind === "reader" || item.kind === "factory") return [item.source];
  if (item.kind === "closure" || item.kind === "capturedClosure") return [item.factory];
  if (item.kind === "capturedAlias") return [item.target];
  if (item.kind === "wrapper") return [item.target];
  if (item.kind === "recordCall") return [item.name];
  if (item.kind === "call" || item.kind === "record") return [item.name];
  return [];
}

function generateFiles(source, spec, names) {
  const defs = new Map();
  for (const name of names)
    for (const index of spec.modules[name].statements) {
      const binding = declarationName(source.statements[index].ast);
      if (binding) {
        check(!defs.has(binding), "duplicate declaration");
        defs.set(binding, name);
      }
    }
  const immutable = new Set(source.statements.filter((s) => s.ast.kind === "constant").map((s) => s.ast.name));
  for (const name of names)
    for (const index of spec.modules[name].statements) {
      const item = source.statements[index].ast;
      if (item.kind === "function") {
        check(defs.get(item.target) === name, "cannot write an imported binding");
        check(!immutable.has(item.target), "cannot write a const binding");
      }
      if (["scopedFunction", "expressionFunction", "capturedFactory"].includes(item.kind))
        for (const target of item.writes) {
          check(defs.get(target) === name, "cannot write an imported binding");
          check(!immutable.has(target), "cannot write a const binding");
        }
    }
  const referencesByModule = Object.fromEntries(
    names.map((name) => [
      name,
      [...new Set(spec.modules[name].statements.flatMap((i) => references(source.statements[i].ast)))],
    ])
  );
  const neededExports = new Set(source.exports);
  for (const name of names)
    for (const binding of referencesByModule[name]) {
      const owner = defs.get(binding);
      check(owner, `missing definition: ${binding}`);
      if (owner !== name) {
        check(spec.modules[name].imports.includes(owner), `missing dependency for ${binding}`);
        neededExports.add(binding);
      }
    }
  const result = {};
  for (const name of names) {
    const module = spec.modules[name];
    const imports = module.imports.map((dep) => {
      const bindings = referencesByModule[name].filter((binding) => defs.get(binding) === dep);
      return bindings.length
        ? `import { ${bindings.join(", ")} } from ${str(`./${dep}`)};`
        : `import ${str(`./${dep}`)};`;
    });
    const declarations = module.statements.map((index) => {
      const item = source.statements[index];
      const binding = declarationName(item.ast);
      const shouldExport =
        binding && neededExports.has(binding) && (name !== spec.entry || source.exports.includes(binding));
      return `${shouldExport ? "export " : ""}${item.source}`;
    });
    const reexports =
      name === spec.entry
        ? source.exports
            .filter((binding) => defs.get(binding) !== name)
            .map((binding) => {
              check(defs.has(binding), `missing exported definition: ${binding}`);
              return `export { ${binding} } from ${str(`./${defs.get(binding)}`)};`;
            })
        : [];
    result[name] = [...imports, ...declarations, ...reexports].join("\n") + "\n";
  }
  return result;
}

function outputAst(text, names, functions) {
  const imports = [];
  const exports = [];
  const statements = [];
  const dependency = (node) => {
    check(typeof node.source?.value === "string" && node.source.value.startsWith("./"), "unsupported module path");
    const dep = names.indexOf(node.source.value.slice(2));
    check(dep >= 0 && (!node.attributes || node.attributes.length === 0), "unrecognized dependency");
    return dep;
  };
  for (const node of parseModule(text)) {
    if (node.type === "ImportDeclaration") {
      const dep = dependency(node);
      const locals = node.specifiers.map((s) => {
        check(
          s.type === "ImportSpecifier" && identifier(s.imported) === identifier(s.local),
          "import aliases are unsupported"
        );
        return identifier(s.local);
      });
      imports.push({ dep, locals, reexports: [] });
    } else if (node.type === "ExportNamedDeclaration") {
      if (node.declaration) {
        const ast = candidateStatement(node.declaration, functions);
        check(declarationName(ast), "unsupported exported declaration");
        exports.push(declarationName(ast));
        statements.push(ast);
      } else if (node.source) imports.push({ dep: dependency(node), locals: [], reexports: exportNames(node) });
      else exports.push(...exportNames(node));
    } else statements.push(candidateStatement(node, functions));
  }
  return { imports, exports, statements };
}

const leanStrings = (values) => list(values.map(str));
function leanExpression(expression) {
  switch (expression.kind) {
    case "literal":
      return `.literal ${expression.value}`;
    case "name":
      return `.name ${str(expression.name)}`;
    case "add":
    case "multiply":
    case "subtract":
    case "equal":
    case "less":
      return `.${expression.kind} (${leanExpression(expression.left)}) (${leanExpression(expression.right)})`;
    case "conditional":
    case "guard":
      return `.${expression.kind} (${leanExpression(expression.test)}) (${leanExpression(expression.consequent)}) (${leanExpression(expression.alternative)})`;
    case "local":
      return `.local ${str(expression.name)} ${expression.mutable} (${leanExpression(expression.initial)}) (${leanExpression(expression.body)})`;
    case "assign":
      return `.assign ${str(expression.name)} (${leanExpression(expression.value)}) (${leanExpression(expression.body)})`;
    case "raise":
      return `.raise (${leanExpression(expression.value)})`;
    case "invoke": {
      const argumentsTerm = expression.arguments.reduceRight(
        (tail, argument) => `.argumentsCons (${leanExpression(argument)}) (${tail})`,
        ".argumentsNil"
      );
      return `.invoke ${str(expression.name)} ${leanStrings(expression.parameters)} (${argumentsTerm}) (${leanExpression(expression.body)})`;
    }
    case "raisePrimitive": {
      const value = expression.value;
      const primitive =
        value === undefined
          ? ".undefined"
          : value === null
            ? ".null"
            : typeof value === "boolean"
              ? `.boolean ${value}`
              : `.text ${str(value)}`;
      return `.raisePrimitive (${primitive})`;
    }
  }
  throw new Error("unknown expression");
}
function leanStatement(item) {
  switch (item.kind) {
    case "variable":
      return `.variable ${str(item.name)} ${item.value}`;
    case "constant":
      return `.constant ${str(item.name)} ${item.value}`;
    case "function":
      return `.function ${str(item.name)} ${str(item.target)} ${str(item.source)}`;
    case "reader":
      return `.reader ${str(item.name)} ${str(item.source)}`;
    case "thrower":
      return `.thrower ${str(item.name)} ${item.value}`;
    case "expressionFunction":
      return `.expressionFunction ${str(item.name)} ${leanStrings(item.parameters)} (${leanExpression(item.expression)})`;
    case "capturedFactory": {
      const locals = list(
        item.locals.map(
          (local) =>
            `({ name := ${str(local.name)}, mutable := ${local.mutable}, initial := ${leanExpression(local.initial)} } : CapturedLocal)`
        )
      );
      return `.capturedFactory ${str(item.name)} ${leanStrings(item.parameters)} ${locals} ${leanStrings(item.closureParameters)} (${leanExpression(item.body)})`;
    }
    case "capturedAlias":
      return `.capturedAlias ${str(item.name)} ${str(item.target)}`;
    case "capturedClosure":
      return `.capturedClosure ${str(item.name)} ${str(item.factory)} ${list(item.arguments.map(String))} ${leanStrings(item.keys)}`;
    case "factory":
      return `.factory ${str(item.name)} ${str(item.local)} ${str(item.source)}`;
    case "closure":
      return `.closure ${str(item.name)} ${str(item.factory)}`;
    case "wrapper":
      return `.wrapper ${str(item.name)} ${str(item.target)}`;
    case "recordCall":
      return `.recordCall ${str(item.name)}`;
    case "call":
      return `.call ${str(item.name)}`;
    case "record":
      return `.record ${str(item.name)}`;
  }
  throw new Error("unknown statement");
}
function leanModule(module, assignment) {
  const imports = module.imports.map(
    (i) =>
      `({ dependency := ${i.dep}, locals := ${leanStrings(i.locals)}, reexports := ${leanStrings(i.reexports)} } : BindingImport)`
  );
  return `({ imports := ${list(imports)}, exports := ${leanStrings(module.exports)}, statements := ${list(module.statements.map(leanStatement))}, assignment := ${list(assignment)} } : BindingModule)`;
}

async function load(sourcePath, specPath) {
  const text = await readFile(sourcePath, "utf8");
  const source = sourceAst(text);
  const spec = JSON.parse(await readFile(specPath, "utf8"));
  const names = readSpec(spec, source.statements.length);
  return { source, spec, names, text };
}

export async function generateBindings(sourcePath, specPath, out) {
  const { source, spec, names } = await load(sourcePath, specPath);
  const files = generateFiles(source, spec, names);
  await mkdir(out, { recursive: true });
  for (const name of names) await writeFile(resolve(out, name), files[name], { flag: "wx" });
}

function execute(path, calls = []) {
  const script = `const events=[];globalThis.record=v=>events.push(v);import(${str(pathToFileURL(resolve(path)).href)}).then(m=>{const exports=Object.keys(m);const results=${str(calls)}.map(call=>typeof call==='string'?m[call]():m[call.name](...call.args));console.log(JSON.stringify(${calls.length ? "{events,exports,results}" : "{events,exports}"}));});`;
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });
  if (child.error || child.status !== 0) throw new Error(child.stderr || String(child.error));
  return JSON.parse(child.stdout);
}

export async function validateBindings(sourcePath, specPath, out) {
  const { source, spec, names, text } = await load(sourcePath, specPath);
  if (source.statements.some((item) => item.ast.kind === "scopedFunction"))
    return { result: "unknown", reason: "scoped function execution is not modeled" };
  const files = await Promise.all(names.map((name) => readFile(resolve(out, name), "utf8")));
  const functions = functionDeclarations(files.map(parseModule));
  const actual = files.map((text) => outputAst(text, names, functions));
  if (actual.some((module) => module.statements.some((item) => item.kind === "scopedFunction")))
    return { result: "unknown", reason: "scoped output function execution is not modeled" };
  const terms = actual.map((module, i) => leanModule(module, spec.modules[names[i]].statements));
  const digest = (content) => createHash("sha256").update(content).digest("hex");
  const prefix = `\n-- Source SHA256: ${digest(text)}\n-- Output SHA256: ${names.map((name, i) => `${name}=${digest(files[i])}`).join(" ")}\ndef bindingSource : List BindingStmt := ${list(source.statements.map((item) => leanStatement(item.ast)))}\ndef bindingExports : List String := ${leanStrings(source.exports)}\ndef bindingModules : List BindingModule := ${list(terms)}\ndef bindingEntry : Nat := ${names.indexOf(spec.entry)}\ntheorem binding_graph_valid : modulesValid bindingSource bindingExports bindingModules bindingEntry = true := by decide\ntheorem binding_source_ready : (sourceBindingState bindingSource).isSome = true := by decide\ntheorem binding_output_ready : (outputBindingState bindingSource bindingModules bindingEntry).isSome = true := by decide\n#print axioms lexical_nearest_binding\n#print axioms accepted_reference_owner\n#print axioms binding_graph_valid\n#print axioms binding_source_ready\n#print axioms binding_output_ready\n`;
  const legacySame = `\ntheorem binding_initialization : sourceBindingState bindingSource = outputBindingState bindingSource bindingModules bindingEntry := by decide\ntheorem binding_public_table : publicFunctionTable bindingSource bindingExports = publicFunctionTable (outputDefinitions bindingModules) bindingExports := by decide\ntheorem binding_public_table_ready : (publicFunctionTable bindingSource bindingExports).isSome = true := by decide\ntheorem binding_future_calls (calls : List String) : (sourceBindingState bindingSource).bind (fun state => (publicFunctionTable bindingSource bindingExports).bind (fun table => runExportedCalls table state calls)) = (outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicFunctionTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runExportedCalls table state calls)) := by\n  exact binding_calls_preserved _ _ _ _ binding_initialization binding_public_table calls\ntheorem binding_return_values (calls : List String) : ((sourceBindingState bindingSource).bind (fun state => (publicFunctionTable bindingSource bindingExports).bind (fun table => runExportedCalls table state calls))).map (fun state => state.returns) = ((outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicFunctionTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runExportedCalls table state calls))).map (fun state => state.returns) := by\n  exact congrArg (fun result => result.map (fun state => state.returns)) (binding_future_calls calls)\ntheorem binding_throw_values (calls : List String) : ((sourceBindingState bindingSource).bind (fun state => (publicFunctionTable bindingSource bindingExports).bind (fun table => runExportedCalls table state calls))).map (fun state => state.thrown) = ((outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicFunctionTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runExportedCalls table state calls))).map (fun state => state.thrown) := by\n  exact congrArg (fun result => result.map (fun state => state.thrown)) (binding_future_calls calls)\n#print axioms binding_throw_values\n#print axioms binding_initialization\n#print axioms binding_public_table\n#print axioms binding_public_table_ready\n#print axioms binding_future_calls\n#print axioms binding_return_values\n`;
  const hasExpressions = source.statements.some((item) =>
    ["expressionFunction", "capturedFactory", "capturedClosure", "capturedAlias"].includes(item.ast.kind)
  );
  const expressionSame = `
theorem binding_initialization : sourceBindingState bindingSource = outputBindingState bindingSource bindingModules bindingEntry := by decide
theorem binding_public_table : publicCallableTable bindingSource bindingExports = publicCallableTable (outputDefinitions bindingModules) bindingExports := by decide
theorem binding_public_table_ready : (publicCallableTable bindingSource bindingExports).isSome = true := by decide
theorem binding_future_calls (calls : List (String × List Nat)) : (sourceBindingState bindingSource).bind (fun state => (publicCallableTable bindingSource bindingExports).bind (fun table => runArgumentCalls table state calls)) = (outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicCallableTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runArgumentCalls table state calls)) := by
  exact argument_calls_preserved _ _ _ _ binding_initialization binding_public_table calls
theorem binding_return_values (calls : List (String × List Nat)) : ((sourceBindingState bindingSource).bind (fun state => (publicCallableTable bindingSource bindingExports).bind (fun table => runArgumentCalls table state calls))).map (fun state => state.returns) = ((outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicCallableTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runArgumentCalls table state calls))).map (fun state => state.returns) := by
  exact congrArg (fun result => result.map (fun state => state.returns)) (binding_future_calls calls)
theorem binding_throw_values (calls : List (String × List Nat)) : ((sourceBindingState bindingSource).bind (fun state => (publicCallableTable bindingSource bindingExports).bind (fun table => runArgumentCalls table state calls))).map (fun state => state.thrown) = ((outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (publicCallableTable (outputDefinitions bindingModules) bindingExports).bind (fun table => runArgumentCalls table state calls))).map (fun state => state.thrown) := by
  exact congrArg (fun result => result.map (fun state => state.thrown)) (binding_future_calls calls)
#print axioms binding_throw_values
#print axioms binding_initialization
#print axioms binding_public_table
#print axioms binding_public_table_ready
#print axioms binding_future_calls
#print axioms binding_return_values
`;
  const directCapturedExports = source.exports.filter((name) =>
    source.statements.some((item) => item.ast.kind === "capturedClosure" && item.ast.name === name)
  );
  const twoCaptured = source.exports.length === 2 && directCapturedExports.length === 2;
  const capturedRelation = twoCaptured
    ? `
def binding_source_first := (publicCapturedEnvironment bindingSource ${str(directCapturedExports[0])}).getD []
def binding_source_second := (publicCapturedEnvironment bindingSource ${str(directCapturedExports[1])}).getD []
def binding_output_first := (publicCapturedEnvironment (outputDefinitions bindingModules) ${str(directCapturedExports[0])}).getD []
def binding_output_second := (publicCapturedEnvironment (outputDefinitions bindingModules) ${str(directCapturedExports[1])}).getD []
def binding_source_state := (sourceBindingState bindingSource).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
def binding_output_state := (outputBindingState bindingSource bindingModules bindingEntry).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
theorem binding_captured_environments_present :
    (publicCapturedEnvironment bindingSource ${str(directCapturedExports[0])}).isSome = true ∧
    (publicCapturedEnvironment bindingSource ${str(directCapturedExports[1])}).isSome = true ∧
    (publicCapturedEnvironment (outputDefinitions bindingModules) ${str(directCapturedExports[0])}).isSome = true ∧
    (publicCapturedEnvironment (outputDefinitions bindingModules) ${str(directCapturedExports[1])}).isSome = true := by decide
theorem binding_source_captures_disjoint : capturesDisjoint binding_source_first binding_source_second := by decide
theorem binding_output_captures_disjoint : capturesDisjoint binding_output_first binding_output_second := by decide
theorem binding_captured_initial_related : twoCapturedStatesRelated binding_source_first binding_output_first
    binding_source_second binding_output_second binding_source_state binding_output_state := by decide
theorem binding_two_captured_calls (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation binding_source_first binding_output_first
      binding_source_second binding_output_second) leftOps rightOps)
    (leftFinal rightFinal : ExecutionState Unit)
    (leftRun : runExecution binding_source_state leftOps = some leftFinal)
    (rightRun : runExecution binding_output_state rightOps = some rightFinal) :
    twoCapturedStatesRelated binding_source_first binding_output_first binding_source_second binding_output_second
      leftFinal rightFinal := by
  exact two_captured_call_sequence_related binding_source_first binding_output_first binding_source_second binding_output_second
    binding_source_captures_disjoint binding_output_captures_disjoint leftOps rightOps operations
    binding_source_state binding_output_state leftFinal rightFinal binding_captured_initial_related leftRun rightRun
#print axioms binding_captured_environments_present
#print axioms binding_source_captures_disjoint
#print axioms binding_output_captures_disjoint
#print axioms binding_captured_initial_related
#print axioms binding_two_captured_calls
`
    : "";
  const same = hasExpressions ? expressionSame : legacySame;
  const model = (
    await Promise.all(
      ["Semantics.lean", "Effects.lean", "Bindings.lean"].map((name) =>
        readFile(new URL(`./${name}`, import.meta.url), "utf8")
      )
    )
  ).join("\n");
  const runLean = (certificate) =>
    spawnSync(process.env.JS_MODULE_PROOF_LEAN ?? "lean", ["-j", "1", "--trust=0", "--stdin"], {
      encoding: "utf8",
      input: model + certificate,
    });
  const has = (result, name) => result.stdout.includes(`'${name}' does not depend on any axioms`);
  const proof = runLean(prefix + same);
  if (proof.error || proof.status === null)
    return {
      result: "unknown",
      reason: "Lean could not be run",
      detail: proof.error
        ? String(proof.error)
        : `Lean terminated by ${proof.signal ?? "unknown signal"}: ${(proof.stderr || proof.stdout || "").slice(-4000)}`,
    };
  if (
    ![
      "lexical_nearest_binding",
      "accepted_reference_owner",
      "binding_graph_valid",
      "binding_source_ready",
      "binding_output_ready",
    ].every((name) => has(proof, name))
  )
    return {
      result: "unknown",
      reason: "Lean could not validate the extracted bindings, statements or module graph",
      detail: proof.stdout || proof.stderr,
    };
  if (
    proof.status === 0 &&
    [
      "binding_initialization",
      "binding_public_table",
      "binding_public_table_ready",
      "binding_future_calls",
      "binding_return_values",
      "binding_throw_values",
    ].every((name) => has(proof, name))
  ) {
    const relationProof = twoCaptured ? runLean(prefix + same + capturedRelation) : null;
    const relationValid =
      relationProof?.status === 0 &&
      [
        "binding_captured_environments_present",
        "binding_source_captures_disjoint",
        "binding_output_captures_disjoint",
        "binding_captured_initial_related",
        "binding_two_captured_calls",
      ].every((name) => has(relationProof, name));
    return {
      result: "proved under assumptions",
      certificate: prefix + same + (relationValid ? capturedRelation : ""),
      assumptions: [
        "The Acorn-to-Lean AST conversion faithfully represents the actual source and output files",
        "The modeled module loading, linking and execution match ECMAScript for the supported syntax",
        "All binding values, arguments and intermediate arithmetic results remain nonnegative safe integers",
        ...(hasExpressions
          ? [
              "Expression functions and captured closures receive exactly their declared number of numeric arguments; other exports are called without arguments",
            ]
          : []),
        "globalThis.record is a stable synchronous host function which returns normally without reentry or asynchronous effects",
        "Only initialization calls, their record observations, and calls through the entry exports after successful initialization are observed; loading, reflection, source text and identity are excluded",
        "Supported thrown values are nonnegative safe integers, booleans, null, undefined or ASCII strings; a throw ends the observed call sequence",
      ],
    };
  }
  const different = `\ntheorem binding_difference : sourceBindingState bindingSource ≠ outputBindingState bindingSource bindingModules bindingEntry := by decide\n#print axioms binding_difference\n`;
  let witnessFailure = proof.stdout || proof.stderr;
  const witnessed = (original, output, calls) => {
    const events = (trace) => {
      if (!trace.events.every((value) => Number.isSafeInteger(value) && value >= 0)) return null;
      return list(trace.events.map((value) => `.number ${value}`));
    };
    const returns = (trace) => {
      if (!trace.results.every((value) => value === null || (Number.isSafeInteger(value) && value >= 0))) return null;
      return list(trace.results.map((value) => (value === null ? "none" : `some ${value}`)));
    };
    const sourceEvents = events(original),
      outputEvents = events(output);
    if (sourceEvents === null || outputEvents === null) return null;
    let left, right, expectedLeft, expectedRight;
    if (calls.length === 0) {
      left = "(sourceBindingState bindingSource).map (fun state => state.events)";
      right = "(outputBindingState bindingSource bindingModules bindingEntry).map (fun state => state.events)";
      expectedLeft = `some ${sourceEvents}`;
      expectedRight = `some ${outputEvents}`;
    } else {
      const sourceReturns = returns(original),
        outputReturns = returns(output);
      if (sourceReturns === null || outputReturns === null) return null;
      const leanCalls = hasExpressions
        ? list(calls.map((call) => `(${str(call.name)}, ${list(call.args.map(String))})`))
        : leanStrings(calls);
      const table = hasExpressions ? "publicCallableTable" : "publicFunctionTable";
      const runner = hasExpressions ? "runArgumentCalls" : "runExportedCalls";
      left = `((sourceBindingState bindingSource).bind (fun state => (${table} bindingSource bindingExports).bind (fun table => ${runner} table state ${leanCalls}))).map (fun state => (state.events, state.returns))`;
      right = `((outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (${table} (outputDefinitions bindingModules) bindingExports).bind (fun table => ${runner} table state ${leanCalls}))).map (fun state => (state.events, state.returns))`;
      expectedLeft = `some (${sourceEvents}, ${sourceReturns})`;
      expectedRight = `some (${outputEvents}, ${outputReturns})`;
    }
    const witness = `\ntheorem binding_witness_original : ${left} = ${expectedLeft} := by decide\ntheorem binding_witness_output : ${right} = ${expectedRight} := by decide\ntheorem binding_witness_difference : ${left} ≠ ${right} := by decide\n#print axioms binding_witness_original\n#print axioms binding_witness_output\n#print axioms binding_witness_difference\n`;
    const checked = runLean(prefix + different + witness);
    if (
      checked.status === 0 &&
      ["binding_difference", "binding_witness_original", "binding_witness_output", "binding_witness_difference"].every(
        (name) => has(checked, name)
      )
    )
      return prefix + different + witness;
    witnessFailure =
      checked.status === null
        ? `Lean terminated by ${checked.signal ?? String(checked.error)}: ${(checked.stderr || checked.stdout || "").slice(-4000)}`
        : (checked.stdout || checked.stderr).slice(-6000);
    return null;
  };
  const original = execute(sourcePath);
  const output = execute(resolve(out, spec.entry));
  if (JSON.stringify(original) !== JSON.stringify(output)) {
    const certificate = witnessed(original, output, []);
    if (certificate) return { result: "unsafe", original, output, certificate };
    return {
      result: "unknown",
      reason: "Node initialization difference did not match the Lean observation",
      detail: witnessFailure,
    };
  }

  // Initialization can leave the same visible trace but different captured
  // state. Search short public-call sequences in fresh Node processes. The
  // conservative growth bound keeps every possible intermediate in range.
  const maximum = source.statements.reduce(
    (max, item) => (item.ast.kind === "variable" || item.ast.kind === "constant" ? Math.max(max, item.ast.value) : max),
    1
  );
  const initialCalls = source.statements.filter((item) => ["call", "recordCall"].includes(item.ast.kind)).length;
  const callable = source.exports.filter(
    (name) => !source.statements.some((item) => item.ast.kind === "thrower" && item.ast.name === name)
  );
  const resolveCapturedDefinition = (name) => {
    const seen = new Set();
    let definition = source.statements.find((item) => item.ast.name === name)?.ast;
    while (definition?.kind === "capturedAlias") {
      if (seen.has(definition.name)) return undefined;
      seen.add(definition.name);
      definition = source.statements.find((item) => item.ast.name === definition.target)?.ast;
    }
    return definition;
  };
  const candidates = hasExpressions
    ? callable.flatMap((name) => {
        const definition = resolveCapturedDefinition(name);
        const factory =
          definition?.kind === "capturedClosure"
            ? source.statements.find((item) => item.ast.name === definition.factory)?.ast
            : null;
        const parameters =
          definition?.kind === "expressionFunction" ? definition.parameters : factory?.closureParameters;
        if (!parameters) return [[{ name, args: [] }]];
        return [0, 1, 2].map((value) => [{ name, args: parameters.map(() => value) }]);
      })
    : callable.map((name) => [name]);
  if (!hasExpressions) for (const first of callable) for (const second of callable) candidates.push([first, second]);
  const expressionBound = (expression, bound) => {
    if (expression.kind === "literal") return BigInt(expression.value);
    if (expression.kind === "name") return bound;
    if (expression.kind === "add")
      return expressionBound(expression.left, bound) + expressionBound(expression.right, bound);
    if (expression.kind === "equal" || expression.kind === "less") {
      const left = expressionBound(expression.left, bound);
      const right = expressionBound(expression.right, bound);
      return [1n, left, right].reduce((a, b) => (a > b ? a : b));
    }
    if (expression.kind === "multiply") {
      const left = expressionBound(expression.left, bound);
      const right = expressionBound(expression.right, bound);
      return [left, right, left * right].reduce((a, b) => (a > b ? a : b));
    }
    if (expression.kind === "subtract") {
      const left = expressionBound(expression.left, bound);
      const right = expressionBound(expression.right, bound);
      return left > right ? left : right;
    }
    if (expression.kind === "conditional" || expression.kind === "guard")
      return [expression.test, expression.consequent, expression.alternative]
        .map((part) => expressionBound(part, bound))
        .reduce((a, b) => (a > b ? a : b));
    if (expression.kind === "local") {
      const initial = expressionBound(expression.initial, bound);
      const body = expressionBound(expression.body, initial > bound ? initial : bound);
      return initial > body ? initial : body;
    }
    if (expression.kind === "assign") {
      const value = expressionBound(expression.value, bound);
      const body = expressionBound(expression.body, value > bound ? value : bound);
      return value > body ? value : body;
    }
    if (expression.kind === "raise") return expressionBound(expression.value, bound);
    if (expression.kind === "raisePrimitive") return 0n;
    if (expression.kind === "invoke") {
      const args = expression.arguments.map((argument) => expressionBound(argument, bound));
      const callBound = args.reduce((largest, value) => (value > largest ? value : largest), bound);
      const body = expressionBound(expression.body, callBound);
      return body > callBound ? body : callBound;
    }
    throw new Error("unknown expression");
  };
  for (const calls of candidates.slice(0, 64)) {
    if (hasExpressions) {
      const definition = resolveCapturedDefinition(calls[0].name);
      const bound = BigInt(Math.max(maximum, 2)) << BigInt(initialCalls + 1);
      if (
        definition?.kind === "expressionFunction" &&
        expressionBound(definition.expression, bound) > BigInt(Number.MAX_SAFE_INTEGER)
      )
        continue;
      if (definition?.kind === "capturedClosure") {
        const factory = source.statements.find((item) => item.ast.name === definition.factory)?.ast;
        if (factory?.kind !== "capturedFactory") continue;
        let capturedBound = definition.arguments.reduce((largest, value) => {
          const n = BigInt(value);
          return n > largest ? n : largest;
        }, bound);
        for (const local of factory.locals) {
          const next = expressionBound(local.initial, capturedBound);
          if (next > capturedBound) capturedBound = next;
        }
        if (expressionBound(factory.body, capturedBound) > BigInt(Number.MAX_SAFE_INTEGER)) continue;
      }
    }
    if (BigInt(maximum) << BigInt(initialCalls + calls.length) > BigInt(Number.MAX_SAFE_INTEGER)) break;
    let before, after;
    try {
      before = execute(sourcePath, calls);
      after = execute(resolve(out, spec.entry), calls);
    } catch {
      // This witness harness only certifies normal returns. Throws are still
      // covered by safe proofs; skip them while looking for a normal witness.
      continue;
    }
    if (JSON.stringify(before) !== JSON.stringify(after)) {
      const certificate = witnessed(before, after, calls);
      if (certificate) return { result: "unsafe", original: before, output: after, calls, certificate };
    }
  }
  return { result: "unknown", reason: "no Lean-certified Node witness was found", detail: witnessFailure };
}
