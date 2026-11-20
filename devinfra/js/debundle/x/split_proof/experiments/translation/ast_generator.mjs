// Bounded scope-aware AST generation. No source fragments participate in generation.
const markTop = (name) => `\u0001${name}\u0002`;
const markLocal = (name) => `\u0003${name}\u0004`;

function random(seed) {
  let state = seed >>> 0;
  return (n) => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), state | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return Math.floor((((t ^ (t >>> 14)) >>> 0) / 4294967296) * n);
  };
}

const id = (name, binding) => ({ type: "Identifier", name, binding });
const num = (value) => ({ type: "Literal", value });
const block = (body) => ({ type: "BlockStatement", body });
const exprStmt = (expression) => ({ type: "ExpressionStatement", expression });
const call = (callee, args) => ({ type: "CallExpression", callee, arguments: args });
const declare = (binding, init, kind) => ({
  type: "VariableDeclaration",
  kind,
  declarations: [{ type: "VariableDeclarator", id: id(binding.name, binding.scope), init }],
});
const log = (value, label) =>
  exprStmt(
    call(
      { type: "MemberExpression", object: id("console", "global"), property: id("log", "property"), computed: false },
      [{ type: "Literal", value: label }, value]
    )
  );

/** Print only the AST forms this generator constructs. `marked` preserves binding
 * identity for the existing module-placement engine; plain output is runnable JS. */
export function printAst(node, marked = false) {
  const p = (child) => printAst(child, marked);
  switch (node.type) {
    case "Program":
      return node.body.map(p).join("\n") + "\n";
    case "Identifier":
      if (marked && typeof marked === "object") {
        if (node.binding === "top") return marked.top(node.name);
        if (node.binding === "local") return marked.local(node.name);
        return node.name;
      }
      return marked && node.binding === "top"
        ? markTop(node.name)
        : marked && node.binding === "local"
          ? markLocal(node.name)
          : node.name;
    case "Literal":
      return JSON.stringify(node.value);
    case "BlockStatement":
      return `{ ${node.body.map(p).join(" ")} }`;
    case "ExpressionStatement":
      return `${p(node.expression)};`;
    case "VariableDeclaration":
      return `${node.kind} ${node.declarations.map(p).join(", ")};`;
    case "VariableDeclarator":
      return `${p(node.id)} = ${p(node.init)}`;
    case "FunctionDeclaration":
      return `function ${p(node.id)}(${node.params.map(p).join(", ")}) ${p(node.body)}`;
    case "ArrowFunctionExpression":
      return `(${node.params.map(p).join(", ")}) => ${p(node.body)}`;
    case "ArrayExpression":
      return `[${node.elements.map(p).join(", ")}]`;
    case "ObjectExpression":
      return `({ ${node.properties.map(p).join(", ")} })`;
    case "Property":
      return `${p(node.key)}: ${p(node.value)}`;
    case "ReturnStatement":
      return `return ${p(node.argument)};`;
    case "BinaryExpression":
      return `(${p(node.left)} ${node.operator} ${p(node.right)})`;
    case "ConditionalExpression":
      return `(${p(node.test)} ? ${p(node.consequent)} : ${p(node.alternate)})`;
    case "CallExpression":
      return `${p(node.callee)}(${node.arguments.map(p).join(", ")})`;
    case "MemberExpression":
      return node.computed ? `${p(node.object)}[${p(node.property)}]` : `${p(node.object)}.${p(node.property)}`;
    case "UpdateExpression":
      return `(${p(node.argument)}${node.operator})`;
    case "AssignmentExpression":
      return `(${p(node.left)} ${node.operator} ${p(node.right)})`;
    case "IfStatement":
      return `if (${p(node.test)}) ${p(node.consequent)} else ${p(node.alternate)}`;
    default:
      throw new Error(`unsupported generated node ${node.type}`);
  }
}

function walk(node, visit, depth = 0) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const item of node) walk(item, visit, depth);
    return;
  }
  if (node.type) visit(node, depth);
  for (const value of Object.values(node))
    if (value && typeof value === "object") walk(value, visit, depth + (node.type ? 1 : 0));
}

/** Produce one program. Global references use only initialized bindings, locals
 * have lexical environments, and recursive calls to a function being built are excluded. */
export function generateAstBundle(seed, round) {
  const pick = random((seed * 100003 + round) >>> 0);
  let next = 0;
  const fresh = () => `v${next++}`;
  // Deferred closures may capture these reserved bindings before their declarations.
  // Eager evaluation and calls remain blocked until both are initialized.
  const future = { name: fresh(), scope: "top" };
  const alias = { name: fresh(), scope: "top" };
  const futureCallable = pick(2) === 0;
  let pendingReady = false;
  let earlyCallbacks = 0;
  const env = {
    values: [],
    functions: [],
    factories: [],
    objects: [],
    arrays: [],
    mutable: [],
    locals: [],
    callback: false,
  };
  const units = [];
  const ref = (b) => id(b.name, b.scope);
  const choose = (xs) => xs[pick(xs.length)];
  const callableNow = (scope) => scope.functions.filter((b) => pendingReady || scope.deferred || !b.needsPending);
  const dependsOnPending = (node, scope) => {
    let depends = false;
    walk(node, (n) => {
      if (n.type !== "Identifier" || n.binding !== "top") return;
      if (
        n.name === future.name ||
        n.name === alias.name ||
        scope.functions.some((b) => b.name === n.name && b.needsPending)
      )
        depends = true;
    });
    return depends;
  };
  function expression(scope, depth) {
    const available = [...scope.values, ...scope.locals];
    const callable = callableNow(scope);
    const mayCapture = scope.deferred && !pendingReady;
    const kinds = [
      "literal",
      ...(available.length ? ["reference", "reference"] : []),
      ...(mayCapture ? ["forward", "forward", "forward"] : []),
    ];
    if (depth > 0)
      kinds.push(
        "binary",
        "binary",
        "conditional",
        ...(callable.length ? ["call"] : []),
        ...(scope.objects.length ? ["memberObject"] : []),
        ...(scope.arrays.length ? ["memberArray"] : [])
      );
    switch (choose(kinds)) {
      case "reference":
        return ref(choose(available));
      case "forward": {
        const binding = choose([future, alias]);
        return futureCallable ? call(ref(binding), [num(1 + pick(8))]) : ref(binding);
      }
      case "binary":
        return {
          type: "BinaryExpression",
          operator: choose(["+", "-", "*"]),
          left: expression(scope, depth - 1),
          right: expression(scope, depth - 1),
        };
      case "conditional":
        return {
          type: "ConditionalExpression",
          test: { type: "BinaryExpression", operator: "<", left: expression(scope, depth - 1), right: num(pick(8)) },
          consequent: expression(scope, depth - 1),
          alternate: expression(scope, depth - 1),
        };
      case "call":
        return call(ref(choose(callable)), [expression(scope, depth - 1)]);
      case "memberObject":
        return {
          type: "MemberExpression",
          object: ref(choose(scope.objects)),
          property: id("value", "property"),
          computed: false,
        };
      case "memberArray":
        return { type: "MemberExpression", object: ref(choose(scope.arrays)), property: num(0), computed: true };
      default:
        return num(pick(10));
    }
  }
  function statement(scope, depth, topLevel) {
    const safeFunctions = callableNow(scope);
    const choices = ["const", "let", "log", ...(scope.mutable.length ? ["update", "assign"] : [])];
    if (topLevel)
      choices.push(
        "function",
        "function",
        "arrow",
        "factory",
        "factory",
        "object",
        "array",
        ...(scope.values.length ? ["alias"] : []),
        ...(scope.factories.length ? ["closure", "closure"] : []),
        ...(scope.functions.length ? ["functionAlias", "storeCallback", "storeCallback", "storeCallback"] : []),
        ...(safeFunctions.length ? ["callResult", "callResult"] : []),
        ...(scope.objects.length ? ["objectAlias"] : []),
        ...(scope.arrays.length ? ["arrayAlias"] : []),
        ...(scope.callback && (pendingReady || !scope.callbackNeedsPending)
          ? Array(scope.callbackFromEarly ? 12 : 3).fill("invokeCallback")
          : [])
      );
    if (depth > 0) choices.push("block", "if");
    const kind = choose(choices);
    const bindingScope = topLevel ? "top" : "local";
    if (kind === "const" || kind === "let" || kind === "alias") {
      const binding = { name: fresh(), scope: bindingScope };
      const init = kind === "alias" ? ref(choose(scope.values)) : expression(scope, 2);
      (topLevel ? scope.values : scope.locals).push(binding);
      if (kind === "let") scope.mutable.push(binding);
      return { node: declare(binding, init, kind === "let" ? "let" : "const"), defs: topLevel ? [binding.name] : [] };
    }
    if (kind === "object" || kind === "array" || kind === "objectAlias" || kind === "arrayAlias") {
      const binding = { name: fresh(), scope: "top" };
      const object = kind === "object" || kind === "objectAlias";
      const pool = object ? scope.objects : scope.arrays;
      const init = kind.endsWith("Alias")
        ? ref(choose(pool))
        : object
          ? {
              type: "ObjectExpression",
              properties: [{ type: "Property", key: id("value", "property"), value: expression(scope, 2) }],
            }
          : { type: "ArrayExpression", elements: [expression(scope, 2)] };
      pool.push(binding);
      return { node: declare(binding, init, "const"), defs: [binding.name] };
    }
    if (kind === "closure" || kind === "functionAlias") {
      const binding = { name: fresh(), scope: "top" };
      const selected = kind === "closure" ? choose(scope.factories) : null;
      const target = selected
        ? selected.callableArgument
          ? choose(scope.functions.filter((b) => b.hoisted))
          : null
        : choose(scope.functions);
      const init = selected ? call(ref(selected), [target ? ref(target) : expression(scope, 2)]) : ref(target);
      binding.needsPending = !!(selected?.needsPending || target?.needsPending);
      scope.functions.push(binding);
      return { node: declare(binding, init, "const"), defs: [binding.name] };
    }
    if (kind === "callResult") {
      const binding = { name: fresh(), scope: "top" };
      const init = call(ref(choose(safeFunctions)), [num(1 + pick(8))]);
      scope.values.push(binding);
      return { node: declare(binding, init, "const"), defs: [binding.name] };
    }
    if (kind === "storeCallback") {
      const target = choose(scope.functions);
      scope.callback = true;
      scope.callbackNeedsPending = !!target.needsPending;
      scope.callbackFromEarly = !pendingReady && !!target.needsPending;
      if (scope.callbackFromEarly) earlyCallbacks++;
      return {
        node: exprStmt({
          type: "AssignmentExpression",
          operator: "=",
          left: {
            type: "MemberExpression",
            object: id("globalThis", "global"),
            property: id("saved", "property"),
            computed: false,
          },
          right: ref(target),
        }),
        defs: [],
      };
    }
    if (kind === "invokeCallback")
      return {
        node: log(
          call(
            {
              type: "MemberExpression",
              object: id("globalThis", "global"),
              property: id("saved", "property"),
              computed: false,
            },
            [expression(scope, 1)]
          ),
          "callback"
        ),
        defs: [],
        deferredInvocation: !!scope.callbackFromEarly,
      };
    if (kind === "factory") {
      const binding = { name: fresh(), scope: "top" };
      const param = { name: fresh(), scope: "local" };
      const inner = { ...scope, mutable: [], locals: [param], deferred: true };
      const callableArgument = scope.functions.some((b) => b.hoisted) && pick(3) !== 0;
      binding.callableArgument = callableArgument;
      const returned = {
        type: "ArrowFunctionExpression",
        params: [],
        body: callableArgument ? call(ref(param), [expression({ ...scope, deferred: true }, 1)]) : expression(inner, 3),
      };
      binding.needsPending = dependsOnPending(returned, scope);
      scope.factories.push(binding);
      return {
        node: {
          type: "FunctionDeclaration",
          id: ref(binding),
          params: [ref(param)],
          body: block([{ type: "ReturnStatement", argument: returned }]),
        },
        defs: [binding.name],
      };
    }
    if (kind === "function" || kind === "arrow") {
      const binding = { name: fresh(), scope: "top" };
      const shadow = scope.values.length && pick(3) === 0 ? choose(scope.values).name : fresh();
      const param = { name: shadow, scope: "local" };
      // A local shadow must hide the top-level binding of the same spelling.
      const inner = {
        values: scope.values.filter((b) => b.name !== shadow),
        functions: scope.functions,
        factories: scope.factories,
        objects: scope.objects,
        arrays: scope.arrays,
        mutable: [],
        locals: [param],
        deferred: true,
      };
      const body = kind === "function" && pick(3) === 0 ? ref(param) : expression(inner, 3);
      binding.hoisted = kind === "function";
      binding.identity =
        kind === "function" && body.type === "Identifier" && body.binding === "local" && body.name === param.name;
      binding.needsPending = dependsOnPending(body, scope);
      scope.functions.push(binding);
      if (kind === "function")
        return {
          node: {
            type: "FunctionDeclaration",
            id: ref(binding),
            params: [ref(param)],
            body: block([{ type: "ReturnStatement", argument: body }]),
          },
          defs: [binding.name],
        };
      return {
        node: declare(binding, { type: "ArrowFunctionExpression", params: [ref(param)], body }, "const"),
        defs: [binding.name],
      };
    }
    if (kind === "update")
      return {
        node: exprStmt({
          type: "UpdateExpression",
          operator: "++",
          prefix: false,
          argument: ref(choose(scope.mutable)),
        }),
        defs: [],
      };
    if (kind === "assign")
      return {
        node: exprStmt({
          type: "AssignmentExpression",
          operator: "=",
          left: ref(choose(scope.mutable)),
          right: expression(scope, 2),
        }),
        defs: [],
      };
    if (kind === "block" || kind === "if") {
      const inner = {
        values: [...scope.values],
        functions: [...scope.functions],
        factories: [...scope.factories],
        objects: [...scope.objects],
        arrays: [...scope.arrays],
        mutable: [...scope.mutable],
        locals: [...scope.locals],
      };
      const nested = block(Array.from({ length: 1 + pick(2) }, () => statement(inner, depth - 1, false).node));
      if (kind === "block") return { node: nested, defs: [] };
      return {
        node: {
          type: "IfStatement",
          test: { type: "BinaryExpression", operator: "<", left: expression(scope, 1), right: num(pick(8)) },
          consequent: nested,
          alternate: block([log(expression(scope, 1), "else")]),
        },
        defs: [],
      };
    }
    return { node: log(expression(scope, 3), "effect"), defs: [] };
  }
  const count = 12 + pick(10);
  const insertAt = Math.floor((count * 2) / 3);
  for (let i = 0; i < count; i++) {
    if (i === insertAt) {
      const identities = env.functions.filter((b) => b.identity && !b.needsPending);
      const factories = env.factories.filter((b) => b.callableArgument && !b.needsPending);
      let init;
      if (futureCallable) {
        const targets = env.functions.filter((b) => b.hoisted && !b.needsPending);
        const factory = factories.length && targets.length ? choose(factories) : null;
        const target = factory ? choose(targets) : null;
        init = factory
          ? call(ref(factory), [ref(target)])
          : { type: "ArrowFunctionExpression", params: [], body: num(pick(10)) };
        future.needsPending = !!(factory?.needsPending || target?.needsPending);
        env.functions.push(future);
      } else {
        init = identities.length ? call(ref(choose(identities)), [num(1 + pick(8))]) : expression(env, 2);
        env.values.push(future);
      }
      units.push({ ast: declare(future, init, "const"), defs: [future.name] });
    } else if (i === insertAt + 1) {
      alias.needsPending = !!future.needsPending;
      units.push({ ast: declare(alias, ref(future), "const"), defs: [alias.name] });
      (futureCallable ? env.functions : env.values).push(alias);
      pendingReady = true;
    } else {
      const { node, defs, deferredInvocation = false } = statement(env, 2, true);
      units.push({ ast: node, defs, deferredInvocation });
    }
  }
  units.push({ ast: log(expression(env, 3), "final"), defs: [] });
  for (const u of units) {
    const uses = new Set();
    const locals = new Set();
    walk(u.ast, (node) => {
      if (node.type === "Identifier" && node.binding === "top" && !u.defs.includes(node.name)) uses.add(node.name);
      if (node.type === "Identifier" && node.binding === "local") locals.add(node.name);
    });
    u.uses = [...uses];
    u.locals = [...locals];
    u.tmpl = `ast:${u.ast.type}`;
    u.src = printAst(u.ast, true);
  }
  // Bias one plan toward a saved callback that can read the later binding.
  // Skip it if an intervening statement directly reads that binding: such a
  // plan would test a trivial eager dependency rather than the callback path.
  const initIndex = units.findIndex((u) => u.defs.includes(future.name));
  const invokeIndex = units.findIndex((u, i) => i > initIndex + 1 && u.deferredInvocation);
  const guidedPlace =
    invokeIndex >= 0 &&
    !units.slice(initIndex + 2, invokeIndex).some((u) => u.uses.includes(future.name) || u.uses.includes(alias.name))
      ? units.map((_, i) => (i < initIndex || (i > initIndex + 1 && i <= invokeIndex) ? 1 : 0))
      : null;
  const ast = { type: "Program", sourceType: "module", body: units.map((u) => u.ast) };
  return {
    ast,
    units,
    exports: [],
    source: printAst(ast),
    pending: [future.name, alias.name],
    earlyCallbacks,
    guidedPlace,
  };
}

export function astStatistics(bundle) {
  const types = {};
  let maxDepth = 0;
  const features = {
    returnedClosures: 0,
    callbackStores: 0,
    callbackCalls: 0,
    aliases: 0,
    identityDeclarations: 0,
    directLiteralCalls: 0,
    directIdentifierCalls: 0,
    forwardReferences: 0,
    earlyCallbacks: bundle.earlyCallbacks,
  };
  const saved = (e) =>
    e?.type === "MemberExpression" && e.object?.name === "globalThis" && e.property?.name === "saved";
  walk(bundle.ast, (node, depth) => {
    types[node.type] = (types[node.type] ?? 0) + 1;
    maxDepth = Math.max(maxDepth, depth);
    if (node.type === "ReturnStatement" && node.argument?.type === "ArrowFunctionExpression")
      features.returnedClosures++;
    if (node.type === "AssignmentExpression" && saved(node.left)) features.callbackStores++;
    if (node.type === "CallExpression" && saved(node.callee)) features.callbackCalls++;
    if (node.type === "VariableDeclarator" && node.init?.type === "Identifier") features.aliases++;
    if (
      node.type === "FunctionDeclaration" &&
      node.body?.body?.length === 1 &&
      node.body.body[0].argument?.type === "Identifier" &&
      node.body.body[0].argument.name === node.params[0]?.name
    )
      features.identityDeclarations++;
    if (
      node.type === "VariableDeclarator" &&
      node.init?.type === "CallExpression" &&
      node.init.callee?.type === "Identifier" &&
      node.init.arguments?.length === 1
    ) {
      if (node.init.arguments[0].type === "Literal") features.directLiteralCalls++;
      if (node.init.arguments[0].type === "Identifier") features.directIdentifierCalls++;
    }
  });
  const declared = new Set();
  for (const unit of bundle.units) {
    walk(unit.ast, (node) => {
      if (
        node.type === "Identifier" &&
        node.binding === "top" &&
        bundle.pending.includes(node.name) &&
        !unit.defs.includes(node.name) &&
        !declared.has(node.name)
      )
        features.forwardReferences++;
    });
    for (const name of unit.defs) declared.add(name);
  }
  return { types, maxDepth, features };
}
