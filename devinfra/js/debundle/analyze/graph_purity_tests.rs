use crate::analyze_chunk;
use analysis_vocabulary::inputs::AnalysisAssumptions;
use analysis_vocabulary::inputs::AnalysisInputs;
use purity::classifier::classify_expr_purity;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Decl;
use swc_ecma_ast::ModuleItem;
use swc_ecma_ast::Stmt;

use analysis_domain::item_views::{compute_shadowed_globals, top_level_item_views};
use analysis_test_support::{analyze_facts, test_id};
use analysis_vocabulary::purity::{Purity, PurityRule};
use purity::*;
use raw_js_test_support::parse;

/// The rules of the reasons a verdict projects its effects to, in evaluation
/// order; empty for a pure verdict. Function verdicts are stored projected, so
/// the rule list is the finest view of their effects the graph keeps.
fn rules_of(purity: &Purity) -> Vec<PurityRule> {
    match purity {
        Purity::Pure { .. } => Vec::new(),
        Purity::NotPure { reasons } => reasons.iter().map(|reason| reason.rule).collect(),
    }
}

/// Queries several purity facts from one unchanged source graph.
struct TestGraph {
    graph: ChunkCodeGraph,
}

impl TestGraph {
    fn new(src: &str) -> Self {
        Self::with_declared_pure(src, BTreeSet::new())
    }

    fn with_declared_pure(src: &str, declared_pure: BTreeSet<String>) -> Self {
        let module = parse(src);
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let graph = ChunkCodeGraph::build(
            &body,
            &shadowed,
            &declared_pure,
            &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
        );
        Self { graph }
    }

    /// `None` means the function is not tracked (only `const`-bound function
    /// and arrow initializers are cached).
    fn function_rules(&self, name: &str) -> Option<Vec<PurityRule>> {
        self.graph
            .function_purity(name)
            .map(|purity| rules_of(purity.as_ref()))
    }

    fn is_plain_data(&self, name: &str) -> bool {
        self.graph.is_plain_data(name)
    }

    fn is_plain_array(&self, name: &str) -> bool {
        self.graph.is_plain_array(name)
    }
}

fn fn_rules(src: &str, name: &str) -> Option<Vec<PurityRule>> {
    TestGraph::new(src).function_rules(name)
}

#[test]
fn fn_purity_mutual_recursion_converges_pure() {
    // `even` and `odd` only reference each other inside their
    // bodies. Optimistic init (Pure) holds through the
    // fixed-point — neither body has an impure operation. (The
    // recursion argument is passed through unchanged: `n - 1`
    // would be a coercing operator on a possibly-object operand,
    // which classifies NotPure by design — this test pins
    // fixed-point convergence, not arithmetic.)
    let src = r#"
    function even(n) { return n === 0 ? true : odd(n); }
    function odd(n) { return n === 0 ? false : even(n); }
"#;
    let graph = TestGraph::new(src);
    assert_eq!(graph.function_rules("even"), Some(vec![]));
    assert_eq!(graph.function_rules("odd"), Some(vec![]));
}

#[test]
fn fn_purity_call_inherits_chunk_local_function_purity() {
    // `f()` where `f` is a chunk-top function in the cache
    // resolves through `ChunkCodeGraph::function_purity`. With
    // `f` body Pure, the call is Pure.
    let module = parse("function f() { return 42; } const x = f();");
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
    );
    let var = match body[1].as_module_item() {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected VarDecl, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init");
    assert!(
        classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
            .kinds()
            .is_empty()
    );
}

/// Build a `ChunkCodeGraph` for `src` and ask whether `name` is
/// tracked as a plain-data binding. `false` covers both "not
/// tracked" and "tracked as Function" — only `PlainData` returns
/// `true`. Used to pin which chunk-top consts the analyzer
/// admits as accessor-free data shapes.
fn is_plain_data(src: &str, name: &str) -> bool {
    TestGraph::new(src).is_plain_data(name)
}

fn is_plain_array(src: &str, name: &str) -> bool {
    TestGraph::new(src).is_plain_array(name)
}

/// `(case, source, binding, expected purity rules)`; `None` means untracked.
type FnRulesCase<'a> = (&'a str, &'a str, &'a str, Option<&'a [PurityRule]>);

/// `(case, source, binding, expected plain-data admission)`.
type PlainDataCase<'a> = (&'a str, &'a str, &'a str, bool);

fn assert_plain_data_cases(cases: &[PlainDataCase<'_>]) {
    for (case, src, binding, admitted) in cases {
        assert_eq!(is_plain_data(src, binding), *admitted, "{case}");
    }
}

/// The purity rules of the chunk fact declaring `binding`.
fn fact_rules(src: &str, binding: &str) -> Vec<PurityRule> {
    let module = parse(src);
    let facts = analyze_facts(&module);
    let fact = facts
        .iter()
        .find(|fact| fact.declared.contains(&test_id(binding)))
        .unwrap_or_else(|| panic!("{binding} declaration"));
    rules_of(&fact.purity)
}

#[test]
fn chunk_fact_purity_rules() {
    let cases: [(&str, &str, &str, &[PurityRule]); 4] = [
        (
            "local class construction before its declaration",
            "const instance = new Registry(); class Registry { value = 1; }",
            "instance",
            &[PurityRule::UnknownNew],
        ),
        (
            "direct eval disables the local class construction proof",
            "class Registry { value = 1; } eval('Registry = class {}'); const instance = new Registry();",
            "instance",
            &[PurityRule::UnknownNew],
        ),
        (
            "new Map of fresh-spread registry entries with plain-data members",
            r#"
    const m = {
        systemToolWebSearchId: "SYS_FN16",
        dataTypeUrlId: "SYS_D10",
    };
    const includeUrl = true;
    const registry = new Map([
        ...[[m.systemToolWebSearchId, () => io()]],
        ...(includeUrl ? [[m.dataTypeUrlId, function () { globalThis.touched = true; }]] : []),
    ]);
"#,
            "registry",
            &[],
        ),
        (
            "fresh-spread registry does not bless an unknown member read",
            r#"
    const registry = new Map([
        ...[[m.systemToolWebSearchId, () => 1]],
    ]);
"#,
            "registry",
            &[PurityRule::UnknownNew, PurityRule::UnknownMember],
        ),
    ];
    for (case, src, binding, expected) in cases {
        assert_eq!(fact_rules(src, binding), expected, "{case}");
    }
}

#[test]
fn enum_member_reads_need_plain_data_after_the_initializer() {
    // An enum member resolves against its settled own fields only while no
    // write or escape reaches the object after its initializer; a binding
    // another statement rebinds never resolves.
    let initializer =
        "var Flags = ((values) => ((values[(values.FIRST = 1)] = 'FIRST'), values))(Flags || {});";
    for (suffix, admitted) in [
        ("", true),
        ("[Flags] = [{ FIRST: () => 1 }];", false),
        ("({ Flags } = { Flags: { FIRST: () => 1 } });", false),
        (
            "Object.defineProperty(Flags, 'FIRST', { get() { return 1; } });",
            false,
        ),
        ("Flags = new Proxy(Flags, {});", false),
        ("globalThis.savedFlags = Flags;", false),
    ] {
        assert_eq!(
            is_plain_data(&format!("{initializer}\n{suffix}"), "Flags"),
            admitted,
            "incorrect enum plain-data fact after {suffix:?}"
        );
    }
    assert!(
        is_plain_data(
            "var Flags = ((values) => ((values.FIRST = 1), values))(Flags || (Flags = {}));",
            "Flags"
        ),
        "the recognized initializer self-write must remain admitted"
    );
}

#[test]
fn function_purity_rules_by_body_shape() {
    use PurityRule::*;
    let cases: [FnRulesCase<'_>; 24] = [
        // SOUNDNESS: `n - 1` runs ToNumeric on `n`; a caller can pass an
        // object whose `valueOf` fires arbitrary code.
        (
            "coercing an opaque param",
            "function dec(n) { return n - 1; }",
            "dec",
            Some(&[CoercingOperator]),
        ),
        // A concise-arrow body classifies the single return expression.
        (
            "const concise arrow",
            "const wrap = (x) => ({ val: x });",
            "wrap",
            Some(&[]),
        ),
        // `let` / `var` bindings are reassignable: a later `f = …` could
        // replace the value with something impure, so only `const`-bound
        // function/arrow initializers are cached.
        (
            "let-bound arrow is untracked",
            "let f = () => 1;",
            "f",
            None,
        ),
        (
            "var-bound function expr is untracked",
            "var f = function () { return 1; };",
            "f",
            None,
        ),
        (
            "const-bound arrow is tracked",
            "const f = () => 1;",
            "f",
            Some(&[]),
        ),
        // `throw` alters control flow observably regardless of the thrown
        // value; reachability is not reasoned about (soundness-first).
        (
            "unconditional throw",
            r#"function f() { throw "boom"; }"#,
            "f",
            Some(&[ThrowStmt]),
        ),
        (
            "conditional throw",
            r#"function f(x) { if (x) throw "boom"; return x; }"#,
            "f",
            Some(&[ThrowStmt]),
        ),
        // `debugger` pauses execution observably to an attached host.
        (
            "debugger",
            "function f() { debugger; return 1; }",
            "f",
            Some(&[DebuggerStmt]),
        ),
        // A non-computed read on a PlainData receiver has no key
        // sub-expression to validate.
        (
            "static property read on plain data",
            r#"
    const mr = { FUNCTIONS_EMULATOR: false, FOO: 1 };
    const check = () => mr.FUNCTIONS_EMULATOR;
"#,
            "check",
            Some(&[]),
        ),
        // Default-value expressions evaluate at call time, like body code.
        (
            "assigning param default",
            "const f = (x = (globalThis.boom = 1)) => 1;",
            "f",
            Some(&[AssignOrUpdate]),
        ),
        (
            "calling param default",
            "function g(x = io()) { return 1; }",
            "g",
            Some(&[UnknownCall]),
        ),
        (
            "primitive param default",
            "const f = (x = 1) => x;",
            "f",
            Some(&[]),
        ),
        (
            "fresh array param default",
            "const g = (x = []) => x;",
            "g",
            Some(&[]),
        ),
        // Destructuring fires getters (object pattern) or the iterator
        // protocol (array pattern) on the argument.
        (
            "object pattern param",
            "const f = ({ a }) => a;",
            "f",
            Some(&[DestructuringPattern]),
        ),
        (
            "array pattern param",
            "const g = ([x]) => x;",
            "g",
            Some(&[DestructuringPattern]),
        ),
        (
            "defaulted object pattern param",
            "function h({ a } = {}) { return a; }",
            "h",
            Some(&[DestructuringPattern]),
        ),
        // A rest param builds a fresh array from the arguments list.
        ("rest param", "const f = (...xs) => xs;", "f", Some(&[])),
        // for-of fires `[Symbol.iterator]`; for-in fires proxy
        // ownKeys/getOwnPropertyDescriptor traps.
        (
            "for-of",
            "function f(xs) { for (const x of xs) {} return 1; }",
            "f",
            Some(&[IterationProtocol]),
        ),
        (
            "for-in",
            "function f(o) { for (const k in o) {} return 1; }",
            "f",
            Some(&[IterationProtocol]),
        ),
        // Plain `while` / C-style `for` fire no protocol beyond their
        // independently classified sub-expressions.
        (
            "while loop with assigning body",
            "function f(flag) { while (flag) { flag = false; } return 1; }",
            "f",
            Some(&[AssignOrUpdate]),
        ),
        (
            "while loop control",
            "function g(flag) { while (flag) {} return 1; }",
            "g",
            Some(&[]),
        ),
        (
            "object pattern declarator",
            "function f(o) { const { a } = o; return a; }",
            "f",
            Some(&[DestructuringPattern]),
        ),
        (
            "array pattern declarator",
            "function g(o) { const [x] = o; return x; }",
            "g",
            Some(&[DestructuringPattern]),
        ),
        (
            "plain declarator control",
            "function h(o) { const a = o; return a; }",
            "h",
            Some(&[]),
        ),
    ];
    for (case, src, name, expected) in cases {
        assert_eq!(fn_rules(src, name).as_deref(), expected, "{case}");
    }
}

#[test]
fn plain_data_initializer_shapes() {
    assert_plain_data_cases(&[
        // KeyValue with non-computed keys and plain values: reads fire no
        // user code.
        (
            "const object literal",
            r#"const TA = { FOO: "bar", BAZ: 1 };"#,
            "TA",
            true,
        ),
        ("const array literal", "const TA = [1, 2, 3];", "TA", true),
        // `let` / `var` admit when the chunk-wide write scan finds no
        // non-plain reassignment. Hoisted pre-init reads of a `var` see
        // `undefined`, whose member read throws an engine TypeError, not
        // user code.
        ("let object literal", "let TA = { a: 1 };", "TA", true),
        ("let array literal", "let TA = [1, 2, 3];", "TA", true),
        ("var object literal", "var TA = { a: 1 };", "TA", true),
        ("var array literal", "var TA = [1, 2, 3];", "TA", true),
        // Every `var` initializer of the shared binding must pass, in any
        // order and in comma lists; `var X;` alone carries no evidence.
        (
            "var redeclarations all plain",
            r#"
    var X = { a: 1 };
    var X = { b: 2 };
"#,
            "X",
            true,
        ),
        (
            "var redeclaration with a call initializer",
            r#"
    var X = { a: 1 };
    var X = io();
"#,
            "X",
            false,
        ),
        (
            "accessor initializer first",
            "var X = { get a() { return 1; } }; var X = { a: 1 };",
            "X",
            false,
        ),
        (
            "accessor initializer last",
            "var X = { a: 1 }; var X = { get a() { return 1; } };",
            "X",
            false,
        ),
        (
            "comma declarator with a call initializer",
            "var X = { a: 1 }, X = io();",
            "X",
            false,
        ),
        // The computed key captures X although this declarator has no simple
        // binding candidate.
        (
            "pattern declarator computed key captures",
            "var X = { a: 1 }; var { [capture(X)]: y } = object;",
            "X",
            false,
        ),
        ("uninitialized var alone", "var X;", "X", false),
        (
            "uninitialized var then plain initializer",
            r#"
    var X;
    var X = { a: 1 };
"#,
            "X",
            true,
        ),
        // A getter, setter or method in the literal makes member reads run
        // user code.
        (
            "const with getter",
            "const X = { get a() { return io(); } };",
            "X",
            false,
        ),
        (
            "const with setter",
            "const X = { set a(v) {}, };",
            "X",
            false,
        ),
        (
            "const with method",
            "const X = { m() { return io(); } };",
            "X",
            false,
        ),
        // `CopyDataProperties` and array spread write data descriptors
        // regardless of the source's shape. The spread's own init-time
        // effects belong to the declaring statement.
        (
            "object spread initializer",
            "const X = { ...other, a: 1 };",
            "X",
            true,
        ),
        (
            "array spread initializer",
            "const X = [1, ...other];",
            "X",
            true,
        ),
        // A computed key can name a Symbol mapping to a prototype accessor.
        ("computed key", "const X = { [k]: 1 };", "X", false),
        // `{__proto__: P}` sets the prototype (ES262 §13.2.5.5); P's
        // accessors would fire on reads.
        (
            "__proto__ key",
            "const X = { __proto__: other, a: 1 };",
            "X",
            false,
        ),
        (
            "quoted __proto__ key",
            r#"const X = { "__proto__": other, a: 1 };"#,
            "X",
            false,
        ),
        // Function initializers are tracked as `Function`, not `PlainData`.
        ("const arrow", "const f = (x) => x;", "f", false),
        (
            "const function expression",
            "const f = function() { return 1; };",
            "f",
            false,
        ),
    ]);
}

#[test]
fn plain_data_post_initializer_uses() {
    assert_plain_data_cases(&[
        (
            "write through an alias",
            "const lookup = { key: 'token' }, alias = lookup; alias.key = () => later;",
            "lookup",
            false,
        ),
        // Regression: a method stored on the object runs with `this` bound to
        // it, so calling one that writes through `this` mutates the object.
        // Arrow methods, `this`-free functions and inherited intrinsics leave
        // it plain.
        (
            "method writing through this",
            "const o = { x: 1, m: function () { this.x = 2; } }; o.m();",
            "o",
            false,
        ),
        (
            "method writing through this via alias",
            "const o = { x: 1, m: function () { this.x = 2; } }; const alias = o; alias.m();",
            "o",
            false,
        ),
        (
            "shorthand function writing through this",
            "function bump() { this.x = 2; } const o = { x: 1, bump }; o.bump();",
            "o",
            false,
        ),
        (
            "this-free methods and intrinsics",
            "function read() { return 3; } \
         const o = { x: 1, m: () => 2, n: function () { return 3; }, read }; \
         o.m(); o.n(); o.read(); o.hasOwnProperty('x');",
            "o",
            true,
        ),
        // A `let` rebinding whose RHS is not a syntactic plain literal could
        // produce an accessor-bearing value; `X || {}` short-circuits to
        // either operand.
        (
            "let reassigned to a call result",
            "let X = { a: 1 }; function reassign() { X = someFn(); }",
            "X",
            false,
        ),
        (
            "let reassigned to a param",
            "let X = { a: 1 }; function reassign(other) { X = other; }",
            "X",
            false,
        ),
        (
            "let reassigned to a short-circuit",
            "let X = { a: 1 }; function reassign() { X = X || {}; }",
            "X",
            false,
        ),
        (
            "let reassigned to an accessor literal",
            "let X = { a: 1 }; function bad() { X = { get a() { return io(); } }; }",
            "X",
            false,
        ),
        (
            "let updated",
            "let X = { a: 1 }; function bad() { X++; }",
            "X",
            false,
        ),
        // Any member write disqualifies, even inside a function body that
        // might run before the read being classified.
        (
            "top-level member write",
            "const X = { a: 1 }; X.b = 2;",
            "X",
            false,
        ),
        (
            "member write in a function",
            "const X = { a: 1 }; function mut() { X.a = 9; }",
            "X",
            false,
        ),
        // Accessor-installing or prototype-changing builtins, and
        // `Object.assign` (the rule is "never written through").
        (
            "Object.defineProperty",
            r#"const X = { a: 1 }; Object.defineProperty(X, "a", { get: () => io() });"#,
            "X",
            false,
        ),
        (
            "Object.defineProperties",
            "const X = { a: 1 }; Object.defineProperties(X, descriptors);",
            "X",
            false,
        ),
        (
            "Object.setPrototypeOf",
            "const X = { a: 1 }; Object.setPrototypeOf(X, P);",
            "X",
            false,
        ),
        (
            "Reflect.defineProperty",
            r#"const X = { a: 1 }; Reflect.defineProperty(X, "a", { get: () => io() });"#,
            "X",
            false,
        ),
        (
            "Object.assign target",
            "const X = { a: 1 }; Object.assign(X, src);",
            "X",
            false,
        ),
        // A local alias preserves identity; a write through it disqualifies
        // the original.
        (
            "unused alias",
            "const X = { a: 1 }; const Y = X;",
            "X",
            true,
        ),
        (
            "defineProperty through alias",
            r#"const X = { a: 1 }; const Y = X; Object.defineProperty(Y, "a", { get: () => io() });"#,
            "X",
            false,
        ),
        // Escapes into calls, containers and value-producing conditionals.
        ("call argument", "const X = { a: 1 }; f(X);", "X", false),
        (
            "constructor argument",
            "const X = { a: 1 }; new Thing(X);",
            "X",
            false,
        ),
        (
            "array element",
            "const X = { a: 1 }; const arr = [X];",
            "X",
            false,
        ),
        (
            "object property value",
            "const X = { a: 1 }; const o = { x: X };",
            "X",
            false,
        ),
        (
            "shorthand property",
            "const X = { a: 1 }; const o = { X };",
            "X",
            false,
        ),
        (
            "conditional alias",
            "const X = { a: 1 }; const Y = flag ? X : null;",
            "X",
            false,
        ),
        (
            "short-circuit alias",
            "const X = { a: 1 }; const Y = X || {};",
            "X",
            false,
        ),
        // Provably non-capturing positions keep admission.
        (
            "member receiver",
            "const X = { a: 1 }; const v = X.a;",
            "X",
            true,
        ),
        (
            "object spread source",
            "const X = { a: 1 }; const Y = { ...X };",
            "X",
            true,
        ),
        (
            "array spread source",
            "const X = [1]; const Y = [...X];",
            "X",
            true,
        ),
        (
            "typeof operand",
            "const X = { a: 1 }; const t = typeof X;",
            "X",
            true,
        ),
        (
            "not operand",
            "const X = { a: 1 }; const b = !X;",
            "X",
            true,
        ),
        (
            "Object.keys argument",
            "const X = { a: 1 }; const ks = Object.keys(X);",
            "X",
            true,
        ),
        (
            "return value",
            "const X = { a: 1 }; function f() { return X; }",
            "X",
            true,
        ),
        (
            "concise arrow body",
            "const X = { a: 1 }; const f = () => X;",
            "X",
            true,
        ),
        (
            "export specifier",
            "const X = { a: 1 }; export { X };",
            "X",
            true,
        ),
        // With `Object` rebound, `Object.keys(X)` calls a user function.
        (
            "Object.keys argument with shadowed Object",
            "const userland = 1; const Object = userland; const X = { a: 1 }; const ks = Object.keys(X);",
            "X",
            false,
        ),
    ]);
}

#[test]
fn plain_data_ts_enum_iife_shapes() {
    assert_plain_data_cases(&[
        // Param-scope tracking: `p.A = "a"` writes the IIFE parameter, not
        // the outer `X`.
        (
            "arrow comma body",
            r#"var X = ((p) => (p.A = "a", p.B = "b", p))({});"#,
            "X",
            true,
        ),
        // Canonical TypeScript emit: self-assigning `X || (X = {})` arg with a
        // param shadowing the binding.
        (
            "canonical self-assigning short-circuit",
            r#"var X = ((X) => (X["A"] = "a", X["B"] = "b", X))(X || (X = {}));"#,
            "X",
            true,
        ),
        // esbuild's `X || {}` lowering.
        (
            "or-plain argument",
            r#"var ColorName = ((p) => (p["Red"] = "red", p["Green"] = "green", p))(ColorName || {});"#,
            "ColorName",
            true,
        ),
        (
            "function expression body",
            r#"var Color = (function (p) { p.RED = "red"; p.GREEN = "green"; return p; })(Color || {});"#,
            "Color",
            true,
        ),
        // Equivalent to `var X = X || {}` after evaluation.
        (
            "degenerate return of the param",
            r#"var X = ((p) => p)({});"#,
            "X",
            true,
        ),
        (
            "computed numeric keys",
            r#"var X = ((p) => (p[0] = "zero", p[1] = "one", p))({});"#,
            "X",
            true,
        ),
        // TypeScript numeric-enum reverse map `n[(n.A = 0)] = "A"`: both
        // writes are data properties.
        (
            "numeric reverse mapping",
            r#"var E = ((n) => (n[(n.A = 0)] = "A", n))(E || {});"#,
            "E",
            true,
        ),
        (
            "numeric reverse mapping with string-key forward write",
            r#"var E = ((n) => (n[(n["A"] = 0)] = "A", n))(E || {});"#,
            "E",
            true,
        ),
        // Writes to a separate IIFE's param named `X` don't disqualify the
        // outer plain `X`.
        (
            "inner param write leaves the outer candidate",
            r#"
    var X = { existing: 1 };
    (function (X) { X.A = "a"; })({});
"#,
            "X",
            true,
        ),
        // `import { j as a }` binds only `a`; the imported NAME `j` is module
        // metadata. Regression: the escape default used to fire on it, losing
        // enum candidates by name coincidence with vendor exports.
        (
            "import specifier name collision",
            r#"
    import { j as a } from "./vendor.js";
    var j = ((n) => ((n[(n.A = 0)] = "A"), n))(j || {});
    const probe = j.A;
"#,
            "j",
            true,
        ),
        // The IIFE must return the object synchronously.
        (
            "async arrow",
            "var X = (async (p) => (p.A = 1, p))({});",
            "X",
            false,
        ),
        (
            "async function",
            "var X = (async function (p) { p.A = 1; return p; })({});",
            "X",
            false,
        ),
        (
            "generator function",
            "var X = (function* (p) { p.A = 1; return p; })({});",
            "X",
            false,
        ),
        (
            "reverse mapping writes __proto__",
            r#"var X = ((p) => (p[(p.A = "__proto__")] = "A", p))({});"#,
            "X",
            false,
        ),
        // `Other || {}` could pass an arbitrary existing object.
        (
            "short-circuit on another binding",
            r#"var X = ((p) => (p.A = "a", p))(Other || {});"#,
            "X",
            false,
        ),
        // A chunk-local function's body is unknown to the IIFE check.
        (
            "call to a named function",
            r#"
    function makeIt(p) { return p; }
    var X = makeIt({ a: 1 });
"#,
            "X",
            false,
        ),
        (
            "non-literal argument",
            r#"var X = ((p) => (p.A = "a", p))(someFn());"#,
            "X",
            false,
        ),
        (
            "call in body",
            r#"var X = ((p) => (p.A = "a", observe(p), p))(X || {});"#,
            "X",
            false,
        ),
        (
            "global write in body",
            r#"var X = ((p) => (globalThis.A = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "other-object write in body",
            r#"var X = ((p) => (other.A = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "opaque computed key",
            r#"var X = ((p) => (p[key] = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "bracketed __proto__ key",
            r#"var X = ((p) => (p["__proto__"] = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "dotted __proto__ key",
            r#"var X = ((p) => (p.__proto__ = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "param escapes",
            r#"var X = ((p) => (leaked = p, p.A = "a", p))(X || {});"#,
            "X",
            false,
        ),
        (
            "body returns another value",
            r#"var X = ((p) => (p.A = "a", other))({});"#,
            "X",
            false,
        ),
        // Only the comma-expression of property writes is admitted.
        (
            "defineProperty in body",
            r#"var X = ((p) => (Object.defineProperty(p, "A", { get: () => io() }), p))({});"#,
            "X",
            false,
        ),
        // The standard chunk-wide write check still applies outside the IIFE.
        (
            "outer member write",
            r#"
    var X = ((p) => (p.A = "a", p))({});
    function mut() { X.B = "b"; }
"#,
            "X",
            false,
        ),
        // The current rule requires the single-param shape.
        ("no param", r#"var X = (() => ({ a: 1 }))();"#, "X", false),
        // The nested reverse-map write must target the same param with a
        // primitive RHS.
        (
            "nested key writes another object",
            r#"var X = ((n) => (n[(other.X = 0)] = "X", n))({});"#,
            "X",
            false,
        ),
        (
            "nested key with call RHS",
            r#"var X = ((n) => (n[(n.A = io())] = "A", n))({});"#,
            "X",
            false,
        ),
    ]);
}

#[test]
fn plain_array_const_derived_by_non_mutating_methods_is_tracked() {
    let src = r#"
const later = "ready";
const pairs = [["tool", "tool-id"]];
const tools = pairs.map(([name, systemNodeId]) => ({
  name,
  systemNodeId,
  run: () => later,
}));
const filtered = tools.filter((tool) => tool.systemNodeId);
const cloned = filtered.slice();
export { tools, filtered, cloned };
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_array("pairs"));
    assert!(graph.is_plain_array("tools"));
    assert!(graph.is_plain_array("filtered"));
    assert!(graph.is_plain_array("cloned"));
}

#[test]
fn plain_array_const_mutating_method_is_not_tracked() {
    assert!(!is_plain_array(
        "const values = []; values.push(1);",
        "values"
    ));
}

#[test]
fn plain_array_derived_candidates_keep_escape_and_mutation_vetoes() {
    let src = r#"
const source = [];
const escaped = source.map((value) => value);
consume(escaped);
const mutated = source.slice();
mutated.push(1);
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_array("source"));
    assert!(!graph.is_plain_array("escaped"));
    assert!(!graph.is_plain_array("mutated"));
}

#[test]
fn plain_data_var_nested_block_reinitializer_disqualifies_value_inference() {
    let src = r#"
    var X = { a: 1 };
    if (condition) {
        var X = { get a() { globalThis.touched = true; return 2; } };
    }
    const read = () => X.a;
"#;

    let graph = TestGraph::new(src);
    assert!(!graph.is_plain_data("X"));
    assert_eq!(
        graph.function_rules("read"),
        Some(vec![PurityRule::UnknownMember])
    );
}

#[test]
fn plain_data_var_for_of_rebind_disqualifies_value_inference() {
    let src = r#"
    var X = { a: 1 };
    for (var X of values) { break; }
    const read = () => X.a;
"#;

    let graph = TestGraph::new(src);
    assert!(!graph.is_plain_data("X"));
    assert_eq!(
        graph.function_rules("read"),
        Some(vec![PurityRule::UnknownMember])
    );
}

#[test]
fn plain_data_var_unblocks_embedded_build_env_config_shape() {
    // The gaffer `embeddedBuildEnvConfig` (`var mr = { … }`)
    // shape: bundler-emitted `var` plain-object literal with no
    // writes elsewhere in the chunk. Pre-this-extension, `mr`
    // was excluded from PlainData (const/let only), making
    // `mr.FUNCTIONS_EMULATOR` flag `unknown_member` and
    // forcing a load-bearing `purity: pure` hint on the
    // downstream `isEmulatorEnv` accessor. With `var`
    // admission, `mr` is PlainData → `mr.FUNCTIONS_EMULATOR`
    // pure → `isEmulatorEnv` body classifies pure with zero
    // hints.
    let src = r#"
    var mr = { FUNCTIONS_EMULATOR: false, OTHER: "x" };
    const isEmulatorEnv = () => mr.FUNCTIONS_EMULATOR;
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("mr"));
    assert_eq!(graph.function_rules("isEmulatorEnv"), Some(vec![]));
}

#[test]
fn plain_data_ts_enum_iife_member_reads_classify_pure() {
    // End-to-end: a downstream accessor reading the enum's
    // members classifies pure because the binding admits as
    // PlainData and the read targets a static property.
    let src = r#"
    var Color = ((p) => (p.RED = "red", p.GREEN = "green", p))(Color || {});
    const isRed = (c) => c === Color.RED;
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("Color"));
    assert_eq!(graph.function_rules("isRed"), Some(vec![]));
}

#[test]
fn plain_data_default_param_write_to_shadowed_m_keeps_system_ids_plain() {
    // an upstream Vite-style shape: the chunk also has a top-level `const m`
    // plain-data systemIds table, while Vite emits a helper with
    // a default parameter named `m` and a later default param
    // writes `m.f = [...]`. That write targets the parameter
    // binding, not the top-level systemIds object, so it must
    // not disqualify top-level `m` from PlainData.
    let src = r#"
    const m = {
        systemToolWebSearchId: "SYS_FN16",
        dataTypeUrlId: "SYS_D10",
    };
    const __vite__mapDeps = (i, m = __vite__mapDeps, d = m.f || (m.f = [0, 1])) => d;
    const readSystemToolId = () => m.systemToolWebSearchId;
    const readDataTypeUrlId = () => m.dataTypeUrlId;
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("m"));
    assert_eq!(graph.function_rules("readSystemToolId"), Some(vec![]));
    assert_eq!(graph.function_rules("readDataTypeUrlId"), Some(vec![]));
}

#[test]
fn plain_data_rest_and_destructuring_param_writes_shadow_top_level_candidate() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    function viaRest(...m) { m.f = [0]; }
    function viaObject({ m }) { m.f = [1]; }
    function viaArray([m]) { m.f = [2]; }
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("m"));
    assert_eq!(graph.function_rules("readSystemToolId"), Some(vec![]));
}

#[test]
fn plain_data_member_read_on_param_shadowed_name_is_not_pure() {
    // SOUNDNESS regression: chunk-top `const X = {a:1}` registers X as
    // PlainData, but inside `function f(X)` the param `X` lexically
    // shadows the const. `X.a` there reads the *argument* (which may be
    // an object with a getter), so `f` must NOT classify Pure — admitting
    // it would drop an S-edge and let a cyclic spec slip past the
    // realizability validator. Mirrors
    // `plain_data_rest_and_destructuring_param_writes_shadow_top_level_candidate`.
    let src = r#"
    const X = { a: 1 };
    function f(X) { return X.a; }
    const readReal = () => X.a;
"#;
    // The top-level const is still genuine PlainData (the param write
    // targets the local binding, not the const).
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("X"));
    // A genuine chunk-top read stays pure.
    assert_eq!(graph.function_rules("readReal"), Some(vec![]));
    // The shadowed read must be impure.
    assert_eq!(
        graph.function_rules("f"),
        Some(vec![PurityRule::UnknownMember])
    );
}

#[test]
fn plain_data_object_keys_arg_on_param_shadowed_name_is_not_pure() {
    // Same soundness hole on the `Object.keys/values/entries(X)` arg path
    // (`plain_data_operand`): when `X` is a function param, the
    // arg is the local value, not the plain-data const.
    let src = r#"
    const X = { a: 1 };
    function f(X) { return Object.keys(X); }
    const readReal = () => Object.keys(X);
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("X"));
    assert_eq!(graph.function_rules("readReal"), Some(vec![]));
    assert_eq!(
        graph.function_rules("f"),
        Some(vec![PurityRule::UnknownCall])
    );
}

#[test]
fn plain_data_real_top_level_m_write_still_disqualifies_after_vite_helper() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    const __vite__mapDeps = (i, m = __vite__mapDeps, d = m.f || (m.f = [0, 1])) => d;
    function mutateRealM() { m.f = [2]; }
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    let graph = TestGraph::new(src);
    assert!(!graph.is_plain_data("m"));
    assert_eq!(
        graph.function_rules("readSystemToolId"),
        Some(vec![PurityRule::UnknownMember])
    );
}

#[test]
fn plain_data_unshadowed_default_param_write_disqualifies_top_level_candidate() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    const helper = (d = (m.f = [0, 1])) => d;
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    let graph = TestGraph::new(src);
    assert!(!graph.is_plain_data("m"));
    assert_eq!(
        graph.function_rules("readSystemToolId"),
        Some(vec![PurityRule::UnknownMember])
    );
}

#[test]
fn plain_data_let_with_plain_literal_replacement_is_tracked() {
    // The `applySystemConfigOverrides` shape from gaffer-private:
    // `let envConfig = {…}` with every reassignment being
    // `envConfig = { ...envConfig, ...n }`. Object spread in the
    // RHS is admitted — `CopyDataProperties` writes data
    // descriptors regardless of source shape, so the post-write
    // value still has no accessor channels.
    let src = r#"
    let envConfig = { REACT_APP_ENV: "production", FOO: 1 };
    const applyOverrides = (n) => {
        envConfig = { ...envConfig, ...n };
    };
    const getEnv = (k) => envConfig[k];
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("envConfig"));
    // `getEnv` is no longer inferred pure: `envConfig[k]` runs
    // ToPropertyKey on the opaque key `k` (user `toString` on an
    // object key) — see `plain_data_computed_read_with_opaque_key_is_not_pure`.
    assert_eq!(
        graph.function_rules("getEnv"),
        Some(vec![PurityRule::ToPropertyKeyCoercion])
    );
}

#[test]
fn plain_data_computed_read_with_opaque_key_is_not_pure() {
    // SOUNDNESS (ToPropertyKey): `TA[n]` runs ToPropertyKey on the
    // key VALUE before the (accessor-free) lookup on the PlainData
    // receiver. A caller can pass an object `n` whose `toString`
    // fires arbitrary code, so the accessor body is NOT
    // pure-callable even though the receiver is PlainData.
    // (This deliberately re-restricts the earlier
    // `Me = (n) => TA[n]` flagship shape; recovering it needs a
    // primitive-args-at-callsite gate or a `purity: pure` hint.)
    let src = r#"
    const TA = { FOO: "bar", BAZ: "qux" };
    const Me = (n) => TA[n];
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("TA"));
    assert_eq!(
        graph.function_rules("Me"),
        Some(vec![PurityRule::ToPropertyKeyCoercion])
    );
}

#[test]
fn plain_data_computed_read_with_primitive_key_is_pure() {
    // Positive direction for the ToPropertyKey gate: statically
    // primitive keys (literals, well-known symbols) keep the
    // PlainData computed read pure.
    let src = r#"
    const TA = { FOO: "bar", BAZ: "qux" };
    const readFoo = () => TA["FOO"];
    const readZero = () => TA[0];
"#;
    let graph = TestGraph::new(src);
    assert!(graph.is_plain_data("TA"));
    assert_eq!(graph.function_rules("readFoo"), Some(vec![]));
    assert_eq!(graph.function_rules("readZero"), Some(vec![]));
}

#[test]
fn fn_purity_deep_pure_chain_propagates_in_one_pass() {
    // `a → b → c → d → e`: a long chain of chunk-local calls,
    // each function pure on its own. SCC bottom-up classifies
    // `e` first (no callees), then `d`, ..., then `a` — each
    // function classified once.
    let src = r#"
        function e() { return 0; }
        function d() { return e(); }
        function c() { return d(); }
        function b() { return c(); }
        function a() { return b(); }
    "#;
    let graph = TestGraph::new(src);
    for name in ["a", "b", "c", "d", "e"] {
        assert_eq!(
            graph.function_rules(name),
            Some(vec![]),
            "expected {name} to classify Pure"
        );
    }
}

#[test]
fn fn_purity_deep_chain_propagates_impurity_to_root() {
    // Same shape but `e` writes `globalThis`. SCC processes
    // `e` first → Impure; the worklist propagates Impure up
    // the chain.
    let src = r#"
        function e() { globalThis.touched = true; return 0; }
        function d() { return e(); }
        function c() { return d(); }
        function b() { return c(); }
        function a() { return b(); }
    "#;
    let graph = TestGraph::new(src);
    assert_eq!(
        graph.function_rules("e"),
        Some(vec![PurityRule::AssignOrUpdate])
    );
    for name in ["a", "b", "c", "d"] {
        assert_eq!(
            graph.function_rules(name),
            Some(vec![PurityRule::ImpureFunctionCall]),
            "expected {name} to inherit Impure from `e`"
        );
    }
}

#[test]
fn fn_purity_mutual_recursion_with_external_impure_callee() {
    // Mutual recursion `a <-> b` (one SCC) + `a` also calls
    // `c` (separate SCC, Impure). `c` is processed first
    // (sink); `c` Impure. SCC {a, b}: optimistic Pure init,
    // worklist sees `a` calls `c` (Impure) → `a` becomes
    // Impure → `b` (which calls `a`) gets pushed to worklist
    // → `b` becomes Impure.
    let src = r#"
        function c() { globalThis.touched = true; return 0; }
        function a(n) { return n === 0 ? c() : b(n - 1); }
        function b(n) { return n === 0 ? 0 : a(n - 1); }
    "#;
    let graph = TestGraph::new(src);
    assert_eq!(
        graph.function_rules("c"),
        Some(vec![PurityRule::AssignOrUpdate])
    );
    assert_eq!(
        graph.function_rules("a"),
        Some(vec![
            PurityRule::ImpureFunctionCall,
            PurityRule::ImpureFunctionCall,
            PurityRule::CoercingOperator
        ])
    );
    assert_eq!(
        graph.function_rules("b"),
        Some(vec![
            PurityRule::ImpureFunctionCall,
            PurityRule::CoercingOperator
        ])
    );
}

#[test]
fn declared_pure_hint_applies_inside_chunk_function_bodies() {
    // `io` is impure by its body. `value` is pure only when chunk analysis
    // honors the hint on `io` while classifying `read`'s body, not just at
    // the top-level statement.
    let module = parse(
        r#"
    function io() { globalThis.touched = true; }
    const read = () => io();
    const value = read();
"#,
    );
    let value_rules = |declared_pure: BTreeSet<String>| {
        analyze_chunk(
            &module,
            &AnalysisInputs {
                assumptions: AnalysisAssumptions {
                    declared_pure,
                    ..AnalysisAssumptions::default()
                },
                ..AnalysisInputs::default()
            },
            None,
        )
        .facts
        .iter()
        .find(|f| f.declared.contains(&test_id("value")))
        .expect("value fact missing")
        .purity
        .clone()
    };
    assert!(rules_of(&value_rules(BTreeSet::from(["io".to_string()]))).is_empty());
    assert_eq!(
        rules_of(&value_rules(BTreeSet::new())),
        [PurityRule::ImpureFunctionCall]
    );
}

#[test]
fn body_shadowed_chunk_function_call_is_not_pure() {
    // SOUNDNESS: inside `outer`, the param `helper` shadows the
    // chunk-top pure function of the same name — the called value
    // is whatever the caller passed.
    let src = r#"
    function helper() { return 1; }
    function outer(helper) { return helper(); }
    function control() { return helper(); }
"#;
    let graph = TestGraph::new(src);
    assert_eq!(
        graph.function_rules("outer"),
        Some(vec![PurityRule::UnknownCall])
    );
    assert_eq!(graph.function_rules("control"), Some(vec![]));
}

#[test]
fn body_shadowed_whitelist_receiver_is_not_pure() {
    // SOUNDNESS: a param named `Math` shadows the global inside
    // the body; `Math.PI` there reads a user object (getter risk).
    let src = r#"
    function f(Math) { return Math.PI; }
    function g() { return Math.PI; }
    function h(Array) { return Array.isArray([]); }
"#;
    let graph = TestGraph::new(src);
    assert_eq!(
        graph.function_rules("f"),
        Some(vec![PurityRule::UnknownMember])
    );
    assert_eq!(graph.function_rules("g"), Some(vec![]));
    assert_eq!(
        graph.function_rules("h"),
        Some(vec![PurityRule::UnknownCall])
    );
}

#[test]
fn body_shadowed_builtin_new_is_not_pure() {
    // SOUNDNESS: a param named `Map` shadows the builtin; `new
    // Map()` constructs the caller-supplied class.
    let src = r#"
    function f(Map) { return new Map(); }
    function g() { return new Map(); }
"#;
    let graph = TestGraph::new(src);
    assert_eq!(
        graph.function_rules("f"),
        Some(vec![PurityRule::UnknownNew])
    );
    assert_eq!(graph.function_rules("g"), Some(vec![]));
}

#[test]
fn body_shadowed_declared_pure_binding_is_not_pure() {
    // SOUNDNESS: `purity: pure` is a trust contract on the
    // chunk-top binding `dp`; a param of the same name is a
    // different value the author never vouched for.
    let src = r#"
    function f(dp) { return dp(); }
    function g() { return dp(); }
"#;
    let graph = TestGraph::with_declared_pure(src, BTreeSet::from(["dp".to_string()]));
    assert_eq!(
        graph.function_rules("f"),
        Some(vec![PurityRule::UnknownCall])
    );
    assert_eq!(graph.function_rules("g"), Some(vec![]));
}

#[test]
fn body_shadowed_pure_member_binding_is_not_pure() {
    // SOUNDNESS: same for `pure_members` — `b.forwardRef(...)` on
    // a param `b` is not the annotated vendor namespace.
    let src = r#"
    import * as b from "vendor";
    function f(b) { return b.forwardRef(1); }
    function g() { return b.forwardRef(1); }
"#;
    let module = parse(src);
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let declared_pure_members: BTreeMap<String, BTreeSet<String>> =
        BTreeMap::from([("b".to_string(), BTreeSet::from(["forwardRef".to_string()]))]);
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &declared_pure_members,
        &BTreeMap::new(),
        &BTreeSet::new(),
        &BTreeSet::new(),
        &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
    );
    assert_eq!(
        graph.function_purity("f").map(|p| rules_of(p)),
        Some(vec![PurityRule::UnknownCall])
    );
    assert_eq!(
        graph.function_purity("g").map(|p| rules_of(p)),
        Some(vec![])
    );
}

#[test]
fn enum_escape_exemption_checks_only_the_named_unshadowed_candidate() {
    for argument in ["X || (X = {})", "X || (X || {})", "(X = {})"] {
        let source =
            format!("var X = {{}}, Y = {{}}, Z = {{}}; ((p) => (p.A = 1, p))({argument});");
        let graph = TestGraph::new(&source);
        for binding in ["X", "Y", "Z"] {
            assert!(graph.is_plain_data(binding), "{binding}: {source}");
        }
    }
    // Matching the outer candidate is insufficient: the full recognizer must
    // reject a different binding nested on the right, and scan both escapes.
    let mixed = "var X = {}, Y = {}; ((p) => (p.A = 1, p))(X || (Y || {}));";
    let graph = TestGraph::new(mixed);
    assert!(!graph.is_plain_data("X"));
    assert!(!graph.is_plain_data("Y"));
    // The callee must still be a vetted IIFE, not an arbitrary escaping call.
    assert!(!is_plain_data("var X = {}; unknown(X || (X = {}));", "X"));
    // Local parameter writes must not disqualify the outer plain-data binding.
    assert!(is_plain_data(
        "var X = {}; function f(X) { ((p) => (p.A = 1, p))(X || (X = {})); }",
        "X",
    ));
}
