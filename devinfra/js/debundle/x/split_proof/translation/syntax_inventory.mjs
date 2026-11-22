// The syntax that a generator of JavaScript programs should reach: ESTree node types of ECMAScript 2024 modules (as acorn
// reports them), and the forms inside those types that differ in meaning (`async`, `static`, `computed`, an operator).
// Each form has a witness snippet; `coverage_audit_test.mjs` checks that the snippet parses and that the form's predicate
// fires on it, so a typo in an entry shows. The coverage audit and the coverage statistics of the generators count these.
import * as acorn from "acorn";

/** [node type, first edition that has it]. `WithStatement` is not valid in a module (strict mode). */
export const NODE_TYPES = [
  ["Program", "ES5"],
  ["ExpressionStatement", "ES5"],
  ["BlockStatement", "ES5"],
  ["EmptyStatement", "ES5"],
  ["DebuggerStatement", "ES5"],
  ["WithStatement", "ES5 (sloppy mode only)"],
  ["ReturnStatement", "ES5"],
  ["LabeledStatement", "ES5"],
  ["BreakStatement", "ES5"],
  ["ContinueStatement", "ES5"],
  ["IfStatement", "ES5"],
  ["SwitchStatement", "ES5"],
  ["SwitchCase", "ES5"],
  ["ThrowStatement", "ES5"],
  ["TryStatement", "ES5"],
  ["CatchClause", "ES5"],
  ["WhileStatement", "ES5"],
  ["DoWhileStatement", "ES5"],
  ["ForStatement", "ES5"],
  ["ForInStatement", "ES5"],
  ["FunctionDeclaration", "ES5"],
  ["VariableDeclaration", "ES5"],
  ["VariableDeclarator", "ES5"],
  ["ThisExpression", "ES5"],
  ["ArrayExpression", "ES5"],
  ["ObjectExpression", "ES5"],
  ["Property", "ES5"],
  ["FunctionExpression", "ES5"],
  ["UnaryExpression", "ES5"],
  ["UpdateExpression", "ES5"],
  ["BinaryExpression", "ES5"],
  ["AssignmentExpression", "ES5"],
  ["LogicalExpression", "ES5"],
  ["MemberExpression", "ES5"],
  ["ConditionalExpression", "ES5"],
  ["CallExpression", "ES5"],
  ["NewExpression", "ES5"],
  ["SequenceExpression", "ES5"],
  ["Identifier", "ES5"],
  ["Literal", "ES5"],
  ["ForOfStatement", "ES2015"],
  ["Super", "ES2015"],
  ["SpreadElement", "ES2015"],
  ["ArrowFunctionExpression", "ES2015"],
  ["YieldExpression", "ES2015"],
  ["TemplateLiteral", "ES2015"],
  ["TaggedTemplateExpression", "ES2015"],
  ["TemplateElement", "ES2015"],
  ["ObjectPattern", "ES2015"],
  ["ArrayPattern", "ES2015"],
  ["RestElement", "ES2015"],
  ["AssignmentPattern", "ES2015"],
  ["ClassBody", "ES2015"],
  ["MethodDefinition", "ES2015"],
  ["ClassDeclaration", "ES2015"],
  ["ClassExpression", "ES2015"],
  ["MetaProperty", "ES2015"],
  ["ImportDeclaration", "ES2015"],
  ["ImportSpecifier", "ES2015"],
  ["ImportDefaultSpecifier", "ES2015"],
  ["ImportNamespaceSpecifier", "ES2015"],
  ["ExportNamedDeclaration", "ES2015"],
  ["ExportSpecifier", "ES2015"],
  ["ExportDefaultDeclaration", "ES2015"],
  ["ExportAllDeclaration", "ES2015"],
  ["AwaitExpression", "ES2017"],
  ["ChainExpression", "ES2020"],
  ["ImportExpression", "ES2020"],
  ["PropertyDefinition", "ES2022"],
  ["PrivateIdentifier", "ES2022"],
  ["StaticBlock", "ES2022"],
  ["ImportAttribute", "ES2025"],
];

/** A syntax form: a name, the edition, a snippet that has it, and a predicate on a node (and its parent) that finds it. */
const forms = [];
const form = (name, since, snippet, test) => forms.push({ name, since, snippet, test });
const is =
  (type, extra = () => true) =>
  (n) =>
    n.type === type && extra(n);

// ---- declarations
form(
  "var",
  "ES5",
  "var a = 1;",
  is("VariableDeclaration", (n) => n.kind === "var")
);
form(
  "let",
  "ES2015",
  "let a = 1;",
  is("VariableDeclaration", (n) => n.kind === "let")
);
form(
  "const",
  "ES2015",
  "const a = 1;",
  is("VariableDeclaration", (n) => n.kind === "const")
);
form(
  "using",
  "ES2026",
  "using a = null;",
  is("VariableDeclaration", (n) => n.kind.endsWith("using"))
);
form(
  "declaration:no-init",
  "ES5",
  "let a;",
  is("VariableDeclarator", (n) => n.init === null)
);
form(
  "declaration:several",
  "ES5",
  "let a = 1, b = 2;",
  is("VariableDeclaration", (n) => n.declarations.length > 1)
);
// ---- functions
form("function:declaration", "ES5", "function f() {}", is("FunctionDeclaration"));
form(
  "function:expression",
  "ES5",
  "(function () {});",
  is("FunctionExpression", (n) => !n.id)
);
form(
  "function:named-expression",
  "ES5",
  "(function f() {});",
  is("FunctionExpression", (n) => !!n.id)
);
form(
  "function:arrow",
  "ES2015",
  "() => {};",
  is("ArrowFunctionExpression", (n) => !n.expression)
);
form(
  "function:arrow-expression",
  "ES2015",
  "() => 1;",
  is("ArrowFunctionExpression", (n) => n.expression)
);
const fn = (n) => /Function/.test(n.type);
form("function:async", "ES2017", "async function f() {}", (n) => fn(n) && n.async && !n.generator);
form("function:generator", "ES2015", "function* f() {}", (n) => fn(n) && n.generator && !n.async);
form("function:async-generator", "ES2018", "async function* f() {}", (n) => fn(n) && n.generator && n.async);
form(
  "function:async-arrow",
  "ES2017",
  "async () => 1;",
  is("ArrowFunctionExpression", (n) => n.async)
);
form("param:default", "ES2015", "function f(a = 1) {}", is("AssignmentPattern"));
form("param:rest", "ES2015", "function f(...a) {}", (n, p) => n.type === "RestElement" && p && fn(p));
form("param:destructured", "ES2015", "function f({ a }, [b]) {}", (n, p) => /Pattern/.test(n.type) && p && fn(p));
form(
  "arguments",
  "ES5",
  "function f() { return arguments; }",
  is("Identifier", (n) => n.name === "arguments")
);
form(
  "new.target",
  "ES2015",
  "function f() { return new.target; }",
  is("MetaProperty", (n) => n.meta.name === "new")
);
form(
  "import.meta",
  "ES2020",
  "import.meta;",
  is("MetaProperty", (n) => n.meta.name === "import")
);
form(
  "directive",
  "ES5",
  '"use strict";',
  is("ExpressionStatement", (n) => typeof n.directive === "string")
);
// ---- statements
form(
  "if",
  "ES5",
  "if (a) b;",
  is("IfStatement", (n) => n.alternate === null)
);
form(
  "if:else",
  "ES5",
  "if (a) b; else c;",
  is("IfStatement", (n) => n.alternate !== null)
);
form(
  "if:else-if",
  "ES5",
  "if (a) b; else if (c) d;",
  is("IfStatement", (n) => n.alternate?.type === "IfStatement")
);
form(
  "for",
  "ES5",
  "for (var i = 0; i < 2; i++) {}",
  is("ForStatement", (n) => n.init && n.test && n.update)
);
form(
  "for:let",
  "ES2015",
  "for (let i = 0; i < 2; i++) {}",
  is("ForStatement", (n) => n.init?.kind === "let")
);
form(
  "for:empty-parts",
  "ES5",
  "for (;;) break;",
  is("ForStatement", (n) => !n.init && !n.test && !n.update)
);
form(
  "for:expression-init",
  "ES5",
  "for (i = 0; i < 2; i++) {}",
  is("ForStatement", (n) => n.init?.type === "AssignmentExpression")
);
form("for-in", "ES5", "for (const k in o) {}", is("ForInStatement"));
form(
  "for-in:var",
  "ES5",
  "for (var k in o) {}",
  is("ForInStatement", (n) => n.left.kind === "var")
);
form(
  "for-in:target",
  "ES5",
  "for (o.k in o) {}",
  is("ForInStatement", (n) => n.left.type === "MemberExpression")
);
form(
  "for-of",
  "ES2015",
  "for (const x of o) {}",
  is("ForOfStatement", (n) => !n.await)
);
form(
  "for-of:destructured",
  "ES2015",
  "for (const [a, b] of o) {}",
  is("ForOfStatement", (n) => /Pattern/.test(n.left.declarations?.[0]?.id.type ?? ""))
);
form(
  "for-of:target",
  "ES2015",
  "for (o.k of a) {}",
  is("ForOfStatement", (n) => n.left.type === "MemberExpression")
);
form(
  "for-await",
  "ES2018",
  "async function f() { for await (const x of o) {} }",
  is("ForOfStatement", (n) => n.await)
);
form("while", "ES5", "while (a) {}", is("WhileStatement"));
form("do-while", "ES5", "do {} while (a);", is("DoWhileStatement"));
form("switch", "ES5", "switch (a) { case 1: b; }", is("SwitchStatement"));
form(
  "switch:default",
  "ES5",
  "switch (a) { default: b; }",
  is("SwitchCase", (n) => n.test === null)
);
form(
  "switch:fallthrough",
  "ES5",
  "switch (a) { case 1: case 2: b; }",
  is("SwitchCase", (n) => n.test && n.consequent.length === 0)
);
form(
  "switch:lexical",
  "ES2015",
  "switch (a) { case 1: let b = 1; }",
  is("SwitchCase", (n) => n.consequent.some((s) => s.type === "VariableDeclaration" && s.kind !== "var"))
);
form(
  "try:catch",
  "ES5",
  "try {} catch (e) {}",
  is("TryStatement", (n) => n.handler && !n.finalizer)
);
form(
  "try:finally",
  "ES5",
  "try {} finally {}",
  is("TryStatement", (n) => !n.handler && n.finalizer)
);
form(
  "try:catch-finally",
  "ES5",
  "try {} catch (e) {} finally {}",
  is("TryStatement", (n) => n.handler && n.finalizer)
);
form(
  "catch:no-binding",
  "ES2019",
  "try {} catch {}",
  is("CatchClause", (n) => n.param === null)
);
form(
  "catch:pattern",
  "ES2015",
  "try {} catch ({ message }) {}",
  is("CatchClause", (n) => /Pattern/.test(n.param?.type ?? ""))
);
form("label", "ES5", "a: for (;;) break a;", is("LabeledStatement"));
form(
  "label:block",
  "ES5",
  "a: { break a; }",
  is("LabeledStatement", (n) => n.body.type === "BlockStatement")
);
form(
  "break:label",
  "ES5",
  "a: for (;;) break a;",
  is("BreakStatement", (n) => n.label !== null)
);
form(
  "continue:label",
  "ES5",
  "a: for (;;) continue a;",
  is("ContinueStatement", (n) => n.label !== null)
);
form(
  "break",
  "ES5",
  "for (;;) break;",
  is("BreakStatement", (n) => n.label === null)
);
form(
  "continue",
  "ES5",
  "for (;;) continue;",
  is("ContinueStatement", (n) => n.label === null)
);
form("throw", "ES5", "throw a;", is("ThrowStatement"));
form(
  "return:value",
  "ES5",
  "function f() { return 1; }",
  is("ReturnStatement", (n) => n.argument !== null)
);
form(
  "return:empty",
  "ES5",
  "function f() { return; }",
  is("ReturnStatement", (n) => n.argument === null)
);
form("empty-statement", "ES5", ";", is("EmptyStatement"));
form("debugger", "ES5", "debugger;", is("DebuggerStatement"));
form("block", "ES5", "{ a; }", (n, p) => n.type === "BlockStatement" && p?.type === "Program");
form(
  "block:lexical",
  "ES2015",
  "{ let a = 1; }",
  is("BlockStatement", (n) => n.body.some((s) => s.type === "VariableDeclaration" && s.kind !== "var"))
);
form(
  "block:function",
  "ES2015",
  "{ function f() {} }",
  is("BlockStatement", (n) => n.body.some((s) => s.type === "FunctionDeclaration"))
);
form(
  "var:in-block",
  "ES5",
  "{ var a = 1; }",
  (n, p) => n.type === "VariableDeclaration" && n.kind === "var" && p?.type === "BlockStatement"
);
// ---- operators
for (const op of [
  "+",
  "-",
  "*",
  "/",
  "%",
  "**",
  "==",
  "!=",
  "===",
  "!==",
  "<",
  "<=",
  ">",
  ">=",
  "<<",
  ">>",
  ">>>",
  "&",
  "|",
  "^",
  "in",
  "instanceof",
])
  form(
    `binary:${op}`,
    op === "**" ? "ES2016" : "ES5",
    `a ${op} b;`,
    is("BinaryExpression", (n) => n.operator === op)
  );
for (const op of ["&&", "||", "??"])
  form(
    `logical:${op}`,
    op === "??" ? "ES2020" : "ES5",
    `a ${op} b;`,
    is("LogicalExpression", (n) => n.operator === op)
  );
for (const op of [
  "=",
  "+=",
  "-=",
  "*=",
  "/=",
  "%=",
  "**=",
  "<<=",
  ">>=",
  ">>>=",
  "&=",
  "|=",
  "^=",
  "&&=",
  "||=",
  "??=",
])
  form(
    `assign:${op}`,
    /^(\*\*=)$/.test(op) ? "ES2016" : /^(&&|\|\||\?\?)=$/.test(op) ? "ES2021" : "ES5",
    `a ${op} b;`,
    is("AssignmentExpression", (n) => n.operator === op)
  );
for (const op of ["!", "~", "+", "-", "typeof", "void", "delete"])
  form(
    `unary:${op}`,
    "ES5",
    op === "delete" ? "delete a.b;" : `${op} a;`,
    is("UnaryExpression", (n) => n.operator === op)
  );
form(
  "update:prefix++",
  "ES5",
  "++a;",
  is("UpdateExpression", (n) => n.prefix && n.operator === "++")
);
form(
  "update:postfix++",
  "ES5",
  "a++;",
  is("UpdateExpression", (n) => !n.prefix && n.operator === "++")
);
form(
  "update:prefix--",
  "ES5",
  "--a;",
  is("UpdateExpression", (n) => n.prefix && n.operator === "--")
);
form(
  "update:postfix--",
  "ES5",
  "a--;",
  is("UpdateExpression", (n) => !n.prefix && n.operator === "--")
);
form(
  "update:member",
  "ES5",
  "a.b++;",
  is("UpdateExpression", (n) => n.argument.type === "MemberExpression")
);
form(
  "typeof:identifier",
  "ES5",
  "typeof a;",
  is("UnaryExpression", (n) => n.operator === "typeof" && n.argument.type === "Identifier")
);
form(
  "delete:member",
  "ES5",
  "delete a.b;",
  is("UnaryExpression", (n) => n.operator === "delete" && n.argument.type === "MemberExpression")
);
form(
  "private-in",
  "ES2022",
  "class A { #x; static f(o) { return #x in o; } }",
  is("BinaryExpression", (n) => n.left.type === "PrivateIdentifier")
);
form("conditional", "ES5", "a ? b : c;", is("ConditionalExpression"));
form("sequence", "ES5", "a, b;", is("SequenceExpression"));
form(
  "assign:destructuring",
  "ES2015",
  "[a, b] = c;",
  is("AssignmentExpression", (n) => /Pattern/.test(n.left.type))
);
form(
  "assign:member",
  "ES5",
  "a.b = c;",
  is("AssignmentExpression", (n) => n.left.type === "MemberExpression")
);
// ---- member access, calls, new
form(
  "member:dot",
  "ES5",
  "a.b;",
  is("MemberExpression", (n) => !n.computed && n.property.type === "Identifier")
);
form(
  "member:computed",
  "ES5",
  "a[b];",
  is("MemberExpression", (n) => n.computed)
);
form(
  "member:private",
  "ES2022",
  "class A { #x; f() { return this.#x; } }",
  is("MemberExpression", (n) => n.property.type === "PrivateIdentifier")
);
form(
  "member:super",
  "ES2015",
  "class A extends B { f() { return super.x; } }",
  is("MemberExpression", (n) => n.object.type === "Super")
);
form(
  "call",
  "ES5",
  "f();",
  is("CallExpression", (n) => !n.optional)
);
form(
  "call:spread",
  "ES2015",
  "f(...a);",
  is("CallExpression", (n) => n.arguments.some((x) => x.type === "SpreadElement"))
);
form(
  "call:method",
  "ES5",
  "a.f();",
  is("CallExpression", (n) => n.callee.type === "MemberExpression")
);
form(
  "call:super",
  "ES2015",
  "class A extends B { constructor() { super(); } }",
  is("CallExpression", (n) => n.callee.type === "Super")
);
form(
  "call:iife",
  "ES5",
  "(function () {})();",
  is("CallExpression", (n) => /Function/.test(n.callee.type))
);
form(
  "new",
  "ES5",
  "new A(1);",
  is("NewExpression", (n) => n.arguments.length > 0)
);
form(
  "new:no-arguments",
  "ES5",
  "new A;",
  is("NewExpression", (n) => n.arguments.length === 0)
);
form(
  "new:spread",
  "ES2015",
  "new A(...b);",
  is("NewExpression", (n) => n.arguments.some((x) => x.type === "SpreadElement"))
);
form(
  "new:member",
  "ES5",
  "new a.B();",
  is("NewExpression", (n) => n.callee.type === "MemberExpression")
);
form(
  "optional:member",
  "ES2020",
  "a?.b;",
  is("MemberExpression", (n) => n.optional && !n.computed)
);
form(
  "optional:computed",
  "ES2020",
  "a?.[b];",
  is("MemberExpression", (n) => n.optional && n.computed)
);
form(
  "optional:call",
  "ES2020",
  "a?.();",
  is("CallExpression", (n) => n.optional)
);
form(
  "optional:chain-continues",
  "ES2020",
  "a?.b.c;",
  is(
    "ChainExpression",
    (n) => n.expression.type === "MemberExpression" && n.expression.object.type === "MemberExpression"
  )
);
form(
  "optional:method-call",
  "ES2020",
  "a?.b();",
  is(
    "ChainExpression",
    (n) => n.expression.type === "CallExpression" && n.expression.callee.type === "MemberExpression"
  )
);
form(
  "optional:delete",
  "ES2020",
  "delete a?.b;",
  is("UnaryExpression", (n) => n.operator === "delete" && n.argument.type === "ChainExpression")
);
// ---- literals, templates
form(
  "literal:string",
  "ES5",
  '"a";',
  is("Literal", (n) => typeof n.value === "string")
);
form(
  "literal:number",
  "ES5",
  "1;",
  is("Literal", (n) => typeof n.value === "number")
);
form(
  "literal:boolean",
  "ES5",
  "true;",
  is("Literal", (n) => typeof n.value === "boolean")
);
form(
  "literal:null",
  "ES5",
  "null;",
  is("Literal", (n) => n.raw === "null")
);
form(
  "literal:regexp",
  "ES5",
  "/a/g;",
  is("Literal", (n) => !!n.regex)
);
form(
  "literal:regexp-v",
  "ES2024",
  "/[\\p{L}--[a-z]]/v;",
  is("Literal", (n) => n.regex?.flags.includes("v"))
);
form(
  "literal:regexp-named-group",
  "ES2018",
  "/(?<a>b)/;",
  is("Literal", (n) => !!n.regex && n.regex.pattern.includes("(?<"))
);
form(
  "literal:bigint",
  "ES2020",
  "1n;",
  is("Literal", (n) => n.bigint !== undefined)
);
form(
  "literal:numeric-separator",
  "ES2021",
  "1_000;",
  is("Literal", (n) => typeof n.raw === "string" && n.raw.includes("_"))
);
form(
  "template",
  "ES2015",
  "`a`;",
  is("TemplateLiteral", (n) => n.expressions.length === 0)
);
form(
  "template:expressions",
  "ES2015",
  "`a${b}c`;",
  is("TemplateLiteral", (n) => n.expressions.length > 0)
);
form(
  "tagged-template",
  "ES2015",
  "a`b`;",
  is("TaggedTemplateExpression", (n) => n.tag.type === "Identifier")
);
form(
  "tagged-template:member",
  "ES2015",
  "a.b`c`;",
  is("TaggedTemplateExpression", (n) => n.tag.type === "MemberExpression")
);
form(
  "tagged-template:expressions",
  "ES2015",
  "a`b${c}`;",
  is("TaggedTemplateExpression", (n) => n.quasi.expressions.length > 0)
);
// ---- array and object literals
form(
  "array:hole",
  "ES5",
  "[, a];",
  is("ArrayExpression", (n) => n.elements.includes(null))
);
form("array:spread", "ES2015", "[...a];", (n, p) => n.type === "SpreadElement" && p?.type === "ArrayExpression");
form("object:spread", "ES2018", "({ ...a });", (n, p) => n.type === "SpreadElement" && p?.type === "ObjectExpression");
form(
  "object:shorthand",
  "ES2015",
  "({ a });",
  is("Property", (n) => n.shorthand && n.kind === "init")
);
form(
  "object:computed",
  "ES2015",
  "({ [a]: 1 });",
  is("Property", (n) => n.computed && n.kind === "init")
);
form(
  "object:method",
  "ES2015",
  "({ a() {} });",
  is("Property", (n) => n.method && !n.value.async && !n.value.generator)
);
form(
  "object:getter",
  "ES5",
  "({ get a() { return 1; } });",
  is("Property", (n) => n.kind === "get")
);
form(
  "object:setter",
  "ES5",
  "({ set a(v) {} });",
  is("Property", (n) => n.kind === "set")
);
form(
  "object:async-method",
  "ES2017",
  "({ async a() {} });",
  is("Property", (n) => n.method && n.value.async && !n.value.generator)
);
form(
  "object:generator-method",
  "ES2015",
  "({ *a() {} });",
  is("Property", (n) => n.method && n.value.generator && !n.value.async)
);
form(
  "object:computed-method",
  "ES2015",
  "({ [a]() {} });",
  is("Property", (n) => n.method && n.computed)
);
form(
  "object:proto",
  "ES2015",
  "({ __proto__: a });",
  (n, p) =>
    n.type === "Property" &&
    p?.type === "ObjectExpression" &&
    !n.computed &&
    !n.shorthand &&
    !n.method &&
    n.key.name === "__proto__"
);
form(
  "object:numeric-key",
  "ES5",
  "({ 1: a });",
  (n, p) =>
    n.type === "Property" &&
    p?.type === "ObjectExpression" &&
    n.key.type === "Literal" &&
    typeof n.key.value === "number"
);
form(
  "object:string-key",
  "ES5",
  '({ "a-b": 1 });',
  (n, p) =>
    n.type === "Property" &&
    p?.type === "ObjectExpression" &&
    n.key.type === "Literal" &&
    typeof n.key.value === "string"
);
form(
  "object:duplicate-key",
  "ES5",
  "({ a: 1, a: 2 });",
  (n) =>
    n.type === "ObjectExpression" &&
    new Set(n.properties.map((q) => q.key?.name ?? q.key?.value)).size < n.properties.filter((q) => q.key).length
);
// ---- yield, await, import()
form(
  "yield",
  "ES2015",
  "function* f() { yield 1; }",
  is("YieldExpression", (n) => !n.delegate)
);
form(
  "yield:delegate",
  "ES2015",
  "function* f() { yield* a; }",
  is("YieldExpression", (n) => n.delegate)
);
form(
  "yield:no-argument",
  "ES2015",
  "function* f() { yield; }",
  is("YieldExpression", (n) => n.argument === null)
);
form("await", "ES2017", "async function f() { await a; }", is("AwaitExpression"));
form("await:top-level", "ES2022", "await a;", (n, p, path) => n.type === "AwaitExpression" && !path.some(fn));
form("import()", "ES2020", 'import("a");', is("ImportExpression"));
// ---- classes
form("class:declaration", "ES2015", "class A {}", is("ClassDeclaration"));
form(
  "class:expression",
  "ES2015",
  "(class {});",
  is("ClassExpression", (n) => !n.id)
);
form(
  "class:named-expression",
  "ES2015",
  "(class A {});",
  is("ClassExpression", (n) => !!n.id)
);
form(
  "class:extends",
  "ES2015",
  "class A extends B {}",
  (n) => /^Class(Declaration|Expression)$/.test(n.type) && n.superClass?.type === "Identifier"
);
form(
  "class:extends-expression",
  "ES2015",
  "class A extends f() {}",
  (n) => /^Class(Declaration|Expression)$/.test(n.type) && n.superClass && n.superClass.type !== "Identifier"
);
form(
  "class:extends-null",
  "ES2015",
  "class A extends null {}",
  (n) => /^Class(Declaration|Expression)$/.test(n.type) && n.superClass?.raw === "null"
);
form(
  "class:constructor",
  "ES2015",
  "class A { constructor() {} }",
  is("MethodDefinition", (n) => n.kind === "constructor")
);
form(
  "method",
  "ES2015",
  "class A { f() {} }",
  is("MethodDefinition", (n) => n.kind === "method" && !n.static && !n.computed && !n.value.async && !n.value.generator)
);
form(
  "method:static",
  "ES2015",
  "class A { static f() {} }",
  is("MethodDefinition", (n) => n.static && n.kind === "method")
);
form(
  "method:getter",
  "ES2015",
  "class A { get f() { return 1; } }",
  is("MethodDefinition", (n) => n.kind === "get" && !n.static)
);
form(
  "method:setter",
  "ES2015",
  "class A { set f(v) {} }",
  is("MethodDefinition", (n) => n.kind === "set" && !n.static)
);
form(
  "method:static-getter",
  "ES2015",
  "class A { static get f() { return 1; } }",
  is("MethodDefinition", (n) => n.kind === "get" && n.static)
);
form(
  "method:static-setter",
  "ES2015",
  "class A { static set f(v) {} }",
  is("MethodDefinition", (n) => n.kind === "set" && n.static)
);
form(
  "method:computed",
  "ES2015",
  "class A { [a]() {} }",
  is("MethodDefinition", (n) => n.computed)
);
form(
  "method:generator",
  "ES2015",
  "class A { *f() {} }",
  is("MethodDefinition", (n) => n.value.generator && !n.value.async)
);
form(
  "method:async",
  "ES2017",
  "class A { async f() {} }",
  is("MethodDefinition", (n) => n.value.async && !n.value.generator)
);
form(
  "method:async-generator",
  "ES2018",
  "class A { async *f() {} }",
  is("MethodDefinition", (n) => n.value.async && n.value.generator)
);
form(
  "method:private",
  "ES2022",
  "class A { #f() {} }",
  is("MethodDefinition", (n) => n.key.type === "PrivateIdentifier" && n.kind === "method")
);
form(
  "method:private-accessor",
  "ES2022",
  "class A { get #f() { return 1; } }",
  is("MethodDefinition", (n) => n.key.type === "PrivateIdentifier" && /^(get|set)$/.test(n.kind))
);
form(
  "method:static-private",
  "ES2022",
  "class A { static #f() {} }",
  is("MethodDefinition", (n) => n.key.type === "PrivateIdentifier" && n.static)
);
form(
  "field",
  "ES2022",
  "class A { a = 1; }",
  is("PropertyDefinition", (n) => !n.static && !n.computed && n.key.type !== "PrivateIdentifier" && n.value !== null)
);
form(
  "field:no-initializer",
  "ES2022",
  "class A { a; }",
  is("PropertyDefinition", (n) => n.value === null)
);
form(
  "field:static",
  "ES2022",
  "class A { static a = 1; }",
  is("PropertyDefinition", (n) => n.static && n.key.type !== "PrivateIdentifier")
);
form(
  "field:computed",
  "ES2022",
  "class A { [a] = 1; }",
  is("PropertyDefinition", (n) => n.computed)
);
form(
  "field:private",
  "ES2022",
  "class A { #a = 1; }",
  is("PropertyDefinition", (n) => n.key.type === "PrivateIdentifier" && !n.static)
);
form(
  "field:static-private",
  "ES2022",
  "class A { static #a = 1; }",
  is("PropertyDefinition", (n) => n.key.type === "PrivateIdentifier" && n.static)
);
form("static-block", "ES2022", "class A { static {} }", is("StaticBlock"));
form(
  "field:arrow-initializer",
  "ES2022",
  "class A { f = () => this; }",
  is("PropertyDefinition", (n) => n.value?.type === "ArrowFunctionExpression")
);
// ---- patterns
form("pattern:object", "ES2015", "const { a } = b;", is("ObjectPattern"));
form("pattern:array", "ES2015", "const [a] = b;", is("ArrayPattern"));
form(
  "pattern:default",
  "ES2015",
  "const { a = 1 } = b;",
  (n, p) => n.type === "AssignmentPattern" && p?.type === "Property"
);
form(
  "pattern:rest-object",
  "ES2018",
  "const { a, ...b } = c;",
  (n, p) => n.type === "RestElement" && p?.type === "ObjectPattern"
);
form(
  "pattern:rest-array",
  "ES2015",
  "const [a, ...b] = c;",
  (n, p) => n.type === "RestElement" && p?.type === "ArrayPattern"
);
form(
  "pattern:nested",
  "ES2015",
  "const { a: [b] } = c;",
  (n, p) => n.type === "ArrayPattern" && p?.type === "Property"
);
form(
  "pattern:computed-key",
  "ES2015",
  "const { [a]: b } = c;",
  (n, p) => n.type === "Property" && p?.type === "ObjectPattern" && n.computed
);
form(
  "pattern:renamed",
  "ES2015",
  "const { a: b } = c;",
  (n, p) => n.type === "Property" && p?.type === "ObjectPattern" && !n.shorthand
);
form(
  "pattern:hole",
  "ES2015",
  "const [, a] = b;",
  is("ArrayPattern", (n) => n.elements.includes(null))
);
form(
  "pattern:default-array",
  "ES2015",
  "const [a = 1] = b;",
  (n, p) => n.type === "AssignmentPattern" && p?.type === "ArrayPattern"
);
form(
  "pattern:member-target",
  "ES2015",
  "[a.b] = c;",
  (n, p, path) =>
    n.type === "MemberExpression" &&
    (p?.type === "ArrayPattern" || (p?.type === "Property" && path.at(-2)?.type === "ObjectPattern"))
);
form(
  "pattern:object-assign",
  "ES2015",
  "({ a } = b);",
  is("AssignmentExpression", (n) => n.left.type === "ObjectPattern")
);
form(
  "pattern:array-swap",
  "ES2015",
  "[a, b] = [b, a];",
  is("AssignmentExpression", (n) => n.left.type === "ArrayPattern")
);
// ---- modules
form("import:default", "ES2015", 'import a from "m";', is("ImportDefaultSpecifier"));
form(
  "import:named",
  "ES2015",
  'import { a } from "m";',
  is("ImportSpecifier", (n) => n.imported.name === n.local.name)
);
form(
  "import:alias",
  "ES2015",
  'import { a as b } from "m";',
  is("ImportSpecifier", (n) => n.imported.name !== n.local.name)
);
form("import:namespace", "ES2015", 'import * as a from "m";', is("ImportNamespaceSpecifier"));
form(
  "import:side-effect",
  "ES2015",
  'import "m";',
  is("ImportDeclaration", (n) => n.specifiers.length === 0)
);
form(
  "import:attributes",
  "ES2025",
  'import a from "m" with { type: "json" };',
  is("ImportDeclaration", (n) => n.attributes?.length > 0)
);
form(
  "export:list",
  "ES2015",
  "const a = 1; export { a };",
  is("ExportNamedDeclaration", (n) => !n.declaration && !n.source)
);
form(
  "export:alias",
  "ES2015",
  "const a = 1; export { a as b };",
  is("ExportSpecifier", (n) => n.local.name !== n.exported.name)
);
form(
  "export:declaration",
  "ES2015",
  "export const a = 1;",
  is("ExportNamedDeclaration", (n) => n.declaration?.type === "VariableDeclaration")
);
form(
  "export:function",
  "ES2015",
  "export function f() {}",
  is("ExportNamedDeclaration", (n) => n.declaration?.type === "FunctionDeclaration")
);
form(
  "export:class",
  "ES2015",
  "export class A {}",
  is("ExportNamedDeclaration", (n) => n.declaration?.type === "ClassDeclaration")
);
form(
  "export:default-expression",
  "ES2015",
  "export default 1;",
  is("ExportDefaultDeclaration", (n) => !/Declaration$/.test(n.declaration.type))
);
form(
  "export:default-function",
  "ES2015",
  "export default function f() {}",
  is("ExportDefaultDeclaration", (n) => n.declaration.type === "FunctionDeclaration")
);
form(
  "export:default-class",
  "ES2015",
  "export default class A {}",
  is("ExportDefaultDeclaration", (n) => n.declaration.type === "ClassDeclaration")
);
form(
  "export:all",
  "ES2015",
  'export * from "m";',
  is("ExportAllDeclaration", (n) => !n.exported)
);
form(
  "export:all-as",
  "ES2020",
  'export * as a from "m";',
  is("ExportAllDeclaration", (n) => !!n.exported)
);
form(
  "export:from",
  "ES2015",
  'export { a } from "m";',
  is("ExportNamedDeclaration", (n) => !!n.source && n.specifiers.length > 0)
);
form(
  "export:default-specifier",
  "ES2015",
  "const a = 1; export { a as default };",
  is("ExportSpecifier", (n) => n.exported.name === "default")
);

export const FORMS = forms;

/** The forms and node types in a program, as counts. */
export function parseModule(source) {
  return acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
}

/** Visit every node with its parent and the chain of nodes above it (the parent last). */
export function walk(root, visit) {
  const path = [];
  const step = (node, parent) => {
    visit(node, parent, path);
    path.push(node);
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) {
        for (const child of value) if (child && typeof child.type === "string") step(child, node);
      } else if (value && typeof value.type === "string") step(value, node);
    }
    path.pop();
  };
  step(root, null);
}

/** `{ types: Map type -> count, forms: Map form -> count }` for a parsed program. */
export function featuresOf(ast) {
  const types = new Map();
  const found = new Map();
  walk(ast, (node, parent, path) => {
    types.set(node.type, (types.get(node.type) ?? 0) + 1);
    for (const f of FORMS) if (f.test(node, parent, path)) found.set(f.name, (found.get(f.name) ?? 0) + 1);
  });
  return { types, forms: found };
}
