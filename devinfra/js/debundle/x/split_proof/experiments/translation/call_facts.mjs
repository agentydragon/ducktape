// Reports syntactic facts and outstanding obligations. This is not a purity
// checker: even an empty `uncertainties` list would not establish equivalence.
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";

function visit(node, f, skipFunctions = true) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const x of node) visit(x, f, skipFunctions);
    return;
  }
  if (typeof node.type !== "string") return;
  f(node);
  if (skipFunctions && ["FunctionExpression", "ArrowFunctionExpression", "FunctionDeclaration"].includes(node.type))
    return;
  for (const value of Object.values(node)) if (value && typeof value === "object") visit(value, f, skipFunctions);
}

const id = (n) => (n?.type === "Identifier" ? n.name : null);
const propertyKey = (node) =>
  id(node) ?? (node?.type === "Literal" && typeof node.value === "string" ? node.value : null);
const member = (n) =>
  n?.type === "MemberExpression" && !n.computed && !n.optional && n.property?.type === "Identifier"
    ? n.property.name
    : null;
const ordered = (xs) => [...new Set(xs)].sort();

// These are independent facts about each top-level direct call, not a sound
// approximation of JavaScript effects. Report unknowns instead of deciding.
export function inspectCalls(source, target) {
  const ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module", ranges: true, locations: true });
  const manager = eslintScope.analyze(ast, { ecmaVersion: 2022, sourceType: "module" });
  const moduleScope = manager.scopes.find((scope) => scope.type === "module");
  if (!moduleScope) throw new Error("module scope missing");
  const refs = new Map();
  for (const scope of manager.scopes) for (const ref of scope.references) refs.set(ref.identifier, ref);
  const helper = moduleScope.set.get(target);
  const helperDef = helper?.defs.length === 1 && helper.defs[0].type === "FunctionName" ? helper.defs[0].node : null;
  if (!helperDef) throw new Error("target must name one module-scope function declaration");
  const helperWrites = helper.references.filter((r) => r.isWrite()).length;
  const constructors = [];
  // The helper's outer body runs at initialization; nested callbacks do not.
  visit(helperDef.body, (node) => {
    if (node.type === "NewExpression") constructors.push(node);
  });
  const classes = constructors.map((newExpr) => {
    const ref = refs.get(newExpr.callee);
    const binding = ref?.resolved;
    const def = binding?.defs.length === 1 && binding.defs[0].type === "ClassName" ? binding.defs[0].node : null;
    const constructor = def?.body.body.find((x) => x.type === "MethodDefinition" && x.kind === "constructor");
    const param = id(constructor?.value?.params[0]);
    const fields = [];
    const reads = [];
    const writes = [];
    for (const x of def?.body.body ?? []) {
      if (x.type === "PropertyDefinition" && !x.static && !x.computed && id(x.key)) fields.push(id(x.key));
    }
    if (constructor) {
      visit(constructor.value.body, (node) => {
        if (node.type === "MemberExpression" && id(node.object) === param && member(node)) reads.push(member(node));
        if (node.type === "AssignmentExpression" && node.left?.object?.type === "ThisExpression" && member(node.left))
          writes.push(member(node.left));
      });
    }
    return {
      name: id(newExpr.callee),
      local_class: !!def && binding.scope === moduleScope,
      // Class declarations may occur after the helper but must precede each call.
      declaration_offset: def?.start ?? null,
      binding_writes_anywhere: binding?.references.filter((r) => r.isWrite()).length ?? null,
      own_fields: ordered(fields),
      argument_reads: ordered(reads),
      receiver_writes: ordered(writes),
      receiver_writes_without_field: ordered(writes.filter((name) => !fields.includes(name))),
    };
  });
  const calls = [];
  let precedingCallStatements = 0;
  for (const statement of ast.body) {
    const hasCall = [];
    visit(statement, (node) => {
      if (["CallExpression", "NewExpression"].includes(node.type)) hasCall.push(node);
    });
    if (statement.type === "VariableDeclaration") {
      for (const declarator of statement.declarations) {
        const call = declarator.init;
        if (call?.type !== "CallExpression" || refs.get(call.callee)?.resolved !== helper) continue;
        const options = call.arguments[0];
        const own = [];
        let ordinary = options?.type === "ObjectExpression" && call.arguments.length === 1;
        if (ordinary) {
          for (const p of options.properties) {
            const key = propertyKey(p.key);
            if (p.type !== "Property" || p.computed || p.kind !== "init" || p.method || !key || key === "__proto__")
              ordinary = false;
            else own.push(key);
          }
        }
        const argumentRisks = [];
        visit(options, (node) => {
          if (
            [
              "MemberExpression",
              "CallExpression",
              "NewExpression",
              "SpreadElement",
              "AwaitExpression",
              "AssignmentExpression",
              "UpdateExpression",
            ].includes(node.type)
          )
            argumentRisks.push(node.type);
        });
        const readNames = ordered(classes.flatMap((c) => c.argument_reads));
        const absent = readNames.filter((name) => !own.includes(name));
        // A source-order prefix is not the emitted module order. Without the
        // concrete plan we cannot say which calls precede this one after splitting.
        const uncertainties = [
          "prototype_state_unproven",
          "helper_and_constructor_effects_unproven",
          "reordered_execution_unanalyzed",
        ];
        if (precedingCallStatements) uncertainties.push("preceding_calls_unanalyzed");
        if (helperWrites) uncertainties.push("helper_binding_written");
        if (!ordinary) uncertainties.push("argument_shape_unknown");
        if (!ordinary || absent.length) uncertainties.push("argument_may_read_inherited_property");
        if (argumentRisks.length) uncertainties.push("argument_operations_unanalyzed");
        if (classes.some((c) => c.receiver_writes_without_field.length))
          uncertainties.push("receiver_write_may_use_prototype");
        if (classes.some((c) => !c.local_class || c.declaration_offset === null || c.declaration_offset >= call.start))
          uncertainties.push("constructor_initialization_unproven");
        if (classes.some((c) => c.binding_writes_anywhere)) uncertainties.push("constructor_binding_written");
        calls.push({
          binding: id(declarator.id),
          line: call.loc.start.line,
          ordinary_argument_shape: ordinary,
          own_argument_properties: ordered(own),
          missing_constructor_reads: ordinary ? absent : null,
          eager_argument_operations: ordered(argumentRisks),
          preceding_statements_with_calls: precedingCallStatements,
          uncertainties,
        });
      }
    }
    if (hasCall.length) precedingCallStatements++;
  }
  return {
    target,
    helper_binding_writes: helperWrites,
    constructors: classes,
    calls,
    limitations: [
      "direct module-scope variable initializer calls only; aliases and other sites are not analyzed",
      "helper and constructor semantics, dynamic property names, and source transformations are not checked",
      "prototype state, unknown calls, external code, and module evaluation order remain unproven",
      "the report is not a verdict and never establishes purity or equivalence",
    ],
  };
}
