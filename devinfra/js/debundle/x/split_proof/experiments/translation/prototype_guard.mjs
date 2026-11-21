// A diagnostic spike, not an input to the checker. It recognizes a closed module containing
// a local class and either a direct construction or a helper that only forwards to it.
import * as acorn from "acorn";

const fail = (reason) => ({ candidate: false, reason });
const name = (node) => (node?.type === "Identifier" ? node.name : null);
const member = (node) =>
  node?.type === "MemberExpression" && !node.computed && !node.optional ? name(node.property) : null;
const primitive = (node) =>
  node?.type === "Literal" &&
  !node.regex &&
  node.bigint === undefined &&
  (node.value === null || ["string", "number", "boolean"].includes(typeof node.value));
// Creating an arrow does not evaluate its body or parameter defaults. Its body can run later.
const argumentValue = (node) => primitive(node) || node?.type === "ArrowFunctionExpression";

export function diagnose(source) {
  let statements;
  try {
    statements = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" }).body;
  } catch {
    return fail("parse");
  }
  if (statements.length !== 2 && statements.length !== 3) return fail("closed-module");
  const cls = statements[0];
  const helper = statements.length === 3 ? statements[1] : null;
  const declaration = statements.at(-1);
  if (cls.type !== "ClassDeclaration" || !name(cls.id) || cls.superClass) return fail("local-class");
  if (helper) {
    if (
      helper.type !== "FunctionDeclaration" ||
      !name(helper.id) ||
      helper.async ||
      helper.generator ||
      helper.params.length !== 1 ||
      !name(helper.params[0]) ||
      name(helper.params[0]) === name(cls.id) ||
      helper.body.body.length !== 1
    )
      return fail("helper-shape");
    const ret = helper.body.body[0];
    const call = ret.argument;
    if (
      ret.type !== "ReturnStatement" ||
      call?.type !== "NewExpression" ||
      name(call.callee) !== name(cls.id) ||
      call.arguments.length !== 1 ||
      name(call.arguments[0]) !== name(helper.params[0])
    )
      return fail("helper-forwarding");
  }
  if (
    declaration.type !== "VariableDeclaration" ||
    declaration.kind !== "const" ||
    declaration.declarations.length !== 1
  )
    return fail("const-construction");
  const d = declaration.declarations[0];
  const call = d.init;
  if (
    !name(d.id) ||
    call?.type !== (helper ? "CallExpression" : "NewExpression") ||
    name(call.callee) !== name(helper?.id ?? cls.id) ||
    call.optional ||
    call.arguments.length !== 1
  )
    return fail("known-constructor");
  const arg = call.arguments[0];
  if (arg?.type !== "ObjectExpression") return fail("literal-argument");
  const keys = new Set();
  for (const p of arg.properties) {
    const key = name(p.key);
    if (
      p.type !== "Property" ||
      p.computed ||
      p.kind !== "init" ||
      p.method ||
      !key ||
      key === "__proto__" ||
      !argumentValue(p.value)
    )
      return fail("ordinary-own-data");
    keys.add(key);
  }
  const fields = new Set();
  let ctor;
  for (const element of cls.body.body) {
    if (
      element.type === "PropertyDefinition" &&
      !element.computed &&
      !element.static &&
      name(element.key) &&
      name(element.key) !== "__proto__" &&
      (!element.value || primitive(element.value))
    ) {
      fields.add(name(element.key));
    } else if (element.type === "MethodDefinition" && element.kind === "constructor" && !ctor) {
      ctor = element.value;
    } else return fail("class-shape");
  }
  if (!ctor || ctor.params.length !== 1 || !name(ctor.params[0]) || ctor.async || ctor.generator)
    return fail("constructor-shape");
  const parameter = name(ctor.params[0]);
  for (const statement of ctor.body.body) {
    const e = statement.expression;
    if (
      statement.type !== "ExpressionStatement" ||
      e?.type !== "AssignmentExpression" ||
      e.operator !== "=" ||
      e.left?.object?.type !== "ThisExpression" ||
      !member(e.left) ||
      name(e.right?.object) !== parameter ||
      !member(e.right)
    )
      return fail("constructor-statement");
    if (!fields.has(member(e.left))) return fail("receiver-field-not-own");
    if (!keys.has(member(e.right))) return fail("argument-property-not-own");
  }
  return { candidate: true, reason: "closed-own-data" };
}
