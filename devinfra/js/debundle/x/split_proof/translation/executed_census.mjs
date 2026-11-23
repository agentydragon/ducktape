// Which syntax and which standard functions of some scripts ran? A count from V8 block coverage, to see how much of
// the language start-up code uses ([`PLAN.md`](../PLAN.md) § Direction). Hand-run; it has no test.
//
//   node executed_census.mjs <served.json> <coverage.json>... > census.json
//
// `served.json` maps each script's URL to its file. A coverage file is the `result` of the DevTools protocol's
// `Profiler.takePreciseCoverage` after `Profiler.startPreciseCoverage` with `callCount` and `detailed`, restricted
// to those URLs; several files are added up. Taking the coverage while the page is paused at a `debugger` statement
// appended to the entry module gives what ran while the modules were evaluated, before any callback.
//
// Per script and feature the output has the nodes that exist (`total`) and the nodes that ran (`ran`). A node ran if
// the innermost coverage range around its first character has a count above zero. A function expression or
// declaration counts with the code around it (it was created); the `… entered` features say that a body ran.
// Limits: a standard call is counted by the method's name whatever the receiver is (`.get` includes the program's
// own methods), and a global is a name the file declares nowhere, so both are estimates.
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import globals from "globals";

const [servedFile, ...coverageFiles] = process.argv.slice(2);
const served = JSON.parse(readFileSync(servedFile, "utf8"));

const ES = new Set(Object.keys(globals.builtin));
const WEB = new Set(Object.keys(globals.browser).filter((n) => !ES.has(n)));
const PROTO = new Map();
for (const C of [
  Object,
  Function,
  Array,
  String,
  Number,
  Boolean,
  Symbol,
  Promise,
  Map,
  Set,
  WeakMap,
  WeakSet,
  Date,
  RegExp,
  Error,
  ArrayBuffer,
  Uint8Array,
  DataView,
])
  for (const m of Object.getOwnPropertyNames(C.prototype))
    if (m !== "constructor" && typeof Object.getOwnPropertyDescriptor(C.prototype, m).value === "function")
      PROTO.set(m, (PROTO.get(m) ?? []).concat(C.name));
for (const m of Object.getOwnPropertyNames(Object.getPrototypeOf(Uint8Array.prototype)))
  if (m !== "constructor") PROTO.set(m, (PROTO.get(m) ?? []).concat("TypedArray"));

const FUNCTIONS = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"]);
const out = {};
for (const [url, file] of Object.entries(served)) {
  const source = readFileSync(file, "utf8");
  const counts = new Uint32Array(source.length + 16);
  const ranges = [];
  for (const cf of coverageFiles) {
    const script = JSON.parse(readFileSync(cf, "utf8")).find((s) => s.url === url);
    if (!script) continue;
    // each file is painted outermost range first, into its own layer, and the layers are added
    const layer = new Uint32Array(source.length + 16);
    const rs = script.functions
      .flatMap((f) => f.ranges)
      .sort((a, b) => b.endOffset - b.startOffset - (a.endOffset - a.startOffset));
    for (const r of rs) layer.fill(r.count, r.startOffset, Math.min(r.endOffset, layer.length));
    for (let i = 0; i < counts.length; i++) counts[i] += layer[i];
    ranges.push(rs.length);
  }
  const ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
  const declared = new Set();
  const declare = (p) => {
    if (!p) return;
    if (p.type === "Identifier") declared.add(p.name);
    else if (p.type === "ObjectPattern")
      p.properties.forEach((q) => declare(q.type === "RestElement" ? q.argument : q.value));
    else if (p.type === "ArrayPattern") p.elements.forEach(declare);
    else if (p.type === "RestElement") declare(p.argument);
    else if (p.type === "AssignmentPattern") declare(p.left);
  };
  const walkAll = (node, visit, parent = null, key = null) => {
    if (!node || typeof node.type !== "string") return;
    visit(node, parent, key);
    for (const k of Object.keys(node)) {
      const v = node[k];
      if (Array.isArray(v)) v.forEach((c) => walkAll(c, visit, node, k));
      else if (v && typeof v.type === "string") walkAll(v, visit, node, k);
    }
  };
  walkAll(ast, (n) => {
    if (n.type === "VariableDeclarator") declare(n.id);
    else if (FUNCTIONS.has(n.type)) {
      declare(n.id);
      n.params.forEach(declare);
    } else if (n.type === "ClassDeclaration" || n.type === "ClassExpression") declare(n.id);
    else if (n.type === "CatchClause") declare(n.param);
    else if (n.type.startsWith("Import") && n.local) declare(n.local);
  });

  const features = {},
    calls = {},
    hostGlobals = {},
    esGlobals = {};
  const bump = (table, name, ran) => {
    const e = (table[name] ??= { total: 0, ran: 0 });
    e.total++;
    if (ran) e.ran++;
  };
  const feature = (name, ran) => bump(features, name, ran);
  let nodes = 0,
    nodesRan = 0,
    functions = 0,
    functionsEntered = 0;
  const visit = (node, parent, key, around) => {
    if (!node || typeof node.type !== "string") return;
    const isFn = FUNCTIONS.has(node.type);
    const ran = isFn ? around : counts[node.start] > 0;
    nodes++;
    if (ran) nodesRan++;
    feature(`node:${node.type}`, ran);
    switch (node.type) {
      case "ObjectPattern":
      case "ArrayPattern":
        feature("destructuring", ran);
        break;
      case "SpreadElement":
        feature("spread", ran);
        break;
      case "RestElement":
        feature("rest", ran);
        break;
      case "AssignmentPattern":
        feature("default value", ran);
        break;
      case "YieldExpression":
        feature("yield", ran);
        break;
      case "AwaitExpression":
        feature("await", ran);
        break;
      case "Super":
        feature("super", ran);
        break;
      case "PrivateIdentifier":
        feature("private name", ran);
        break;
      case "StaticBlock":
        feature("static block", ran);
        break;
      case "LabeledStatement":
        feature("label", ran);
        break;
      case "SwitchStatement":
        feature("switch", ran);
        break;
      case "DoWhileStatement":
        feature("do-while", ran);
        break;
      case "ForInStatement":
        feature("for-in", ran);
        break;
      case "ForOfStatement":
        feature(node.await ? "for-await" : "for-of", ran);
        break;
      case "ChainExpression":
        feature("optional chain", ran);
        break;
      case "TaggedTemplateExpression":
        feature("tagged template", ran);
        break;
      case "MetaProperty":
        feature(`${node.meta.name}.${node.property.name}`, ran);
        break;
      case "ImportExpression":
        feature("dynamic import", ran);
        break;
      case "ThrowStatement":
        feature("throw", ran);
        break;
      case "TryStatement":
        feature("try", ran);
        break;
      case "NewExpression":
        feature("new", ran);
        break;
      case "ThisExpression":
        feature("this", ran);
        break;
      case "TemplateLiteral":
        feature("template literal", ran);
        break;
      case "SequenceExpression":
        feature("comma", ran);
        break;
      case "Literal":
        if (node.regex) feature("regular expression", ran);
        else if (node.bigint) feature("BigInt literal", ran);
        break;
      case "ArrayExpression":
        if (node.elements.includes(null)) feature("array hole", ran);
        break;
      case "BinaryExpression":
        if (node.operator === "**" || node.operator === "instanceof" || node.operator === "in")
          feature(`operator ${node.operator}`, ran);
        break;
      case "UnaryExpression":
        if (node.operator === "delete" || node.operator === "typeof") feature(`operator ${node.operator}`, ran);
        break;
      case "ClassDeclaration":
      case "ClassExpression":
        feature(node.superClass ? "class with extends" : "class without extends", ran);
        break;
      case "Property":
        if (node.kind !== "init") feature("object literal accessor", ran);
        else if (node.computed) feature("computed key", ran);
        break;
      case "MethodDefinition":
        if (node.kind === "get" || node.kind === "set") feature("class accessor", ran);
        break;
      case "PropertyDefinition":
        feature(node.static ? "static field" : "instance field", ran);
        break;
      case "Identifier": {
        const isRef = !(
          parent &&
          ((parent.type === "MemberExpression" && key === "property" && !parent.computed) ||
            ((parent.type === "Property" ||
              parent.type === "MethodDefinition" ||
              parent.type === "PropertyDefinition") &&
              key === "key" &&
              !parent.computed) ||
            parent.type === "LabeledStatement" ||
            parent.type === "BreakStatement" ||
            parent.type === "ContinueStatement" ||
            parent.type.endsWith("Specifier"))
        );
        if (isRef && node.name === "arguments") feature("arguments", ran);
        if (isRef && !declared.has(node.name)) {
          if (ES.has(node.name)) bump(esGlobals, node.name, ran);
          else if (WEB.has(node.name)) bump(hostGlobals, node.name, ran);
        }
        break;
      }
      case "CallExpression": {
        const c = node.callee;
        if (c.type === "MemberExpression" && !c.computed && c.property.type === "Identifier") {
          if (c.object.type === "Identifier" && ES.has(c.object.name) && !declared.has(c.object.name))
            bump(calls, `${c.object.name}.${c.property.name}`, ran);
          else if (PROTO.has(c.property.name)) bump(calls, `.${c.property.name}`, ran);
        }
        break;
      }
    }
    if (isFn) {
      const entered = counts[node.body.start] > 0;
      functions++;
      if (entered) functionsEntered++;
      if (node.generator) feature(node.async ? "async generator entered" : "generator entered", entered);
      else if (node.async) feature("async function entered", entered);
      if (
        parent &&
        (parent.type === "Property" || parent.type === "MethodDefinition") &&
        (parent.kind === "get" || parent.kind === "set")
      )
        feature("accessor entered", entered);
    }
    for (const k of Object.keys(node)) {
      const v = node[k];
      if (Array.isArray(v)) v.forEach((c) => visit(c, node, k, ran));
      else if (v && typeof v.type === "string") visit(v, node, k, ran);
    }
  };
  visit(ast, null, null, true);
  const topLevel = ast.body.length;
  out[url.split("/").pop()] = {
    characters: source.length,
    coverageRanges: ranges,
    topLevel,
    nodes,
    nodesRan,
    functions,
    functionsEntered,
    features,
    calls,
    esGlobals,
    hostGlobals,
  };
}
console.log(JSON.stringify(out));
