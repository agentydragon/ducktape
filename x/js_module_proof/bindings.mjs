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
  check(node?.type === "Identifier", `expected an identifier, got ${node?.type ?? "nothing"}`);
  return node.name;
};
const primitiveLiteral = (node) =>
  node?.type === "Literal" &&
  !node.regex &&
  !node.bigint &&
  (node.value === null || ["string", "boolean", "number"].includes(typeof node.value));
const supportedText = (value) => typeof value === "string";
const list = (values) => `[${values.join(", ")}]`;
const str = JSON.stringify;
// JavaScript strings are sequences of UTF-16 code units, not Unicode scalars.
const leanText = (value) => list(Array.from({ length: value.length }, (_, index) => value.charCodeAt(index)));

// One syntax-directed representation is used for both recorded and thrown
// constants. Unsupported expressions are left to the ordinary expression parser.
function constantObservation(node) {
  if (node?.type === "Literal" && !node.regex) {
    if (typeof node.value === "bigint")
      return { kind: "bigInteger", negative: false, magnitude: node.value.toString() };
    if (supportedText(node.value)) return { kind: "text", value: node.value };
    if (node.value === null && !node.bigint) return { kind: "nullValue" };
    if (typeof node.value === "boolean") return { kind: "boolean", value: node.value };
    if (Number.isSafeInteger(node.value) && node.value >= 0) return { kind: "number", value: node.value };
  }
  if (node?.type === "UnaryExpression" && node.operator === "void" && primitiveLiteral(node.argument))
    return { kind: "undefined" };
  if (node?.type === "UnaryExpression" && node.operator === "-" && node.argument.type === "Literal") {
    const value = node.argument.value;
    if (typeof value === "bigint") return { kind: "bigInteger", negative: value !== 0n, magnitude: value.toString() };
    if (Number.isSafeInteger(value) && value >= 0)
      return value === 0 ? { kind: "negativeZero" } : { kind: "negativeNumber", magnitude: value };
  }
  if (
    node?.type === "BinaryExpression" &&
    node.operator === "/" &&
    node.right.type === "Literal" &&
    node.right.value === 0
  ) {
    if (node.left.type === "Literal" && (node.left.value === 0 || node.left.value === 1))
      return { kind: node.left.value === 0 ? "notANumber" : "positiveInfinity" };
    if (
      node.left.type === "UnaryExpression" &&
      node.left.operator === "-" &&
      node.left.argument.type === "Literal" &&
      node.left.argument.value === 1
    )
      return { kind: "negativeInfinity" };
  }
  return null;
}

function leanObservation(observation) {
  if (observation.kind === "text") return `.text ${leanText(observation.value)}`;
  if (observation.kind === "number" || observation.kind === "boolean")
    return `.${observation.kind} ${observation.value}`;
  if (observation.kind === "negativeNumber") return `.negativeNumber ${observation.magnitude}`;
  if (observation.kind === "bigInteger") return `.bigInteger ${observation.negative} ${observation.magnitude}`;
  return `.${observation.kind}`;
}

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

// Scalarize nonescaping local data objects and arrays. This is intentionally
// restricted to an allocation prefix and statically named, existing fields;
// any other use of an object binding is rejected rather than guessed at.
function scalarizeLocalData(node) {
  if (node.body?.type !== "BlockStatement") return node;
  const original = node.body.body;
  const hasData = original.some(
    (statement) =>
      statement.type === "VariableDeclaration" &&
      statement.declarations.some((declaration) =>
        ["ObjectExpression", "ArrayExpression"].includes(declaration.init?.type)
      )
  );
  if (!hasData) return node;
  const occupied = new Set();
  const gather = (item) => {
    if (!item || typeof item !== "object") return;
    if (item.type === "Identifier") occupied.add(item.name);
    for (const child of Object.values(item)) Array.isArray(child) ? child.forEach(gather) : gather(child);
  };
  gather(node);
  let next = 0;
  const fresh = () => {
    let name;
    do {
      name = `__local_data_${next++}`;
    } while (occupied.has(name));
    occupied.add(name);
    return name;
  };
  const objects = new Map();
  const output = [];
  let index = 0;
  for (; index < original.length; index++) {
    const statement = original[index];
    if (statement.type !== "VariableDeclaration" || statement.declarations.length !== 1) break;
    const decl = statement.declarations[0];
    const name = identifier(decl.id);
    const initial = decl.init;
    if (initial?.type === "Identifier" && objects.has(initial.name)) {
      check(statement.kind === "const" && !objects.has(name), "unsupported object alias");
      objects.set(name, objects.get(initial.name));
      continue;
    }
    if (!["ObjectExpression", "ArrayExpression"].includes(initial?.type)) break;
    check(statement.kind === "const" && !objects.has(name), "unsupported object binding");
    const fields = new Map();
    const addField = (key, value) => {
      check(key !== "__proto__" && !fields.has(key), "unsupported object property");
      check(
        value?.type === "Literal" && Number.isSafeInteger(value.value) && value.value >= 0,
        "unsupported object field initializer"
      );
      const cell = fresh();
      fields.set(key, cell);
      output.push({
        type: "VariableDeclaration",
        kind: "let",
        declarations: [
          {
            type: "VariableDeclarator",
            id: { type: "Identifier", name: cell },
            init: value,
          },
        ],
      });
    };
    if (initial.type === "ObjectExpression") {
      for (const property of initial.properties) {
        check(
          property.type === "Property" &&
            property.kind === "init" &&
            !property.method &&
            !property.computed &&
            !property.shorthand,
          "unsupported object property"
        );
        const key = property.key.type === "Identifier" ? property.key.name : property.key.value;
        check(typeof key === "string", "unsupported object property name");
        addField(key, property.value);
      }
    } else {
      initial.elements.forEach((element, elementIndex) => addField(String(elementIndex), element));
    }
    objects.set(name, fields);
  }
  check(objects.size > 0, "object allocations must precede other statements");
  const rewrite = (item) => {
    if (!item || typeof item !== "object") return item;
    if (Array.isArray(item)) return item.map(rewrite);
    if (item.type === "MemberExpression") {
      check(
        !item.optional && item.object?.type === "Identifier" && objects.has(item.object.name),
        "unsupported object access"
      );
      const key = item.computed
        ? item.property?.type === "Literal" && item.property.value
        : item.property?.type === "Identifier" && item.property.name;
      check(typeof key === "string" || (Number.isSafeInteger(key) && key >= 0), "dynamic object property");
      const cell = objects.get(item.object.name).get(String(key));
      check(cell, "unknown object property");
      return { type: "Identifier", name: cell };
    }
    if (item.type === "Identifier") check(!objects.has(item.name), "object identity escapes or is shadowed");
    check(
      ![
        "FunctionDeclaration",
        "FunctionExpression",
        "ArrowFunctionExpression",
        "ObjectExpression",
        "ArrayExpression",
      ].includes(item.type),
      "unsupported nested object use"
    );
    return Object.fromEntries(Object.entries(item).map(([key, value]) => [key, rewrite(value)]));
  };
  return { ...node, body: { ...node.body, body: [...output, ...original.slice(index).map(rewrite)] } };
}

// A compositional expression grammar for functions with numeric parameters.
// This is also emitted into the Lean AST; the generator's free-name list is
// only used to construct imports, while Lean recomputes it for validation.
function expressionFunction(node, functions, stack = [], budget = { remaining: 500 }) {
  check(node.type === "FunctionDeclaration" && !node.async && !node.generator, "unsupported function");
  node = scalarizeLocalData(node);
  const name = identifier(node.id);
  check(!stack.includes(name), "recursive calls are unsupported");
  const parameters = node.params.map(identifier);
  // A prefix of initialized function-scoped vars behaves like nested numeric
  // locals once self/forward references and redeclarations are excluded.
  const prefixVars = [];
  let afterVarPrefix = false;
  for (const statement of node.body.body) {
    if (statement.type === "VariableDeclaration" && statement.kind === "var") {
      check(!afterVarPrefix && statement.declarations.length === 1, "local var must be an initialized prefix");
      prefixVars.push(statement);
    } else afterVarPrefix = true;
  }
  const varNames = prefixVars.map((statement) => identifier(statement.declarations[0].id));
  check(
    new Set(varNames).size === varNames.length && varNames.every((item) => !parameters.includes(item)),
    "ambiguous local var"
  );
  const mentions = (item, names) => {
    if (!item || typeof item !== "object") return false;
    if (item.type === "Identifier" && names.has(item.name)) return true;
    return Object.values(item).some((child) =>
      Array.isArray(child) ? child.some((value) => mentions(value, names)) : mentions(child, names)
    );
  };
  for (const [index, statement] of prefixVars.entries())
    check(
      statement.declarations[0].init && !mentions(statement.declarations[0].init, new Set(varNames.slice(index))),
      "local var initializer reads a hoisted binding before initialization"
    );
  const containsNestedVar = (item) => {
    if (!item || typeof item !== "object") return false;
    if (["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"].includes(item.type)) return false;
    if (item.type === "VariableDeclaration" && item.kind === "var") return !prefixVars.includes(item);
    return Object.values(item).some((child) =>
      Array.isArray(child) ? child.some(containsNestedVar) : containsNestedVar(child)
    );
  };
  check(!node.body.body.some(containsNestedVar), "nested local var is unsupported");
  const localFunctions = new Map();
  for (const statement of node.body.body)
    if (statement.type === "FunctionDeclaration") {
      const localName = identifier(statement.id);
      check(
        !localFunctions.has(localName) &&
          !parameters.includes(localName) &&
          !varNames.includes(localName) &&
          !functions?.has(localName),
        "ambiguous local function binding"
      );
      localFunctions.set(localName, statement);
    }
  const nestedDeclarations = new Set();
  const collectNestedDeclarations = (item, depth = 0) => {
    if (!item || typeof item !== "object") return;
    if (
      item.type === "FunctionDeclaration" ||
      item.type === "FunctionExpression" ||
      item.type === "ArrowFunctionExpression"
    )
      return;
    if (item.type === "VariableDeclaration" && depth > 0)
      for (const declaration of item.declarations) nestedDeclarations.add(identifier(declaration.id));
    if (item.type === "ForStatement" && item.init?.type === "VariableDeclaration")
      for (const declaration of item.init.declarations) nestedDeclarations.add(identifier(declaration.id));
    const nextDepth = depth + (item.type === "BlockStatement" ? 1 : 0);
    for (const child of Object.values(item))
      if (Array.isArray(child)) child.forEach((value) => collectNestedDeclarations(value, nextDepth));
      else if (child && typeof child === "object") collectNestedDeclarations(child, nextDepth);
  };
  for (const statement of node.body.body) collectNestedDeclarations(statement);
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
  const binaryKinds = {
    "+": "add",
    "*": "multiply",
    "-": "subtract",
    "%": "remainder",
    "/": "divide",
    "&": "bitAnd",
    "|": "bitOr",
    "^": "bitXor",
    ">>>": "unsignedShift",
    "<<": "shiftLeft",
    ">>": "shiftRight",
  };
  let logicalIndex = 0;
  const expression = (node) => {
    check(--budget.remaining >= 0, "expression expansion limit exceeded");
    if (node?.type === "Literal") {
      check(Number.isSafeInteger(node.value) && node.value >= 0, "expected a nonnegative safe integer");
      return { kind: "literal", value: node.value };
    }
    if (node?.type === "Identifier") {
      const value = identifier(node);
      check(!["globalThis", "eval", "arguments"].includes(value), "unsupported dynamic reference");
      check(!localFunctions.has(value), "local function identity escapes");
      return { kind: "name", name: value };
    }
    if (node?.type === "BinaryExpression" && Object.hasOwn(binaryKinds, node.operator)) {
      check(
        !["%", "/"].includes(node.operator) || node.right?.type !== "Literal" || node.right.value !== 0,
        "zero divisor"
      );
      if (
        node.operator === "/" &&
        node.left?.type === "Literal" &&
        node.right?.type === "Literal" &&
        Number.isSafeInteger(node.left.value) &&
        Number.isSafeInteger(node.right.value) &&
        node.right.value !== 0
      )
        check(node.left.value % node.right.value === 0, "non-integral division");
      if (
        ["&", "|", "^"].includes(node.operator) &&
        node.left?.type === "Literal" &&
        node.right?.type === "Literal" &&
        Number.isSafeInteger(node.left.value) &&
        Number.isSafeInteger(node.right.value)
      )
        check(
          (node.operator === "&"
            ? node.left.value & node.right.value
            : node.operator === "|"
              ? node.left.value | node.right.value
              : node.left.value ^ node.right.value) >= 0,
          "negative bitwise result"
        );
      return {
        kind: binaryKinds[node.operator],
        left: expression(node.left),
        right: expression(node.right),
      };
    }
    if (node?.type === "LogicalExpression" && ["&&", "||"].includes(node.operator)) {
      const left = expression(node.left),
        right = expression(node.right);
      if (node.operator === "&&")
        return { kind: "conditional", test: left, consequent: right, alternative: { kind: "literal", value: 0 } };
      // A name that cannot be a JavaScript identifier keeps the evaluated
      // operand available without evaluating it a second time.
      const temporary = `@logical:${name}:${logicalIndex++}`;
      return {
        kind: "local",
        name: temporary,
        mutable: false,
        initial: left,
        body: {
          kind: "conditional",
          test: { kind: "name", name: temporary },
          consequent: { kind: "name", name: temporary },
          alternative: right,
        },
      };
    }
    if (node?.type === "ConditionalExpression")
      return {
        kind: "conditional",
        test: condition(node.test),
        consequent: expression(node.consequent),
        alternative: expression(node.alternate),
      };
    if (node?.type === "CallExpression" && !node.optional) {
      const target = identifier(node.callee);
      check(target !== name, "recursive calls are unsupported");
      if (localFunctions.has(target)) {
        check(!nestedDeclarations.has(target), "local function may be shadowed at its call site");
        const callee = expressionFunction(localFunctions.get(target), functions, [...stack, name], budget);
        check(
          callee.references.every((reference) => !nestedDeclarations.has(reference)),
          "local closure may be shadowed at its call site"
        );
        const args = node.arguments.map(expression);
        check(args.length === callee.parameters.length, "call argument count differs from parameters");
        return {
          kind: "invokeLocal",
          name: target,
          parameters: callee.parameters,
          arguments: args,
          body: callee.expression,
        };
      }
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
    if (node?.type === "BinaryExpression" && ["===", "==", "<", ">"].includes(node.operator))
      return {
        kind: ["===", "=="].includes(node.operator) ? "equal" : node.operator === "<" ? "less" : "greater",
        left: expression(node.left),
        right: expression(node.right),
      };
    if (node?.type === "BinaryExpression" && ["!==", "!=", ">=", "<="].includes(node.operator))
      return {
        kind: "conditional",
        test: {
          kind: ["!==", "!="].includes(node.operator) ? "equal" : node.operator === ">=" ? "less" : "greater",
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
    if (first.type === "FunctionDeclaration") {
      check(
        localFunctions.get(identifier(first.id)) === first && rest.length > 0,
        "unsupported nested function declaration"
      );
      return sequence(rest);
    }
    if (first.type === "ForStatement") {
      const init = first.init;
      check(
        init?.type === "VariableDeclaration" &&
          init.kind === "let" &&
          init.declarations.length === 1 &&
          rest.length > 0,
        "unsupported loop initializer"
      );
      const index = identifier(init.declarations[0].id);
      check(!["globalThis", "eval", "arguments"].includes(index), "unsupported loop binding");
      check(
        init.declarations[0].init?.type === "Literal" &&
          Number.isInteger(init.declarations[0].init.value) &&
          init.declarations[0].init.value >= 0 &&
          init.declarations[0].init.value <= 8,
        "unsupported loop initial value"
      );
      check(
        first.test?.type === "BinaryExpression" &&
          ["<", "<="].includes(first.test.operator) &&
          first.test.left?.type === "Identifier" &&
          first.test.left.name === index &&
          first.test.right?.type === "Literal" &&
          Number.isInteger(first.test.right.value) &&
          first.test.right.value >= 0 &&
          first.test.right.value <= 8,
        "unsupported loop bound"
      );
      check(
        (first.update?.type === "UpdateExpression" &&
          first.update.operator === "++" &&
          first.update.argument?.type === "Identifier" &&
          first.update.argument.name === index) ||
          (first.update?.type === "AssignmentExpression" &&
            first.update.operator === "+=" &&
            first.update.left?.type === "Identifier" &&
            first.update.left.name === index &&
            first.update.right?.type === "Literal" &&
            first.update.right.value === 1),
        "unsupported loop update"
      );
      check(
        first.body?.type === "BlockStatement" &&
          first.body.body.every((item) => {
            if (item.type !== "ExpressionStatement") return false;
            const target =
              item.expression?.type === "AssignmentExpression"
                ? item.expression.left
                : item.expression?.type === "UpdateExpression"
                  ? item.expression.argument
                  : null;
            return target?.type === "Identifier" && target.name !== index;
          }),
        "unsupported loop body"
      );
      const initial = init.declarations[0].init.value;
      const count = Math.max(0, first.test.right.value - initial + (first.test.operator === "<=" ? 1 : 0));
      check(count <= 8, "unsupported loop iteration count");
      const iterations = Array.from({ length: count }, () => [
        ...first.body.body,
        { type: "ExpressionStatement", expression: first.update },
      ]).flat();
      const loopBody = sequence([...iterations, { type: "ReturnStatement", argument: { type: "Literal", value: 0 } }]);
      return {
        kind: "followedBy",
        first: {
          kind: "local",
          name: index,
          mutable: true,
          initial: { kind: "literal", value: initial },
          body: loopBody,
        },
        continuation: sequence(rest),
      };
    }
    if (first.type === "VariableDeclaration") {
      check(
        (first.kind === "let" || first.kind === "const" || (first.kind === "var" && prefixVars.includes(first))) &&
          first.declarations.length === 1 &&
          rest.length > 0,
        "unsupported local declaration"
      );
      const declaration = first.declarations[0];
      const name = identifier(declaration.id);
      check(name !== "globalThis", "unsupported local binding");
      return {
        kind: "local",
        name,
        mutable: first.kind !== "const",
        initial: expression(declaration.init),
        body: sequence(rest),
      };
    }
    if (first.type === "ExpressionStatement" && rest.length > 0) {
      const assignment = first.expression;
      let name, value;
      if (
        assignment?.type === "AssignmentExpression" &&
        ["=", "+=", "*=", "-=", "%=", "/=", "&=", "|=", "^=", ">>>=", "<<=", ">>="].includes(assignment.operator)
      ) {
        name = identifier(assignment.left);
        check(
          !["%=", "/="].includes(assignment.operator) ||
            assignment.right?.type !== "Literal" ||
            assignment.right.value !== 0,
          "zero divisor"
        );
        value = expression(assignment.right);
        if (assignment.operator !== "=")
          value = {
            kind: binaryKinds[assignment.operator.slice(0, -1)],
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
    if (
      first.type === "IfStatement" &&
      !first.alternate &&
      rest.length > 0 &&
      first.consequent.type === "BlockStatement" &&
      first.consequent.body.every((item) => item.type === "ExpressionStatement")
    )
      return {
        kind: "followedBy",
        first: {
          kind: "conditional",
          test: condition(first.test),
          consequent: sequence([
            ...first.consequent.body,
            { type: "ReturnStatement", argument: { type: "Literal", value: 0 } },
          ]),
          alternative: { kind: "literal", value: 0 },
        },
        continuation: sequence(rest),
      };
    if (first.type === "TryStatement" && rest.length > 0) {
      const nonreturning = (block) => {
        const throws = block.body.at(-1)?.type === "ThrowStatement";
        check(
          block.body.every(
            (item, index) => item.type === "ExpressionStatement" || (throws && index === block.body.length - 1)
          ),
          "unsupported nonreturning try block"
        );
        return {
          ...block,
          body: throws
            ? block.body
            : [...block.body, { type: "ReturnStatement", argument: { type: "Literal", value: 0 } }],
        };
      };
      return {
        kind: "followedBy",
        first: returning({
          ...first,
          block: nonreturning(first.block),
          handler: first.handler ? { ...first.handler, body: nonreturning(first.handler.body) } : null,
        }),
        continuation: sequence(rest),
      };
    }
    if (first.type === "IfStatement" && !first.alternate && rest.length > 0)
      return {
        kind: "guard",
        test: condition(first.test),
        consequent: returning(first.consequent),
        alternative: sequence(rest),
      };
    if (first.type === "IfStatement" && first.alternate && rest.length > 0) {
      const branch = (statement) => {
        check(
          statement.type === "BlockStatement" && statement.body.every((item) => item.type === "ExpressionStatement"),
          "unsupported nonreturning branch"
        );
        return sequence([...statement.body, { type: "ReturnStatement", argument: { type: "Literal", value: 0 } }]);
      };
      return {
        kind: "followedBy",
        first: {
          kind: "conditional",
          test: condition(first.test),
          consequent: branch(first.consequent),
          alternative: branch(first.alternate),
        },
        continuation: sequence(rest),
      };
    }
    check(rest.length === 0, "unsupported statements after return");
    return returning(first);
  };
  const returning = (statement) => {
    if (statement?.type === "TryStatement") {
      if (statement.handler?.param) {
        const caught = identifier(statement.handler.param);
        const mentions = (node) => {
          if (!node || typeof node !== "object") return false;
          if (node.type === "Identifier" && node.name === caught) return true;
          return Object.values(node).some((value) => (Array.isArray(value) ? value.some(mentions) : mentions(value)));
        };
        check(!mentions(statement.handler.body), "used catch bindings are unsupported");
      }
      let body = sequence(statement.block.body);
      if (statement.handler) body = { kind: "catchError", body, handler: sequence(statement.handler.body.body) };
      if (!statement.finalizer) return body;
      check(
        statement.finalizer.body.every((item) => item.type === "ExpressionStatement"),
        "unsupported finalizer statement"
      );
      return {
        kind: "finally",
        body,
        finalizer: sequence([
          ...statement.finalizer.body,
          { type: "ReturnStatement", argument: { type: "Literal", value: 0 } },
        ]),
      };
    }
    if (statement?.type === "ReturnStatement") return expression(statement.argument);
    if (statement?.type === "ThrowStatement") {
      const observation = constantObservation(statement.argument);
      if (observation) return { kind: "raiseConstant", observation };
      return { kind: "raise", value: expression(statement.argument) };
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
      case "remainder":
      case "divide":
      case "bitAnd":
      case "bitOr":
      case "bitXor":
      case "unsignedShift":
      case "shiftLeft":
      case "shiftRight":
      case "equal":
      case "less":
      case "greater":
        return [...free(item.left, bound), ...free(item.right, bound)];
      case "conditional":
      case "guard":
        return [...free(item.test, bound), ...free(item.consequent, bound), ...free(item.alternative, bound)];
      case "local":
        return [...free(item.initial, bound), ...free(item.body, [item.name, ...bound])];
      case "catchError":
        return [...free(item.body, bound), ...free(item.handler, bound)];
      case "finally":
        return [...free(item.body, bound), ...free(item.finalizer, bound)];
      case "followedBy":
        return [...free(item.first, bound), ...free(item.continuation, bound)];
      case "assign":
        if (!bound.includes(item.name)) writes.add(item.name);
        return [
          ...(bound.includes(item.name) ? [] : [item.name]),
          ...free(item.value, bound),
          ...free(item.body, bound),
        ];
      case "raise":
        return free(item.value, bound);
      case "raiseConstant":
        return [];
      case "invoke":
        return [item.name, ...item.arguments.flatMap((argument) => free(argument, bound))];
      case "invokeLocal":
        return [
          ...item.arguments.flatMap((argument) => free(argument, bound)),
          ...free(item.body, [...item.parameters, ...bound]),
        ];
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
      (statement) =>
        (statement.type === "FunctionDeclaration" && statement.id?.name === functionName) ||
        (statement.type === "VariableDeclaration" &&
          statement.kind === "const" &&
          statement.declarations.length === 1 &&
          statement.declarations[0].id?.name === functionName &&
          ["ArrowFunctionExpression", "FunctionExpression"].includes(statement.declarations[0].init?.type))
    );
    check(matches.length === 1, "expected one returned local function declaration");
    const definition = matches[0];
    closure = definition.type === "FunctionDeclaration" ? definition : definition.declarations[0].init;
    declarations = declarations.filter((statement) => statement !== definition);
  }
  check(
    ["ArrowFunctionExpression", "FunctionDeclaration", "FunctionExpression"].includes(closure?.type) &&
      !closure.async &&
      !closure.generator &&
      (closure.type !== "FunctionExpression" || closure.id === null),
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
  const expression = node.type === "ExpressionStatement" ? node.expression : null;
  const literal =
    expression?.type === "UnaryExpression" && ["void", "!", "typeof"].includes(expression.operator)
      ? expression.argument
      : expression;
  if (primitiveLiteral(literal)) return { kind: "discardedLiteral" };
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
    check(["let", "const", "var"].includes(node.kind), "expected a supported declaration");
    check(
      name !== "globalThis" && d.init?.type === "Literal" && Number.isSafeInteger(d.init.value) && d.init.value >= 0,
      "expected a nonnegative safe integer initializer"
    );
    return {
      kind: node.kind === "const" ? "constant" : node.kind === "var" ? "hoistedVariable" : "variable",
      name,
      value: d.init.value,
    };
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
  check(
    expr?.type === "CallExpression" && !expr.optional,
    `unsupported statement: ${node.type}${expr?.type ? ` (${expr.type})` : ""}`
  );
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
  const observation = constantObservation(arg);
  if (observation) return { kind: "recordConstant", observation };
  if (arg?.type === "CallExpression" && !arg.optional) {
    check(
      arg.arguments.every(
        (value) => value.type === "Literal" && Number.isSafeInteger(value.value) && value.value >= 0 && value.value <= 8
      ),
      "recorded call arguments must be integer literals from 0 to 8"
    );
    return { kind: "recordCall", name: identifier(arg.callee), arguments: arg.arguments.map((value) => value.value) };
  }
  return { kind: "record", name: identifier(arg) };
}

function exportMappings(node) {
  check(
    node.specifiers.every((specifier) => specifier.type === "ExportSpecifier"),
    "unsupported export specifier"
  );
  return node.specifiers.map((specifier) => ({
    local: identifier(specifier.local),
    public: identifier(specifier.exported),
  }));
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
  const mappings = [];
  const body = parseModule(text);
  const functions = functionDeclarations([body]);
  for (const node of body) {
    if (node.type === "ExportNamedDeclaration" && !node.declaration && !node.source) {
      const names = exportMappings(node);
      mappings.push(...names);
      exports.push(...names.map((mapping) => mapping.local));
    } else if (node.type === "ExportNamedDeclaration" && node.declaration && !node.source) {
      const item = candidateStatement(node.declaration, functions);
      check(declarationName(item), "expected an exported declaration");
      exports.push(declarationName(item));
      mappings.push({ local: declarationName(item), public: declarationName(item) });
      statements.push({ ast: item, source: text.slice(node.declaration.start, node.declaration.end) });
    } else statements.push({ ast: candidateStatement(node, functions), source: text.slice(node.start, node.end) });
  }
  check(
    statements.length > 0 && statements.length <= 20 && exports.length > 0,
    "expected up to twenty statements and public exports"
  );
  return { statements, exports: [...new Set(exports)], mappings };
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
    check(module.aliasImports === undefined || typeof module.aliasImports === "boolean", "invalid import alias option");
    check(
      module.renameReexports === undefined || typeof module.renameReexports === "boolean",
      "invalid reexport alias option"
    );
    check(
      module.reexports === undefined ||
        (module.reexports && typeof module.reexports === "object" && !Array.isArray(module.reexports)),
      "invalid reexport map"
    );
    for (const [dependency, bindings] of Object.entries(module.reexports ?? {}))
      check(
        name !== spec.entry &&
          module.imports.includes(dependency) &&
          Array.isArray(bindings) &&
          bindings.every((binding) => typeof binding === "string") &&
          new Set(bindings).size === bindings.length,
        "invalid reexport declaration"
      );
    assigned.push(...module.statements);
  }
  check(assigned.length === count && new Set(assigned).size === count, "assign every statement once");
  return names;
}

const declarationName = (item) =>
  [
    "variable",
    "hoistedVariable",
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
  let reexportPrefix = "__split_reexport_";
  while ([...defs.keys()].some((binding) => binding.startsWith(reexportPrefix))) reexportPrefix += "_";
  const renamedReexport = (moduleName, binding) =>
    `${reexportPrefix}${names.indexOf(moduleName)}_${[...defs.keys()].indexOf(binding)}`;
  const provides = (moduleName, binding, seen = new Set()) => {
    if (defs.get(binding) === moduleName) return binding;
    if (seen.has(moduleName)) return null;
    const next = new Set(seen).add(moduleName);
    for (const [dependency, bindings] of Object.entries(spec.modules[moduleName].reexports ?? {})) {
      if (!bindings.includes(binding)) continue;
      const upstream = provides(dependency, binding, next);
      if (upstream) return spec.modules[moduleName].renameReexports ? renamedReexport(moduleName, binding) : upstream;
    }
    return null;
  };
  const neededExports = new Set(source.exports);
  for (const name of names) {
    const declared = new Set();
    for (const [dependency, bindings] of Object.entries(spec.modules[name].reexports ?? {}))
      for (const binding of bindings) {
        check(
          !declared.has(binding) &&
            defs.get(binding) !== name &&
            defs.get(binding) !== spec.entry &&
            provides(dependency, binding),
          `invalid reexport for ${binding}`
        );
        declared.add(binding);
        neededExports.add(binding);
      }
  }
  const providers = Object.fromEntries(names.map((name) => [name, new Map()]));
  for (const name of names)
    for (const binding of referencesByModule[name]) {
      const owner = defs.get(binding);
      check(owner, `missing definition: ${binding}`);
      if (owner !== name) {
        const dependencies = spec.modules[name].imports;
        const provider = dependencies.includes(owner) ? owner : dependencies.find((dep) => provides(dep, binding));
        check(provider, `missing dependency for ${binding}`);
        providers[name].set(binding, provider);
        neededExports.add(binding);
      }
    }
  let aliasPrefix = "__split_alias_";
  while ([...defs.keys()].some((binding) => binding.startsWith(aliasPrefix))) aliasPrefix += "_";
  const generatedAliases = new Map([...defs.keys()].map((binding, index) => [binding, `${aliasPrefix}${index}`]));
  const aliasExports = new Set();
  for (const name of names)
    if (spec.modules[name].aliasImports)
      for (const binding of referencesByModule[name])
        if (defs.get(binding) !== name) {
          check(defs.get(binding) !== spec.entry, "cannot add internal exports to the entry module");
          aliasExports.add(binding);
        }
  const result = {};
  for (const name of names) {
    const module = spec.modules[name];
    const imports = module.imports.map((dep) => {
      const bindings = referencesByModule[name].filter((binding) => providers[name].get(binding) === dep);
      const imported = bindings.map((binding) => {
        const exported = module.aliasImports ? generatedAliases.get(binding) : provides(dep, binding);
        return exported === binding ? binding : `${exported} as ${binding}`;
      });
      return bindings.length
        ? `import { ${imported.join(", ")} } from ${str(`./${dep}`)};`
        : `import ${str(`./${dep}`)};`;
    });
    const declarations = module.statements.map((index) => {
      const item = source.statements[index];
      const binding = declarationName(item.ast);
      const shouldExport =
        binding &&
        neededExports.has(binding) &&
        (name !== spec.entry ||
          (source.mappings.every((item) => item.local === item.public) && source.exports.includes(binding)));
      return `${shouldExport ? "export " : ""}${item.source}`;
    });
    const reexports =
      name === spec.entry
        ? source.mappings
            .filter(
              ({ local }) => defs.get(local) !== name || source.mappings.some((item) => item.local !== item.public)
            )
            .map(({ local, public: publicName }) => {
              check(defs.has(local), `missing exported definition: ${local}`);
              const suffix = defs.get(local) === name ? "" : ` from ${str(`./${defs.get(local)}`)}`;
              return `export { ${local}${local === publicName ? "" : ` as ${publicName}`} }${suffix};`;
            })
        : [];
    const relayed = Object.entries(module.reexports ?? {}).flatMap(([dependency, bindings]) =>
      bindings.flatMap((binding) => {
        const upstream = provides(dependency, binding);
        const exported = module.renameReexports ? renamedReexport(name, binding) : upstream;
        return [
          `export { ${upstream}${exported === upstream ? "" : ` as ${exported}`} } from ${str(`./${dependency}`)};`,
          ...(aliasExports.has(binding)
            ? [`export { ${generatedAliases.get(binding)} } from ${str(`./${dependency}`)};`]
            : []),
        ];
      })
    );
    const internalAliases = [...aliasExports]
      .filter((binding) => defs.get(binding) === name)
      .map((binding) => `export { ${binding} as ${generatedAliases.get(binding)} };`);
    result[name] = [...imports, ...declarations, ...internalAliases, ...relayed, ...reexports].join("\n") + "\n";
  }
  return result;
}

function outputAst(text, names, functions) {
  const imports = [];
  const exports = [];
  const mappings = [];
  const directMappings = [];
  let aliasedReexport = false;
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
      const links = node.specifiers.map((specifier) => {
        check(specifier.type === "ImportSpecifier", "unsupported import specifier");
        return { local: identifier(specifier.local), public: identifier(specifier.imported) };
      });
      imports.push({ dep, locals: links.map((item) => item.local), links, reexports: [], reexportLinks: [] });
    } else if (node.type === "ExportNamedDeclaration") {
      if (node.declaration) {
        const ast = candidateStatement(node.declaration, functions);
        check(declarationName(ast), "unsupported exported declaration");
        exports.push(declarationName(ast));
        mappings.push({ local: declarationName(ast), public: declarationName(ast) });
        directMappings.push({ local: declarationName(ast), public: declarationName(ast) });
        statements.push(ast);
      } else {
        const mapped = exportMappings(node);
        mappings.push(...mapped);
        if (node.source) {
          aliasedReexport ||= mapped.some((item) => item.local !== item.public);
          imports.push({
            dep: dependency(node),
            locals: [],
            links: [],
            reexports: mapped.map((item) => item.local),
            reexportLinks: mapped,
          });
        } else {
          directMappings.push(...mapped);
          exports.push(...mapped.map((item) => item.local));
        }
      }
    } else statements.push(candidateStatement(node, functions));
  }
  return { imports, exports, statements, mappings, directMappings, aliasedReexport };
}

const leanStrings = (values) => list(values.map(str));
const leanMappings = (mappings) =>
  list(mappings.map(({ local, public: publicName }) => `(${str(local)}, ${str(publicName)})`));
function leanExpression(expression) {
  switch (expression.kind) {
    case "literal":
      return `.literal ${expression.value}`;
    case "name":
      return `.name ${str(expression.name)}`;
    case "add":
    case "multiply":
    case "subtract":
    case "remainder":
    case "divide":
    case "bitAnd":
    case "bitOr":
    case "bitXor":
    case "unsignedShift":
    case "shiftLeft":
    case "shiftRight":
    case "equal":
    case "less":
    case "greater":
      return `.${expression.kind} (${leanExpression(expression.left)}) (${leanExpression(expression.right)})`;
    case "conditional":
    case "guard":
      return `.${expression.kind} (${leanExpression(expression.test)}) (${leanExpression(expression.consequent)}) (${leanExpression(expression.alternative)})`;
    case "local":
      return `.local ${str(expression.name)} ${expression.mutable} (${leanExpression(expression.initial)}) (${leanExpression(expression.body)})`;
    case "catchError":
      return `.catchError (${leanExpression(expression.body)}) (${leanExpression(expression.handler)})`;
    case "finally":
      return `.finally (${leanExpression(expression.body)}) (${leanExpression(expression.finalizer)})`;
    case "followedBy":
      return `.followedBy (${leanExpression(expression.first)}) (${leanExpression(expression.continuation)})`;
    case "assign":
      return `.assign ${str(expression.name)} (${leanExpression(expression.value)}) (${leanExpression(expression.body)})`;
    case "raise":
      return `.raise (${leanExpression(expression.value)})`;
    case "invoke":
    case "invokeLocal": {
      const argumentsTerm = expression.arguments.reduceRight(
        (tail, argument) => `.argumentsCons (${leanExpression(argument)}) (${tail})`,
        ".argumentsNil"
      );
      return `.${expression.kind} ${str(expression.name)} ${leanStrings(expression.parameters)} (${argumentsTerm}) (${leanExpression(expression.body)})`;
    }
    case "raiseConstant":
      return `.raisePrimitive (${leanObservation(expression.observation)})`;
  }
  throw new Error("unknown expression");
}
function leanStatement(item) {
  switch (item.kind) {
    case "discardedLiteral":
      return ".discardedLiteral";
    case "recordConstant":
      return `.recordPrimitive (${leanObservation(item.observation)})`;
    case "variable":
    case "hoistedVariable":
      return `.${item.kind} ${str(item.name)} ${item.value}`;
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
      return `.recordCall ${str(item.name)} ${list(item.arguments.map(String))}`;
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
      `({ dependency := ${i.dep}, locals := ${leanStrings(i.locals)}, links := ${leanMappings(i.links)}, reexports := ${leanStrings(i.reexports)}, reexportLinks := ${leanMappings(i.reexportLinks)} } : BindingImport)`
  );
  return `({ imports := ${list(imports)}, exports := ${leanStrings(module.exports)}, exportMappings := ${leanMappings(module.directMappings)}, statements := ${list(module.statements.map(leanStatement))}, assignment := ${list(assignment)} } : BindingModule)`;
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
  const script = `const events=[];globalThis.record=v=>events.push(v===undefined?{undefined:true}:typeof v==='bigint'?{bigint:String(v)}:typeof v==='number'&&(Object.is(v,-0)||!Number.isFinite(v))?{number:Object.is(v,-0)?'-0':String(v)}:v);import(${str(pathToFileURL(resolve(path)).href)}).then(m=>{const exports=Object.keys(m),results=[];let thrown;for(const call of ${str(calls)}){try{results.push(typeof call==='string'?m[call]():m[call.name](...call.args));}catch(value){thrown=value===null?{kind:'null'}:value===undefined?{kind:'undefined'}:typeof value==='number'?{kind:'number',value:Number.isNaN(value)?'NaN':Object.is(value,-0)?'-0':Number.isFinite(value)?value:String(value)}:typeof value==='string'||typeof value==='boolean'?{kind:typeof value,value}:typeof value==='bigint'?{kind:'bigint',value:String(value)}:{kind:'opaque'};break;}}console.log(JSON.stringify(${calls.length ? "{events,exports,results,...(thrown?{thrown}:{})}" : "{events,exports}"}));});`;
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
  const entryIndex = names.indexOf(spec.entry);
  if (
    actual[entryIndex].aliasedReexport &&
    actual.some((module) => module.imports.some((item) => item.dep === entryIndex))
  )
    return { result: "unknown", reason: "importing an entry module with renamed reexports is unsupported" };
  const terms = actual.map((module, i) => leanModule(module, spec.modules[names[i]].statements));
  const digest = (content) => createHash("sha256").update(content).digest("hex");
  const witnessPrefix = `\n-- Source SHA256: ${digest(text)}\n-- Output SHA256: ${names.map((name, i) => `${name}=${digest(files[i])}`).join(" ")}\ndef bindingSource : List BindingStmt := ${list(source.statements.map((item) => leanStatement(item.ast)))}\ndef bindingExports : List String := ${leanStrings(source.exports)}\ndef bindingPublicMappings : List (String × String) := ${leanMappings(source.mappings)}\ndef bindingOutputMappings : List (String × String) := ${leanMappings(actual[names.indexOf(spec.entry)].mappings)}\ndef bindingModules : List BindingModule := ${list(terms)}\ndef bindingEntry : Nat := ${names.indexOf(spec.entry)}\ntheorem binding_source_ready : (sourceBindingState bindingSource).isSome = true := by decide\ntheorem binding_output_ready : (outputBindingState bindingSource bindingModules bindingEntry).isSome = true := by decide\n#print axioms lexical_nearest_binding\n#print axioms accepted_reference_owner\n#print axioms binding_source_ready\n#print axioms binding_output_ready\n`;
  const prefix =
    witnessPrefix +
    `theorem binding_graph_valid : modulesValid bindingSource bindingExports bindingModules bindingEntry = true := by decide\ntheorem binding_public_mappings : bindingPublicMappings.length = bindingOutputMappings.length ∧ bindingPublicMappings.all (fun mapping => bindingOutputMappings.contains mapping) = true := by decide\n#print axioms binding_graph_valid\n#print axioms binding_public_mappings\n`;
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
  const closureRoot = (name) => {
    const seen = new Set();
    while (!seen.has(name)) {
      seen.add(name);
      const definition = source.statements.find((item) => item.ast.name === name)?.ast;
      if (definition?.kind === "capturedClosure") return name;
      if (definition?.kind !== "capturedAlias") return null;
      name = definition.target;
    }
    return null;
  };
  const exportRoots = source.mappings.map((mapping) => closureRoot(mapping.local));
  const directCapturedExports = [...new Set(exportRoots.filter((root) => root !== null))];
  const twoCaptured =
    exportRoots.every((root) => root !== null) &&
    directCapturedExports.length >= 1 &&
    directCapturedExports.length <= 2;
  const manyCaptured = exportRoots.every((root) => root !== null) && directCapturedExports.length > 2;
  const captureEnvironment = (definitions, name) =>
    name ? `(publicCapturedEnvironment ${definitions} ${str(name)})` : `(some ([] : List (String × String)))`;
  const sourceFirstEnv = captureEnvironment("bindingSource", directCapturedExports[0]);
  const sourceSecondEnv = captureEnvironment("bindingSource", directCapturedExports[1]);
  const outputFirstEnv = captureEnvironment(
    "(namespacedOutputDefinitions binding_output_key_names bindingModules)",
    directCapturedExports[0]
  );
  const outputSecondEnv = captureEnvironment(
    "(namespacedOutputDefinitions binding_output_key_names bindingModules)",
    directCapturedExports[1]
  );
  const pairedTableProof = source.mappings.reduceRight(
    (tail, mapping, index) =>
      `(.cons ${str(mapping.public)} (.${exportRoots[index] === directCapturedExports[0] ? "first" : "second"} _ _) ${tail})`,
    ".nil"
  );
  const outputCaptureNames = source.statements.flatMap((item) =>
    item.ast.kind === "capturedClosure" ? item.ast.keys.map((key) => `(${str(key)}, ${str(`output:${key}`)})`) : []
  );
  const manySourceEnvs = directCapturedExports.map((name) => captureEnvironment("bindingSource", name));
  const manyOutputEnvs = directCapturedExports.map((name) =>
    captureEnvironment("(namespacedOutputDefinitions binding_output_key_names bindingModules)", name)
  );
  const manyPairs = directCapturedExports.map(
    (_, index) => `(${manySourceEnvs[index]}.getD [], ${manyOutputEnvs[index]}.getD [])`
  );
  const membershipProof = (index) =>
    Array.from({ length: index }).reduce((result) => `(.tail _ ${result})`, "(.head _)");
  const manyTableProof = source.mappings.reduceRight(
    (tail, mapping, index) =>
      `(.cons ${str(mapping.public)} (.captured _ _ _ _ ${membershipProof(directCapturedExports.indexOf(exportRoots[index]))}) ${tail})`,
    ".nil"
  );
  const manyPresence = [...manySourceEnvs, ...manyOutputEnvs].map((env) => `${env}.isSome = true`).join(" ∧ ");
  const capturedRelation = twoCaptured
    ? `
def binding_output_key_names := ${list(outputCaptureNames)}
def binding_source_first := ${sourceFirstEnv}.getD []
def binding_source_second := ${sourceSecondEnv}.getD []
def binding_output_first := ${outputFirstEnv}.getD []
def binding_output_second := ${outputSecondEnv}.getD []
def binding_source_state := (sourceBindingState bindingSource).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
def binding_output_state := (namespacedOutputBindingState binding_output_key_names bindingSource bindingModules bindingEntry).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
def binding_source_table := ((publicCallableTable bindingSource bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).getD []
def binding_output_table := ((publicCallableTable (namespacedOutputDefinitions binding_output_key_names bindingModules) bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).getD []
theorem binding_namespaced_output_ready : (namespacedOutputBindingState binding_output_key_names bindingSource bindingModules bindingEntry).isSome = true := by decide
theorem binding_captured_tables_present :
    ((publicCallableTable bindingSource bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).isSome = true ∧
    ((publicCallableTable (namespacedOutputDefinitions binding_output_key_names bindingModules) bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).isSome = true := by decide
theorem binding_captured_environments_present :
    ${sourceFirstEnv}.isSome = true ∧
    ${sourceSecondEnv}.isSome = true ∧
    ${outputFirstEnv}.isSome = true ∧
    ${outputSecondEnv}.isSome = true := by decide
theorem binding_source_captures_disjoint : capturesDisjoint binding_source_first binding_source_second := by decide
theorem binding_output_captures_disjoint : capturesDisjoint binding_output_first binding_output_second := by decide
theorem binding_source_first_unique : CaptureKeysUnique binding_source_first := by decide
theorem binding_source_second_unique : CaptureKeysUnique binding_source_second := by decide
theorem binding_output_first_unique : CaptureKeysUnique binding_output_first := by decide
theorem binding_output_second_unique : CaptureKeysUnique binding_output_second := by decide
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
theorem binding_two_captured_calls_simulate (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation binding_source_first binding_output_first
      binding_source_second binding_output_second) leftOps rightOps)
    (leftFinal : ExecutionState Unit)
    (leftRun : runExecution binding_source_state leftOps = some leftFinal) :
    ∃ rightFinal, runExecution binding_output_state rightOps = some rightFinal ∧
      twoCapturedStatesRelated binding_source_first binding_output_first binding_source_second binding_output_second
        leftFinal rightFinal := by
  exact two_distinct_captured_call_sequence_simulates binding_source_first binding_output_first binding_source_second binding_output_second
    binding_source_captures_disjoint binding_output_captures_disjoint binding_source_first_unique binding_output_first_unique
    binding_source_second_unique binding_output_second_unique leftOps rightOps operations
    binding_source_state binding_output_state leftFinal binding_captured_initial_related leftRun
#print axioms binding_two_captured_calls_simulate
theorem binding_two_captured_calls_complete_together (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedTwoCapturedOperation binding_source_first binding_output_first
      binding_source_second binding_output_second) leftOps rightOps) :
    (runExecution binding_source_state leftOps).isSome = (runExecution binding_output_state rightOps).isSome := by
  exact two_distinct_captured_runs_complete_together binding_source_first binding_output_first
    binding_source_second binding_output_second binding_source_captures_disjoint binding_output_captures_disjoint
    binding_source_first_unique binding_output_first_unique binding_source_second_unique binding_output_second_unique
    leftOps rightOps operations binding_source_state binding_output_state binding_captured_initial_related
#print axioms binding_two_captured_calls_complete_together
theorem binding_captured_tables_paired : PairedCapturedTables binding_source_first binding_output_first
    binding_source_second binding_output_second binding_source_table binding_output_table := by
  exact ${pairedTableProof}
theorem binding_exported_captured_calls (calls : List (String × List Nat))
    (leftFinal rightFinal : ExecutionState Unit)
    (leftRun : runArgumentCalls binding_source_table binding_source_state calls = some leftFinal)
    (rightRun : runArgumentCalls binding_output_table binding_output_state calls = some rightFinal) :
    twoCapturedStatesRelated binding_source_first binding_output_first binding_source_second binding_output_second
      leftFinal rightFinal := by
  exact two_captured_argument_calls_related binding_source_first binding_output_first binding_source_second binding_output_second
    binding_source_captures_disjoint binding_output_captures_disjoint binding_source_table binding_output_table
    (paired_captured_table_call binding_source_first binding_output_first binding_source_second binding_output_second
      binding_source_table binding_output_table binding_captured_tables_paired) calls
    binding_source_state binding_output_state leftFinal rightFinal binding_captured_initial_related leftRun rightRun
theorem binding_exported_captured_calls_simulate (calls : List (String × List Nat))
    (leftFinal : ExecutionState Unit)
    (leftRun : runArgumentCalls binding_source_table binding_source_state calls = some leftFinal) :
    ∃ rightFinal, runArgumentCalls binding_output_table binding_output_state calls = some rightFinal ∧
      twoCapturedStatesRelated binding_source_first binding_output_first binding_source_second binding_output_second
        leftFinal rightFinal := by
  exact two_distinct_captured_argument_calls_simulate binding_source_first binding_output_first
    binding_source_second binding_output_second binding_source_captures_disjoint binding_output_captures_disjoint
    binding_source_first_unique binding_output_first_unique binding_source_second_unique binding_output_second_unique
    binding_source_table binding_output_table binding_captured_tables_paired calls
    binding_source_state binding_output_state leftFinal binding_captured_initial_related leftRun
theorem binding_captured_observations (calls : List (String × List Nat)) :
    (runArgumentCalls binding_source_table binding_source_state calls).map capturedCallObservation =
    (runArgumentCalls binding_output_table binding_output_state calls).map capturedCallObservation := by
  exact two_distinct_captured_argument_calls_observations binding_source_first binding_output_first
    binding_source_second binding_output_second binding_source_captures_disjoint binding_output_captures_disjoint
    binding_source_first_unique binding_output_first_unique binding_source_second_unique binding_output_second_unique
    binding_source_table binding_output_table binding_captured_tables_paired calls
    binding_source_state binding_output_state binding_captured_initial_related
#print axioms binding_captured_tables_paired
#print axioms binding_namespaced_output_ready
#print axioms binding_captured_tables_present
#print axioms binding_captured_observations
#print axioms binding_exported_captured_calls
#print axioms binding_exported_captured_calls_simulate
#print axioms binding_captured_environments_present
#print axioms binding_source_captures_disjoint
#print axioms binding_output_captures_disjoint
#print axioms binding_captured_initial_related
#print axioms binding_two_captured_calls
`
    : manyCaptured
      ? `
def binding_output_key_names := ${list(outputCaptureNames)}
def binding_capture_pairs : List CapturePair := ${list(manyPairs)}
def binding_source_state := (sourceBindingState bindingSource).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
def binding_output_state := (namespacedOutputBindingState binding_output_key_names bindingSource bindingModules bindingEntry).getD (⟨[], [], [], none, [], none, []⟩ : ExecutionState Unit)
def binding_source_table := ((publicCallableTable bindingSource bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).getD []
def binding_output_table := ((publicCallableTable (namespacedOutputDefinitions binding_output_key_names bindingModules) bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).getD []
theorem binding_namespaced_output_ready : (namespacedOutputBindingState binding_output_key_names bindingSource bindingModules bindingEntry).isSome = true := by decide
theorem binding_captured_tables_present : ((publicCallableTable bindingSource bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).isSome = true ∧
  ((publicCallableTable (namespacedOutputDefinitions binding_output_key_names bindingModules) bindingExports).bind (fun table => aliasPublicTable table bindingPublicMappings)).isSome = true := by decide
theorem binding_captured_environments_present : ${manyPresence} := by decide
theorem binding_capture_pairs_unique : capturePairsUnique binding_capture_pairs := by decide
theorem binding_capture_pairs_disjoint : capturePairsDisjoint binding_capture_pairs := by decide
theorem binding_capture_pairs_initial : capturePairsRelated binding_capture_pairs binding_source_state binding_output_state := by decide
theorem binding_captured_tables_paired : PairedCapturedListTables binding_capture_pairs binding_source_table binding_output_table := by
  exact ${manyTableProof}
theorem binding_captured_observations (calls : List (String × List Nat)) :
    (runArgumentCalls binding_source_table binding_source_state calls).map capturedCallObservation =
    (runArgumentCalls binding_output_table binding_output_state calls).map capturedCallObservation := by
  exact captured_pairs_argument_calls_observations binding_capture_pairs binding_capture_pairs_unique
    binding_capture_pairs_disjoint binding_source_table binding_output_table binding_captured_tables_paired
    (${manySourceEnvs[0]}.getD []) (${manyOutputEnvs[0]}.getD []) (.head _) calls
    binding_source_state binding_output_state binding_capture_pairs_initial
#print axioms binding_namespaced_output_ready
#print axioms binding_captured_tables_present
#print axioms binding_captured_environments_present
#print axioms binding_capture_pairs_unique
#print axioms binding_capture_pairs_disjoint
#print axioms binding_capture_pairs_initial
#print axioms binding_captured_tables_paired
#print axioms binding_captured_observations
`
      : "";
  const aliasTable = hasExpressions ? "publicCallableTable" : "publicFunctionTable";
  const aliasRunner = hasExpressions ? "runArgumentCalls" : "runExportedCalls";
  const aliasCallType = hasExpressions ? "List (String × List Nat)" : "List String";
  const aliasProof = `
theorem binding_aliased_future_calls (calls : ${aliasCallType}) :
  (sourceBindingState bindingSource).bind (fun state => (${aliasTable} bindingSource bindingExports).bind (fun table => (aliasPublicTable table bindingPublicMappings).bind (fun aliases => ${aliasRunner} aliases state calls))) =
  (outputBindingState bindingSource bindingModules bindingEntry).bind (fun state => (${aliasTable} (outputDefinitions bindingModules) bindingExports).bind (fun table => (aliasPublicTable table bindingPublicMappings).bind (fun aliases => ${aliasRunner} aliases state calls))) := by
  rw [binding_initialization, binding_public_table]
#print axioms binding_aliased_future_calls
`;
  const same = (hasExpressions ? expressionSame : legacySame) + aliasProof;
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
  const proof = runLean(
    prefix + same + `\n#eval IO.println s!"witness-calls-stable:{witnessedCallableImportsStable bindingModules}"\n`
  );
  if (proof.error || proof.status === null)
    return {
      result: "unknown",
      reason: "Lean could not be run",
      detail: proof.error
        ? String(proof.error)
        : `Lean terminated by ${proof.signal ?? "unknown signal"}: ${(proof.stderr || proof.stdout || "").slice(-4000)}`,
    };
  if (
    !["lexical_nearest_binding", "accepted_reference_owner", "binding_source_ready", "binding_output_ready"].every(
      (name) => has(proof, name)
    )
  )
    return {
      result: "unknown",
      reason: "Lean could not validate the extracted bindings, statements or module graph",
      detail: proof.stdout || proof.stderr,
    };
  const legacyValid =
    proof.status === 0 &&
    [
      "binding_public_mappings",
      "binding_aliased_future_calls",
      "binding_initialization",
      "binding_public_table",
      "binding_public_table_ready",
      "binding_future_calls",
      "binding_return_values",
      "binding_throw_values",
    ].every((name) => has(proof, name));
  const relationProof = twoCaptured || manyCaptured ? runLean(prefix + capturedRelation) : null;
  const relationValid =
    relationProof?.status === 0 &&
    (manyCaptured
      ? [
          "binding_namespaced_output_ready",
          "binding_captured_tables_present",
          "binding_captured_environments_present",
          "binding_capture_pairs_unique",
          "binding_capture_pairs_disjoint",
          "binding_capture_pairs_initial",
          "binding_captured_tables_paired",
          "binding_captured_observations",
        ]
      : [
          "binding_captured_tables_present",
          "binding_namespaced_output_ready",
          "binding_captured_observations",
          "binding_captured_environments_present",
          "binding_source_captures_disjoint",
          "binding_output_captures_disjoint",
          "binding_captured_initial_related",
          "binding_two_captured_calls",
          "binding_two_captured_calls_simulate",
          "binding_two_captured_calls_complete_together",
          "binding_exported_captured_calls",
          "binding_exported_captured_calls_simulate",
          "binding_captured_tables_paired",
        ]
    ).every((name) => has(relationProof, name));
  if (legacyValid || relationValid) {
    return {
      result: "proved under assumptions",
      certificate: prefix + (legacyValid ? same : "") + (relationValid ? capturedRelation : ""),
      assumptions: [
        "The Acorn-to-Lean conversion, including local object and array scalarization when used, faithfully represents the actual source and output files",
        "The modeled module loading, linking and execution match ECMAScript for the supported syntax",
        "All initialized numeric binding values, exported-function arguments and modeled numeric expression intermediates remain nonnegative safe integers other than negative zero; recorded integer literals may also be negative safe integers or negative zero, and the supported constant zero-divisions may be recorded; other remainder and division divisors are nonzero, division results are integers, and bitwise results are nonnegative",
        ...(source.statements.some((item) => item.ast.kind === "hoistedVariable")
          ? [
              "Before a var initializer, the model supports only direct globalThis.record observations of its undefined value",
            ]
          : []),
        ...(hasExpressions
          ? [
              "Expression functions and captured closures receive exactly their declared number of numeric arguments; other exports are called without arguments",
            ]
          : []),
        "globalThis.record is a stable synchronous host function which returns normally without reentry or asynchronous effects",
        "Only initialization calls, their record observations, and calls through the entry exports after successful initialization are observed; loading, reflection, source text and identity are excluded",
        "Supported thrown constants include signed safe integers, negative zero, 0/0, 1/0, -1/0, BigInt literals, booleans, null, undefined and UTF-16 strings; other numeric expressions remain nonnegative safe integers; a throw ends the observed call sequence",
      ],
    };
  }
  const original = execute(sourcePath);
  const output = execute(resolve(out, spec.entry));
  const name = [...new Set([...original.exports, ...output.exports])].find(
    (item) => original.exports.includes(item) !== output.exports.includes(item)
  );
  if (name) {
    const certificate =
      witnessPrefix +
      `
theorem binding_export_difference :
  (bindingPublicMappings.map Prod.snd).contains ${str(name)} ≠
    (bindingOutputMappings.map Prod.snd).contains ${str(name)} := by decide
#print axioms binding_export_difference
`;
    const checked = runLean(certificate);
    if (checked.status === 0 && has(checked, "binding_export_difference"))
      return { result: "unsafe", original, output, certificate };
  }
  // Saturate bounds so a syntactically large expression cannot construct an
  // exponentially large BigInt while checking whether it exceeds the limit.
  const witnessLimit = BigInt(Number.MAX_SAFE_INTEGER);
  const capWitnessBound = (value) => (value > witnessLimit ? witnessLimit + 1n : value);
  let witnessObservedCallBound = null;
  const expressionBound = (expression, bound) => {
    if (expression.kind === "literal") return capWitnessBound(BigInt(expression.value));
    if (expression.kind === "name") return bound;
    // A nested call can mutate a local observed by a later operand. Propagate
    // the bound from left to right, just as JavaScript evaluates the operands.
    const operands = () => {
      const left = expressionBound(expression.left, bound);
      const right = expressionBound(expression.right, left > bound ? left : bound);
      return [left, right];
    };
    if (expression.kind === "add") {
      const [left, right] = operands();
      return capWitnessBound(left + right);
    }
    if (expression.kind === "equal" || expression.kind === "less" || expression.kind === "greater") {
      const [left, right] = operands();
      return [1n, left, right].reduce((a, b) => (a > b ? a : b));
    }
    if (expression.kind === "multiply") {
      const [left, right] = operands();
      return capWitnessBound([left, right, left * right].reduce((a, b) => (a > b ? a : b)));
    }
    if (["unsignedShift", "shiftLeft", "shiftRight"].includes(expression.kind)) {
      const [left, right] = operands();
      return [left, right, 4294967295n].reduce((a, b) => (a > b ? a : b));
    }
    if (expression.kind === "bitOr" || expression.kind === "bitXor") {
      const [left, right] = operands();
      return capWitnessBound(left + right);
    }
    if (["remainder", "divide", "bitAnd"].includes(expression.kind)) {
      const [left, right] = operands();
      return left > right ? left : right;
    }
    if (expression.kind === "subtract") {
      const [left, right] = operands();
      return left > right ? left : right;
    }
    if (expression.kind === "conditional" || expression.kind === "guard") {
      const test = expressionBound(expression.test, bound);
      const branchBound = test > bound ? test : bound;
      return [
        test,
        expressionBound(expression.consequent, branchBound),
        expressionBound(expression.alternative, branchBound),
      ].reduce((a, b) => (a > b ? a : b));
    }
    if (expression.kind === "local") {
      const initial = expressionBound(expression.initial, bound);
      const body = expressionBound(expression.body, initial > bound ? initial : bound);
      return initial > body ? initial : body;
    }
    if (expression.kind === "catchError") {
      const body = expressionBound(expression.body, bound);
      const handler = expressionBound(expression.handler, body > bound ? body : bound);
      return body > handler ? body : handler;
    }
    if (expression.kind === "finally") {
      const body = expressionBound(expression.body, bound);
      const finalizer = expressionBound(expression.finalizer, body > bound ? body : bound);
      return body > finalizer ? body : finalizer;
    }
    if (expression.kind === "followedBy") {
      const first = expressionBound(expression.first, bound);
      const continuation = expressionBound(expression.continuation, first > bound ? first : bound);
      return first > continuation ? first : continuation;
    }
    if (expression.kind === "assign") {
      const value = expressionBound(expression.value, bound);
      const body = expressionBound(expression.body, value > bound ? value : bound);
      return value > body ? value : body;
    }
    if (expression.kind === "raise") return expressionBound(expression.value, bound);
    if (expression.kind === "raiseConstant") return 0n;
    if (expression.kind === "invoke" || expression.kind === "invokeLocal") {
      const callBound = expression.arguments.reduce((largest, argument) => {
        const value = expressionBound(argument, largest);
        return value > largest ? value : largest;
      }, bound);
      // Track arguments of nested calls so redirected bodies are also checked
      // at every input bound they can receive.
      if (witnessObservedCallBound !== null && callBound > witnessObservedCallBound)
        witnessObservedCallBound = callBound;
      const body = expressionBound(expression.body, callBound);
      return body > callBound ? body : callBound;
    }
    throw new Error("unknown expression");
  };
  // Ask the existing Lean resolver for the call graph. The frontend supplies
  // syntactic call sites and then follows only resolved declaration identities;
  // an incomplete graph falls back to the conservative whole-program bound.
  let witnessGraph;
  const witnessReachableItems = (calls) => {
    if (witnessGraph === undefined) {
      const nodes = [];
      const byIdentity = new Map();
      const queries = [];
      const rootQueries = new Map();
      const directCalls = (expression, result = new Set()) => {
        if (!expression || typeof expression !== "object") return result;
        if (expression.kind === "invoke") {
          result.add(expression.name);
          for (const argument of expression.arguments) directCalls(argument, result);
        } else {
          for (const value of Object.values(expression)) {
            if (Array.isArray(value)) for (const part of value) directCalls(part, result);
            else directCalls(value, result);
          }
        }
        return result;
      };
      for (const [moduleIndex, module] of actual.entries()) {
        const assignments = spec.modules[names[moduleIndex]].statements;
        if (assignments.length !== module.statements.length) return (witnessGraph = null);
        for (const [position, statement] of module.statements.entries()) {
          if (["capturedFactory", "capturedClosure", "capturedAlias"].includes(statement.kind))
            return (witnessGraph = null);
          const identity = `${moduleIndex}:${assignments[position]}`;
          if (byIdentity.has(identity)) return (witnessGraph = null);
          const index = nodes.length;
          nodes.push({ statement, edges: [] });
          byIdentity.set(identity, index);
          if (statement.kind === "expressionFunction")
            for (const name of directCalls(statement.expression))
              queries.push({ kind: "edge", index, moduleIndex, name });
          if (statement.kind === "recordCall")
            queries.push({ kind: "initial", index, moduleIndex, name: statement.name });
        }
      }
      for (const mapping of actual[entryIndex].mappings) queries.push({ kind: "public", name: mapping.public });
      const program = queries
        .map((query, index) => {
          const resolved =
            query.kind === "public"
              ? `resolveActualExportFuel bindingModules (bindingModules.length + 1) bindingEntry ${str(query.name)}`
              : `resolveActualReference bindingModules ${query.moduleIndex} ${str(query.name)}`;
          return `#eval IO.println (match ${resolved} with | some result => s!"witness-edge:${index}:{result.1}:{result.2.1}" | none => "witness-edge:${index}:missing")`;
        })
        .join("\n");
      const resolved = runLean(witnessPrefix + "\n" + program + "\n");
      if (resolved.error || resolved.status !== 0 || typeof resolved.stdout !== "string") return (witnessGraph = null);
      const lines = new Map();
      for (const match of resolved.stdout.matchAll(/^witness-edge:(\d+):(\d+):(\d+)$/gm))
        lines.set(Number(match[1]), `${match[2]}:${match[3]}`);
      if (lines.size !== queries.length) return (witnessGraph = null);
      for (const [index, query] of queries.entries()) {
        const target = byIdentity.get(lines.get(index));
        if (target === undefined) return (witnessGraph = null);
        if (query.kind === "edge") nodes[query.index].edges.push(target);
        else if (query.kind === "initial") nodes[query.index].edges.push(target);
        else rootQueries.set(query.name, target);
      }
      witnessGraph = { nodes, roots: rootQueries };
    }
    if (!witnessGraph) return null;
    const pending = witnessGraph.nodes.flatMap((node) => (node.statement.kind === "recordCall" ? node.edges : []));
    for (const call of calls) {
      const name = typeof call === "string" ? call : call.name;
      const root = witnessGraph.roots.get(name);
      if (root === undefined) return null;
      pending.push(root);
    }
    const reachable = new Set();
    while (pending.length) {
      const index = pending.pop();
      if (reachable.has(index)) continue;
      reachable.add(index);
      pending.push(...witnessGraph.nodes[index].edges);
    }
    return [...reachable].map((index) => witnessGraph.nodes[index].statement);
  };
  // The Lean arithmetic model is conditional on exact JavaScript integers.
  // Bound both extracted programs, including initialization-call arguments,
  // before accepting a concrete Node difference as a certified witness.
  const witnessNumbersBounded = (calls) => {
    const sourceItems = source.statements.map((item) => item.ast);
    const outputItems = actual.flatMap((module) => module.statements);
    const programs = [sourceItems, outputItems];
    let maximum = 2;
    for (const item of programs.flat()) {
      if (["variable", "hoistedVariable", "constant"].includes(item.kind)) maximum = Math.max(maximum, item.value);
      if (["recordCall", "capturedClosure"].includes(item.kind))
        for (const value of item.arguments) maximum = Math.max(maximum, value);
    }
    for (const call of calls)
      if (typeof call !== "string") for (const value of call.args) maximum = Math.max(maximum, value);
    const initialCalls = Math.max(
      ...programs.map((items) => items.filter((item) => ["call", "recordCall"].includes(item.kind)).length)
    );
    const limit = witnessLimit;
    const growthSteps = initialCalls + calls.length + 1;
    // maximum is at least 2, so this is already beyond the safe range. Avoid
    // allocating a huge BigInt for a program with many initialization calls.
    if (growthSteps >= 53) return false;
    let bound = BigInt(maximum) << BigInt(growthSteps);
    if (bound > limit) return false;
    const callsStable = proof.stdout.includes("witness-calls-stable:true");
    const reachable = callsStable ? null : witnessReachableItems(calls);
    if (reachable !== null)
      for (const item of sourceItems)
        if (item.kind === "expressionFunction" && expressionBound(item.expression, bound) > limit) return false;
    const items = reachable ?? programs.flat();
    // Stable callable imports use the already-inlined bodies. Otherwise, a
    // redirected call may select any output function; widen call inputs
    // through at most one layer per function. Recursive redirected graphs
    // cannot produce a finite witnessed Lean lowering and are rejected there.
    const rounds = callsStable ? 1 : 1 + items.filter((item) => item.kind === "expressionFunction").length;
    try {
      for (let round = 0; round < rounds; round++) {
        witnessObservedCallBound = callsStable ? null : bound;
        const factories = [];
        let stateBound = bound;
        for (const item of items) {
          if (item.kind === "expressionFunction") {
            const resultBound = expressionBound(item.expression, bound);
            if (resultBound > limit) return false;
            // The replacement body can return a larger value than the
            // frontend's inlined body. Later expressions may consume it.
            if (witnessObservedCallBound !== null && resultBound > witnessObservedCallBound)
              witnessObservedCallBound = resultBound;
          }
          if (item.kind === "capturedFactory") {
            let localBound = bound;
            for (const local of item.locals) {
              const initial = expressionBound(local.initial, localBound);
              if (initial > localBound) localBound = initial;
              if (localBound > limit) return false;
            }
            factories.push(item.body);
            if (expressionBound(item.body, localBound) > limit) return false;
            if (localBound > stateBound) stateBound = localBound;
          }
        }
        for (let i = 0; i < calls.length; i++) {
          for (const body of factories) {
            const next = expressionBound(body, stateBound);
            if (next > stateBound) stateBound = next;
            if (stateBound > limit) return false;
          }
        }
        if (callsStable || witnessObservedCallBound <= bound) return true;
        bound = witnessObservedCallBound;
        if (bound > limit) return false;
      }
      return true;
    } finally {
      witnessObservedCallBound = null;
    }
  };
  const different = `\ntheorem binding_difference : sourceBindingState bindingSource ≠ witnessedOutputState bindingSource bindingModules bindingEntry := by decide\n#print axioms binding_difference\n`;
  let witnessFailure = proof.stdout || proof.stderr;
  const witnessed = (original, output, calls) => {
    if (!witnessNumbersBounded(calls)) {
      witnessFailure = "numeric bounds could not establish safe-integer witness execution";
      return null;
    }
    const events = (trace) => {
      const undefinedValue = (value) =>
        value && typeof value === "object" && value.undefined === true && Object.keys(value).length === 1;
      const negativeZero = (value) =>
        value && typeof value === "object" && value.number === "-0" && Object.keys(value).length === 1;
      const specialNumber = (value) =>
        value &&
        typeof value === "object" &&
        ["NaN", "Infinity", "-Infinity"].includes(value.number) &&
        Object.keys(value).length === 1;
      const bigInteger = (value) =>
        value &&
        typeof value === "object" &&
        typeof value.bigint === "string" &&
        /^(?:0|[1-9]\d*|-[1-9]\d*)$(?![\s\S])/.test(value.bigint) &&
        Object.keys(value).length === 1;
      if (
        !trace.events.every(
          (value) =>
            undefinedValue(value) ||
            negativeZero(value) ||
            specialNumber(value) ||
            bigInteger(value) ||
            value === null ||
            typeof value === "boolean" ||
            supportedText(value) ||
            (Number.isSafeInteger(value) && !Object.is(value, -0))
        )
      )
        return null;
      return list(
        trace.events.map((value) =>
          undefinedValue(value)
            ? ".undefined"
            : negativeZero(value)
              ? ".negativeZero"
              : specialNumber(value)
                ? { NaN: ".notANumber", Infinity: ".positiveInfinity", "-Infinity": ".negativeInfinity" }[value.number]
                : bigInteger(value)
                  ? `.bigInteger ${value.bigint.startsWith("-")} ${value.bigint.replace(/^-/, "")}`
                  : value === null
                    ? ".nullValue"
                    : typeof value === "boolean"
                      ? `.boolean ${value}`
                      : typeof value === "string"
                        ? `.text ${leanText(value)}`
                        : value < 0
                          ? `.negativeNumber ${-value}`
                          : `.number ${value}`
        )
      );
    };
    const returns = (trace) => {
      if (!trace.results.every((value) => value === null || (Number.isSafeInteger(value) && value >= 0))) return null;
      return list(trace.results.map((value) => (value === null ? "none" : `some ${value}`)));
    };
    const thrown = (trace) => {
      if (!Object.hasOwn(trace, "thrown")) return "none";
      const value = trace.thrown;
      if (value.kind === "undefined") return "some (.undefined : ThrownValue)";
      if (value.kind === "null") return "some (.nullValue : ThrownValue)";
      if (value.kind === "boolean" && typeof value.value === "boolean") return `some (.boolean ${value.value})`;
      if (value.kind === "string" && supportedText(value.value)) return `some (.text ${leanText(value.value)})`;
      if (value.kind === "number") {
        const special = {
          "-0": "negativeZero",
          NaN: "notANumber",
          Infinity: "positiveInfinity",
          "-Infinity": "negativeInfinity",
        };
        if (Object.hasOwn(special, value.value)) return `some (.${special[value.value]} : ThrownValue)`;
        if (Number.isSafeInteger(value.value))
          return value.value < 0 ? `some (.negativeNumber ${-value.value})` : `some (.number ${value.value})`;
      }
      if (
        value.kind === "bigint" &&
        typeof value.value === "string" &&
        /^(?:0|[1-9]\d*|-[1-9]\d*)$(?![\s\S])/.test(value.value)
      )
        return `some (.bigInteger ${value.value.startsWith("-")} ${value.value.replace(/^-/, "")})`;
      return null;
    };
    const sourceEvents = events(original),
      outputEvents = events(output);
    if (sourceEvents === null || outputEvents === null) return null;
    let left, right, expectedLeft, expectedRight;
    if (calls.length === 0) {
      left = "(sourceBindingState bindingSource).map (fun state => state.events)";
      right = "(witnessedOutputState bindingSource bindingModules bindingEntry).map (fun state => state.events)";
      expectedLeft = `some ${sourceEvents}`;
      expectedRight = `some ${outputEvents}`;
    } else {
      const sourceReturns = returns(original),
        outputReturns = returns(output),
        sourceThrown = thrown(original),
        outputThrown = thrown(output);
      if (sourceReturns === null || outputReturns === null || sourceThrown === null || outputThrown === null)
        return null;
      const leanCalls = hasExpressions
        ? list(calls.map((call) => `(${str(call.name)}, ${list(call.args.map(String))})`))
        : leanStrings(calls);
      const table = hasExpressions ? "publicCallableTable" : "publicFunctionTable";
      const runner = hasExpressions ? "runArgumentCalls" : "runExportedCalls";
      left = `((sourceBindingState bindingSource).bind (fun state => (${table} bindingSource bindingExports).bind (fun table => (aliasPublicTable table bindingPublicMappings).bind (fun aliases => ${runner} aliases state ${leanCalls})))).map (fun state => (state.events, state.returns, state.thrown))`;
      right = `((witnessedOutputState bindingSource bindingModules bindingEntry).bind (fun state => (witnessedOutputDefinitions bindingModules).bind (fun definitions => (witnessedPublicMappings bindingModules bindingEntry bindingOutputMappings).bind (fun mappings => (${table} definitions (mappings.map Prod.fst)).bind (fun table => (aliasPublicTable table mappings).bind (fun aliases => ${runner} aliases state ${leanCalls})))))).map (fun state => (state.events, state.returns, state.thrown))`;
      expectedLeft = `some (${sourceEvents}, ${sourceReturns}, ${sourceThrown})`;
      expectedRight = `some (${outputEvents}, ${outputReturns}, ${outputThrown})`;
    }
    const witness = `\ntheorem binding_witness_original : ${left} = ${expectedLeft} := by decide\ntheorem binding_witness_output : ${right} = ${expectedRight} := by decide\ntheorem binding_witness_difference : ${left} ≠ ${right} := by decide\n#print axioms binding_witness_original\n#print axioms binding_witness_output\n#print axioms binding_witness_difference\n`;
    // Call observations can differ even when initialization states are equal,
    // for example when an emitted function body has changed.
    const initializationDifference = calls.length === 0 ? different : "";
    const required = ["binding_witness_original", "binding_witness_output", "binding_witness_difference"];
    if (initializationDifference) required.push("binding_difference");
    // A changed emitted body can make modulesValid false. The witness only
    // needs the extracted programs and a checked difference in observations;
    // it must not assume the equivalence-side graph validation theorem.
    const certificate = witnessPrefix + initializationDifference + witness;
    const checked = runLean(certificate);
    if (checked.status === 0 && required.every((name) => has(checked, name))) return certificate;
    witnessFailure =
      checked.status === null
        ? `Lean terminated by ${checked.signal ?? String(checked.error)}: ${(checked.stderr || checked.stdout || "").slice(-4000)}`
        : (checked.stdout || checked.stderr).slice(-6000);
    return null;
  };
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
    (max, item) =>
      ["variable", "hoistedVariable", "constant"].includes(item.ast.kind) ? Math.max(max, item.ast.value) : max,
    1
  );
  const initialCalls = source.statements.filter((item) => ["call", "recordCall"].includes(item.ast.kind)).length;
  const callable = source.mappings.map((mapping) => mapping.public);
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
  const localName = (name) => source.mappings.find((mapping) => mapping.public === name)?.local ?? name;
  const candidates = hasExpressions
    ? callable.flatMap((name) => {
        const definition = resolveCapturedDefinition(localName(name));
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

  for (const calls of candidates.slice(0, 64)) {
    if (hasExpressions) {
      const definition = resolveCapturedDefinition(localName(calls[0].name));
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
