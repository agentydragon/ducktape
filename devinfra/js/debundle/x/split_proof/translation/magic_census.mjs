// Where do JavaScript files use the features that make a program hard to reason about? Code from strings, interception,
// reflection, hooks the language calls on its own, patched standard objects, and what observes how code is spelled.
// A count by syntax, to decide what a debundler supports ([`SUPPORT.md`](../SUPPORT.md)).
//
//   node magic_census.mjs [--list=TEXT] @LIST > census.md   (LIST: one path of a source file per line; or the files)
//
// `--list=TEXT` also prints, to standard error, each top-level site of the features whose name contains TEXT, as
// `file:line: source`.
//
// A site is counted twice: in all, and "at the top level", outside every function body, where it runs when the module
// is evaluated unless a condition skips it. A site inside a function may run then too (a function called at start-up),
// so the second number is a lower bound on what start-up runs, not the answer.
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import globals from "globals";

const FUNCTIONS = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"]);
const GLOBAL_OBJECTS = new Set(["globalThis", "window", "self", "global"]);
const HOOK_SYMBOLS = new Set([
  "toPrimitive",
  "species",
  "hasInstance",
  "unscopables",
  "isConcatSpreadable",
  "iterator",
  "asyncIterator",
  "dispose",
  "asyncDispose",
]);
const HOOK_METHODS = new Set(["valueOf", "toString", "toJSON"]);
const STANDARD = new Set(Object.keys(globals.builtin));

/** [feature, group] in the order of the report. */
const FEATURES = [
  ["direct `eval(…)`", "code from strings"],
  ["indirect eval: `(0, eval)(…)`, `globalThis.eval(…)`", "code from strings"],
  ["`eval` mentioned without a call", "code from strings"],
  ["`Function(…)` or `new Function(…)`", "code from strings"],
  ["`import(…)`", "code loaded later"],
  ["`import.meta`", "code loaded later"],
  ["`new Proxy(…)` or `Proxy.revocable(…)`", "interception"],
  ["getter or setter in an object literal or a class", "interception"],
  ["`Object.defineProperty` or `defineProperties` with an accessor in a literal descriptor", "interception"],
  ["`Object.defineProperty` or `defineProperties`, other", "interception"],
  ["`__defineGetter__` or `__defineSetter__`", "interception"],
  ["`Object.setPrototypeOf` or `Reflect.setPrototypeOf`", "prototypes"],
  ["`__proto__` assigned, or as a key of an object literal", "prototypes"],
  ["assignment to a standard object or its prototype (`Array.prototype.x = …`)", "prototypes"],
  ["`Reflect.…(…)`", "reflection"],
  ["`Symbol.toPrimitive`, `species`, `hasInstance`, `unscopables` or `isConcatSpreadable`", "hooks the language calls"],
  ["`Symbol.iterator` or `Symbol.asyncIterator`", "hooks the language calls"],
  ["a method named `valueOf`, `toString` or `toJSON` in an object literal or a class", "hooks the language calls"],
  ["`using` or `await using` declaration", "hooks the language calls"],
  ["`Symbol.dispose` or `Symbol.asyncDispose`", "hooks the language calls"],
  ["top-level `await`", "evaluation order"],
  ["`throw` statement", "evaluation order"],
  ["`throw` outside every function and every `try` block that has a `catch`", "evaluation order"],
  ["`WeakRef` or `FinalizationRegistry`", "nondeterminism"],
  ["`Atomics` or `SharedArrayBuffer`", "nondeterminism"],
  ["`arguments.callee` or `.caller`", "how code is spelled"],
  ["`Error.captureStackTrace` or `Error.prepareStackTrace`", "how code is spelled"],
  ["`.stack` read", "how code is spelled"],
  ["`Function.prototype.toString`", "how code is spelled"],
  ["`with` statement", "scope"],
  ["`debugger` statement", "scope"],
];

const counts = new Map(
  FEATURES.map(([feature]) => [feature, { sites: 0, files: new Set(), topSites: 0, topFiles: new Set() }])
);
const listed = process.argv.find((arg) => arg.startsWith("--list="))?.slice("--list=".length);
const files = process.argv
  .slice(2)
  .filter((arg) => !arg.startsWith("--list="))
  .flatMap((arg) => (arg.startsWith("@") ? readFileSync(arg.slice(1), "utf8").split("\n").filter(Boolean) : [arg]));

const staticKey = (member) =>
  !member.computed && member.property.type === "Identifier"
    ? member.property.name
    : member.computed && member.property.type === "Literal" && typeof member.property.value === "string"
      ? member.property.value
      : undefined;
const keyName = (property) =>
  property.computed
    ? undefined
    : property.key.type === "Identifier"
      ? property.key.name
      : typeof property.key.value === "string"
        ? property.key.value
        : undefined;

for (const file of files) {
  const source = readFileSync(file, "utf8");
  let ast,
    sourceType = "module";
  try {
    ast = acorn.parse(source, { ecmaVersion: "latest", sourceType, allowHashBang: true, ranges: true });
  } catch {
    sourceType = "script";
    ast = acorn.parse(source, {
      ecmaVersion: "latest",
      sourceType,
      allowHashBang: true,
      allowReturnOutsideFunction: true,
      ranges: true,
    });
  }
  const scopes = eslintScope.analyze(ast, { ecmaVersion: 2024, sourceType, ignoreEval: true });
  const free = new Set(scopes.globalScope.through.map((reference) => reference.identifier));
  const isGlobal = (node, name) =>
    node?.type === "Identifier" && free.has(node) && (name === undefined || node.name === name);
  /** `X`, `globalThis.X`: the global `X`, when `node` names it. */
  const globalName = (node) => {
    if (isGlobal(node)) return GLOBAL_OBJECTS.has(node.name) ? undefined : node.name;
    if (node?.type === "MemberExpression" && isGlobal(node.object) && GLOBAL_OBJECTS.has(node.object.name))
      return staticKey(node);
    return undefined;
  };
  /** `X.a` -> ["X", "a"], for a global `X`. */
  const globalMember = (node) =>
    node?.type === "MemberExpression" && globalName(node.object) && staticKey(node)
      ? [globalName(node.object), staticKey(node)]
      : undefined;

  // `guarded`: inside the block of a `try` that has a `catch`, with no function in between
  const stack = [[ast, false, undefined, false]];
  while (stack.length > 0) {
    const [node, inFunction, parent, guarded] = stack.pop();
    const hit = (feature) => {
      const entry = counts.get(feature);
      entry.sites += 1;
      entry.files.add(file);
      if (!inFunction) {
        entry.topSites += 1;
        entry.topFiles.add(file);
        if (listed !== undefined && feature.includes(listed)) {
          const line = source.slice(0, node.start).split("\n").length;
          console.error(`${file}:${line}: ${source.slice(node.start, node.end).replace(/\s+/g, " ").slice(0, 160)}`);
        }
      }
    };
    switch (node.type) {
      case "CallExpression":
      case "NewExpression": {
        let callee = node.callee;
        if (node.type === "CallExpression" && isGlobal(callee, "eval")) hit("direct `eval(…)`");
        else {
          if (callee.type === "SequenceExpression") callee = callee.expressions.at(-1);
          const name = globalName(callee);
          const member = globalMember(callee);
          if (name === "eval" && node.type === "CallExpression")
            hit("indirect eval: `(0, eval)(…)`, `globalThis.eval(…)`");
          if (name === "Function") hit("`Function(…)` or `new Function(…)`");
          if ((name === "Proxy" && node.type === "NewExpression") || member?.join(".") === "Proxy.revocable")
            hit("`new Proxy(…)` or `Proxy.revocable(…)`");
          if (member?.[0] === "Reflect") hit("`Reflect.…(…)`");
          if (member && ["Object.setPrototypeOf", "Reflect.setPrototypeOf"].includes(member.join(".")))
            hit("`Object.setPrototypeOf` or `Reflect.setPrototypeOf`");
          if (member?.[0] === "Object" && ["defineProperty", "defineProperties"].includes(member[1])) {
            const descriptor = node.arguments.at(-1);
            const accessor = (literal) =>
              literal?.type === "ObjectExpression" &&
              literal.properties.some((p) => p.type === "Property" && ["get", "set"].includes(keyName(p)));
            const literalAccessor =
              member[1] === "defineProperty"
                ? accessor(descriptor)
                : descriptor?.type === "ObjectExpression" &&
                  descriptor.properties.some((p) => p.type === "Property" && accessor(p.value));
            hit(
              literalAccessor
                ? "`Object.defineProperty` or `defineProperties` with an accessor in a literal descriptor"
                : "`Object.defineProperty` or `defineProperties`, other"
            );
          }
          if ((name === "WeakRef" || name === "FinalizationRegistry") && node.type === "NewExpression")
            hit("`WeakRef` or `FinalizationRegistry`");
          if (
            callee.type === "MemberExpression" &&
            ["__defineGetter__", "__defineSetter__"].includes(staticKey(callee))
          )
            hit("`__defineGetter__` or `__defineSetter__`");
        }
        break;
      }
      case "ImportExpression":
        hit("`import(…)`");
        break;
      case "MetaProperty":
        if (node.meta.name === "import") hit("`import.meta`");
        break;
      case "Identifier":
        if (
          isGlobal(node, "eval") &&
          !(parent?.type === "CallExpression" && parent.callee === node) &&
          parent?.type !== "SequenceExpression"
        )
          hit("`eval` mentioned without a call");
        if (
          (isGlobal(node, "Atomics") || isGlobal(node, "SharedArrayBuffer")) &&
          !(parent?.type === "UnaryExpression" && parent.operator === "typeof")
        )
          hit("`Atomics` or `SharedArrayBuffer`");
        break;
      case "MemberExpression": {
        const key = staticKey(node);
        const member = globalMember(node);
        if (member?.[0] === "Symbol" && HOOK_SYMBOLS.has(member[1])) {
          hit(
            ["iterator", "asyncIterator"].includes(member[1])
              ? "`Symbol.iterator` or `Symbol.asyncIterator`"
              : ["dispose", "asyncDispose"].includes(member[1])
                ? "`Symbol.dispose` or `Symbol.asyncDispose`"
                : "`Symbol.toPrimitive`, `species`, `hasInstance`, `unscopables` or `isConcatSpreadable`"
          );
        }
        if (member?.[0] === "Error" && ["captureStackTrace", "prepareStackTrace"].includes(member[1]))
          hit("`Error.captureStackTrace` or `Error.prepareStackTrace`");
        if (key === "stack" && !(parent?.type === "AssignmentExpression" && parent.left === node)) hit("`.stack` read");
        if (key === "toString" && globalMember(node.object)?.join(".") === "Function.prototype")
          hit("`Function.prototype.toString`");
        if (["callee", "caller"].includes(key) && node.object.type === "Identifier" && node.object.name === "arguments")
          hit("`arguments.callee` or `.caller`");
        if (key === "__proto__" && parent?.type === "AssignmentExpression" && parent.left === node)
          hit("`__proto__` assigned, or as a key of an object literal");
        if (parent?.type === "AssignmentExpression" && parent.left === node) {
          // `Std.x = …`, `Std.prototype.x = …`
          const root =
            globalName(node.object) ??
            (globalMember(node.object)?.[1] === "prototype" ? globalMember(node.object)[0] : undefined);
          if (root !== undefined && STANDARD.has(root))
            hit("assignment to a standard object or its prototype (`Array.prototype.x = …`)");
        }
        break;
      }
      case "Property":
      case "MethodDefinition": {
        if (node.kind === "get" || node.kind === "set") hit("getter or setter in an object literal or a class");
        const name = keyName(node);
        if (
          node.type === "Property" &&
          name === "__proto__" &&
          parent?.type === "ObjectExpression" &&
          !node.shorthand &&
          !node.method
        )
          hit("`__proto__` assigned, or as a key of an object literal");
        if (HOOK_METHODS.has(name) && node.value && FUNCTIONS.has(node.value.type) && parent?.type !== "ObjectPattern")
          hit("a method named `valueOf`, `toString` or `toJSON` in an object literal or a class");
        break;
      }
      case "VariableDeclaration":
        if (node.kind === "using" || node.kind === "await using") hit("`using` or `await using` declaration");
        if (node.kind === "await using" && !inFunction) hit("top-level `await`");
        break;
      case "AwaitExpression":
        if (!inFunction) hit("top-level `await`");
        break;
      case "ForOfStatement":
        if (node.await && !inFunction) hit("top-level `await`");
        break;
      case "ThrowStatement":
        hit("`throw` statement");
        if (!inFunction && !guarded) hit("`throw` outside every function and every `try` block that has a `catch`");
        break;
      case "WithStatement":
        hit("`with` statement");
        break;
      case "DebuggerStatement":
        hit("`debugger` statement");
        break;
      default:
    }
    const inside = inFunction || FUNCTIONS.has(node.type);
    for (const key of Object.keys(node)) {
      const value = node[key];
      const childGuarded = !inside && (guarded || (node.type === "TryStatement" && key === "block" && node.handler));
      if (Array.isArray(value)) {
        for (const child of value)
          if (child && typeof child.type === "string") stack.push([child, inside, node, childGuarded]);
      } else if (value && typeof value.type === "string") stack.push([value, inside, node, childGuarded]);
    }
  }
}

const lines = [
  `${files.length} files. A site at the top level is outside every function body.`,
  "",
  "| Feature | Sites | Files | Sites at the top level | Files |",
  "| --- | ---: | ---: | ---: | ---: |",
];
let group;
for (const [feature, featureGroup] of FEATURES) {
  const entry = counts.get(feature);
  if (featureGroup !== group) lines.push(`| **${featureGroup}** | | | | |`);
  group = featureGroup;
  lines.push(`| ${feature} | ${entry.sites} | ${entry.files.size} | ${entry.topSites} | ${entry.topFiles.size} |`);
}
console.log(lines.join("\n"));
