use std::collections::{BTreeMap, BTreeSet};

use super::*;
use crate::facts::{compute_shadowed_globals, top_level_item_views};
use crate::*;
use swc_common::{FileName, SyntaxContext, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

fn test_id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

fn analyze_facts(module: &Module) -> Vec<StatementFacts> {
    analyze_chunk(module, &AnalysisHints::default(), None, |_| None).facts
}

fn parse(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("test.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Es(Default::default()),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer).parse_module().unwrap()
}

// --- ChunkCodeGraph: function-body purity inference --------------------

/// Build a `ChunkCodeGraph` for `src` and return whether the
/// named function is classified as Pure. `None` means the
/// function isn't tracked in the chunk's purity graph (only
/// `const`-bound function/arrow initializers are cached).
/// Tests the full pipeline: chunk parsing → function
/// collection → fixed-point.
fn fn_purity(src: &str, name: &str) -> Option<bool> {
    let module = parse(src);
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    graph.function_purity(name).map(|p| p.is_pure())
}

#[test]
fn fn_purity_pure_hof_wrapper() {
    // Body returns a fresh object literal whose values are a
    // bound parameter — no observable side effect.
    assert_eq!(
        fn_purity(
            r#"function wrap(f) { return { kind: "wrapped", impl: f }; }"#,
            "wrap"
        ),
        Some(true)
    );
}

#[test]
fn fn_purity_impure_globalthis_write() {
    // Assignment to a member of `globalThis` is unambiguously
    // impure regardless of what's on the RHS.
    assert_eq!(
        fn_purity("function tag(x) { globalThis.tag = x; }", "tag"),
        Some(false)
    );
}

#[test]
fn fn_purity_unknown_when_calling_console_log() {
    // `console.log(...)` is a member-call on a non-whitelisted
    // receiver — Unknown. Caller inherits.
    assert_eq!(
        fn_purity(
            r#"function logged(x) { console.log("init", x); return x; }"#,
            "logged"
        ),
        Some(false)
    );
}

#[test]
fn fn_purity_propagates_transitive_impurity() {
    // `caller` only calls `tainted`. `tainted` writes
    // `globalThis.touched`, so it's Impure. Fixed-point
    // propagates: `caller` becomes Impure on iteration 2.
    let src = r#"
    function tainted() { globalThis.touched = true; return 1; }
    function caller() { return tainted(); }
"#;
    assert_eq!(fn_purity(src, "tainted"), Some(false));
    assert_eq!(fn_purity(src, "caller"), Some(false));
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
    assert_eq!(fn_purity(src, "even"), Some(true));
    assert_eq!(fn_purity(src, "odd"), Some(true));
}

#[test]
fn fn_purity_param_arithmetic_is_not_pure() {
    // SOUNDNESS: `n - 1` runs ToNumeric on `n`; a caller can pass
    // an object whose `valueOf` fires arbitrary code, so a body
    // coercing an opaque param is not pure-callable.
    assert_eq!(
        fn_purity("function dec(n) { return n - 1; }", "dec"),
        Some(false)
    );
}

#[test]
fn fn_purity_arrow_const_init() {
    // `const f = (x) => …` — chunk-top function in a VarDecl
    // initializer. Concise-arrow body classifies the single
    // return expression.
    assert_eq!(
        fn_purity("const wrap = (x) => ({ val: x });", "wrap"),
        Some(true)
    );
}

#[test]
fn fn_purity_call_inherits_chunk_local_function_purity() {
    // `f()` where `f` is a chunk-top function in the cache
    // resolves through `ChunkCodeGraph::function_purity`. With
    // `f` body Pure, the call is Pure.
    let module = parse("function f() { return 42; } const x = f();");
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    let var = match body[1].as_module_item() {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected VarDecl, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init");
    assert!(
        (classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph))
            .is_pure()
    );
}

#[test]
fn new_map_fresh_spread_registry_entries_with_plain_data_members_is_pure() {
    let src = r#"
    const m = {
        systemToolWebSearchId: "SYS_FN16",
        dataTypeUrlId: "SYS_D10",
    };
    const includeUrl = true;
    const registry = new Map([
        ...[[m.systemToolWebSearchId, () => io()]],
        ...(includeUrl ? [[m.dataTypeUrlId, function () { globalThis.touched = true; }]] : []),
    ]);
"#;
    let module = parse(src);
    let facts = analyze_facts(&module);
    let registry_fact = facts
        .iter()
        .find(|f| f.declared.contains(&test_id("registry")))
        .expect("registry fact missing");
    assert!(
        registry_fact.purity.is_pure(),
        "fresh-spread registry literal should classify pure: {:?}",
        registry_fact.purity
    );
}

#[test]
fn new_map_fresh_spread_registry_does_not_bless_unknown_members() {
    let src = r#"
    const registry = new Map([
        ...[[m.systemToolWebSearchId, () => 1]],
    ]);
"#;
    let module = parse(src);
    let facts = analyze_facts(&module);
    let registry_fact = facts
        .iter()
        .find(|f| f.declared.contains(&test_id("registry")))
        .expect("registry fact missing");
    assert!(
        !registry_fact.purity.is_pure(),
        "unknown member read must still block: {:?}",
        registry_fact.purity
    );
}

#[test]
fn fn_purity_let_var_bound_arrows_are_not_cached() {
    // `let` and `var` bindings are reassignable. Caching their
    // body's purity would be unsound: a later `f = …` could
    // replace the value with something impure between graph
    // construction and the call site. Restrict graph entries
    // to `const`-bound function/arrow initializers.
    assert_eq!(
        fn_purity("let f = () => 1;", "f"),
        None,
        "`let`-bound arrow must not be in the function-purity graph"
    );
    assert_eq!(
        fn_purity("var f = function () { return 1; };", "f"),
        None,
        "`var`-bound function expr must not be in the function-purity graph"
    );
    // Sanity: `const` still works.
    assert_eq!(fn_purity("const f = () => 1;", "f"), Some(true));
}

#[test]
fn fn_purity_throw_makes_function_impure_even_with_pure_arg() {
    // `throw e` alters control flow observably regardless of
    // whether `e` itself is pure. A function that always
    // throws must not classify as Pure.
    assert_eq!(
        fn_purity(r#"function f() { throw "boom"; }"#, "f"),
        Some(false)
    );
    // Conditional throw is still Impure (we don't reason
    // about reachability — soundness-first).
    assert_eq!(
        fn_purity(r#"function f(x) { if (x) throw "boom"; return x; }"#, "f"),
        Some(false)
    );
}

#[test]
fn fn_purity_debugger_makes_function_impure() {
    // `debugger` pauses execution observably to a host
    // attached to the process — not Pure.
    assert_eq!(
        fn_purity("function f() { debugger; return 1; }", "f"),
        Some(false)
    );
}

// --- Recursive purity via PlainData chunk-local bindings ---------------

/// Build a `ChunkCodeGraph` for `src` and ask whether `name` is
/// tracked as a plain-data binding. `false` covers both "not
/// tracked" and "tracked as Function" — only `PlainData` returns
/// `true`. Used to pin which chunk-top consts the analyzer
/// admits as accessor-free data shapes.
fn is_plain_data(src: &str, name: &str) -> bool {
    let module = parse(src);
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    graph.is_plain_data(name)
}

#[test]
fn plain_data_const_object_literal_is_tracked() {
    // A vanilla data shape: KeyValue with non-computed keys,
    // plain values, no spreads/accessors/methods. Reads on it
    // can fire no user code; the analyzer can short-circuit
    // member access purity on this receiver.
    assert!(is_plain_data(r#"const TA = { FOO: "bar", BAZ: 1 };"#, "TA"));
}

#[test]
fn plain_data_const_array_literal_is_tracked() {
    assert!(is_plain_data("const TA = [1, 2, 3];", "TA"));
}

#[test]
fn plain_data_let_object_literal_with_no_writes_is_tracked() {
    // `let TA = {…}` is admissible even though re-assignment
    // is syntactically allowed: the chunk-wide write scan
    // confirms no `TA = rhs` assigns exist anywhere, so the
    // post-init value is invariant just like a `const`.
    assert!(is_plain_data("let TA = { a: 1 };", "TA"));
    assert!(is_plain_data("let TA = [1, 2, 3];", "TA"));
}

#[test]
fn plain_data_var_object_literal_is_tracked() {
    // `var X = { … }` at chunk top admits as PlainData on the
    // same terms as `let`: every init must be plain-literal,
    // and no chunk-wide member writes / hostile builtin calls /
    // non-plain ident reassigns. Hoisting: pre-init reads see
    // `undefined` and `undefined.k` throws a spec-mandated
    // TypeError — engine-emitted, not user-defined, so the
    // read-purity claim still holds.
    assert!(is_plain_data("var TA = { a: 1 };", "TA"));
    assert!(is_plain_data("var TA = [1, 2, 3];", "TA"));
}

#[test]
fn plain_data_var_multi_decl_all_plain_is_tracked() {
    // Multiple chunk-top `var X = init` redeclarations are
    // legal; every init must independently pass the plain-
    // literal shape rule. Both inits are plain-objects → X
    // admits as PlainData.
    let src = r#"
    var X = { a: 1 };
    var X = { b: 2 };
"#;
    assert!(is_plain_data(src, "X"));
}

#[test]
fn plain_data_var_multi_decl_with_one_non_plain_init_is_not_tracked() {
    // First decl init is plain, second is `io()` → the second
    // init's runtime value could carry accessor properties, so
    // reads on X after the second decl could fire user code.
    // Disqualify even though the first decl is fine.
    let src = r#"
    var X = { a: 1 };
    var X = io();
"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_var_member_write_disqualifies() {
    // Same write-scan rule as let/const — `X.k = v` in any
    // chunk body disqualifies.
    assert!(!is_plain_data(
        "var X = { a: 1 }; function mut() { X.b = 2; }",
        "X"
    ));
}

#[test]
fn plain_data_var_non_plain_ident_assign_disqualifies() {
    // `X = nonPlain` ident assign on a candidate disqualifies
    // (existing PlainDataWriteScanner rule applies to var
    // candidates unchanged).
    assert!(!is_plain_data(
        "var X = { a: 1 }; function bad() { X = someFn(); }",
        "X"
    ));
}

#[test]
fn plain_data_var_uninitialized_decl_alone_is_not_tracked() {
    // `var X;` with no initializer contributes nothing to the
    // candidate set. Without a chunk-top plain init, X has no
    // admission evidence, so reading X.k could see whatever
    // an external assignment installed.
    assert!(!is_plain_data("var X;", "X"));
}

#[test]
fn plain_data_var_uninitialized_then_initialized_is_tracked() {
    // `var X;` followed by `var X = { … }` (or `X = { … }`)
    // contributes the plain init for the binding. The first
    // no-init decl is hoisting noise; the second declares the
    // shape. Admits as PlainData.
    let src = r#"
    var X;
    var X = { a: 1 };
"#;
    assert!(is_plain_data(src, "X"));
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
    assert!(is_plain_data(src, "mr"));
    assert_eq!(fn_purity(src, "isEmulatorEnv"), Some(true));
}

// --- TS-enum IIFE PlainData admission ---------------------------------

#[test]
fn plain_data_ts_enum_iife_arrow_comma_body_is_tracked() {
    // The minimal arrow-comma-body shape with shadow-named param.
    // The scanner's new param-scope tracking is what makes this
    // work — `p.A = "a"` looks like a member write on `p`, but
    // `p` here is the inner IIFE parameter shadowing the outer
    // `X`, so the scanner doesn't disqualify the outer X.
    let src = r#"var X = ((p) => (p.A = "a", p.B = "b", p))({});"#;
    assert!(is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_canonical_self_assign_short_circuit_is_tracked() {
    // The canonical TypeScript emit form: the IIFE arg uses the
    // self-assigning short-circuit `X || (X = {})` to ensure the
    // arg is a plain object even on first reach. Param is named
    // the same as the binding (shadow case).
    let src = r#"var X = ((X) => (X["A"] = "a", X["B"] = "b", X))(X || (X = {}));"#;
    assert!(is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_or_plain_arg_is_tracked() {
    // The shorter `X || {}` arg form (esbuild's TS lowering for
    // const-enums and simple string enums).
    let src =
        r#"var ColorName = ((p) => (p["Red"] = "red", p["Green"] = "green", p))(ColorName || {});"#;
    assert!(is_plain_data(src, "ColorName"));
}

#[test]
fn plain_data_ts_enum_iife_function_expression_body_is_tracked() {
    let src = r#"var Color = (function (p) { p.RED = "red"; p.GREEN = "green"; return p; })(Color || {});"#;
    assert!(is_plain_data(src, "Color"));
}

#[test]
fn plain_data_ts_enum_iife_self_binding_short_circuit_must_match_decl() {
    // `Other || {}` could pass an arbitrary existing object into
    // the IIFE. The enum-init rule is only for the binding being
    // initialized, whose writes remain local to that binding.
    let src = r#"var X = ((p) => (p.A = "a", p))(Other || {});"#;
    assert!(!is_plain_data(src, "X"));
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
    assert!(is_plain_data(src, "Color"));
    assert_eq!(fn_purity(src, "isRed"), Some(true));
}

#[test]
fn plain_data_ts_enum_iife_degenerate_return_param_is_tracked() {
    // Edge case: arrow body is just the param (no mutations).
    // Equivalent to `var X = X || {}` after evaluation.
    let src = r#"var X = ((p) => p)({});"#;
    assert!(is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_non_iife_call_is_not_tracked() {
    // Not an inline arrow callable — calling a chunk-local
    // function whose body is unknown to the IIFE check.
    let src = r#"
    function makeIt(p) { return p; }
    var X = makeIt({ a: 1 });
"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_non_literal_arg_is_not_tracked() {
    // IIFE arg isn't a plain object or `X || plain` — could be
    // anything at runtime. Reject.
    let src = r#"var X = ((p) => (p.A = "a", p))(someFn());"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_non_primitive_rhs_is_not_tracked() {
    // Property write RHS is a call, not a primitive literal. The
    // resulting object would still be plain (writes are data
    // descriptors regardless of RHS shape), but the conservative
    // rule rejects to keep the soundness story uniform with
    // `is_plain_data_prop`.
    let src = r#"var X = ((p) => (p.A = io(), p))({});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_call_in_body_is_not_tracked() {
    let src = r#"var X = ((p) => (p.A = "a", observe(p), p))(X || {});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_global_or_other_object_write_is_not_tracked() {
    assert!(!is_plain_data(
        r#"var X = ((p) => (globalThis.A = "a", p))(X || {});"#,
        "X"
    ));
    assert!(!is_plain_data(
        r#"var X = ((p) => (other.A = "a", p))(X || {});"#,
        "X"
    ));
}

#[test]
fn plain_data_ts_enum_iife_computed_unsafe_key_is_not_tracked() {
    let src = r#"var X = ((p) => (p[key] = "a", p))(X || {});"#;
    assert!(!is_plain_data(src, "X"));
    let src = r#"var X = ((p) => (p["__proto__"] = "a", p))(X || {});"#;
    assert!(!is_plain_data(src, "X"));
    let src = r#"var X = ((p) => (p.__proto__ = "a", p))(X || {});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_param_escape_is_not_tracked() {
    let src = r#"var X = ((p) => (leaked = p, p.A = "a", p))(X || {});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_body_does_not_return_param_is_not_tracked() {
    // Body returns something other than the param — could be a
    // different object (an external reference, the result of a
    // call, etc.). Without verifying the returned value is
    // plain, reject.
    let src = r#"var X = ((p) => (p.A = "a", other))({});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_define_property_inside_body_is_not_admitted_directly() {
    // The body has a write that ISN'T `p.K = primLit` — calls
    // `Object.defineProperty` instead. The IIFE-body check
    // rejects this shape (only the comma-expression of
    // property writes is admitted).
    let src = r#"var X = ((p) => (Object.defineProperty(p, "A", { get: () => io() }), p))({});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_with_outer_member_write_disqualifies() {
    // Even when admitted as a TS-enum-IIFE candidate, a
    // chunk-wide member write `X.K = …` outside the IIFE
    // disqualifies — the scanner's standard write check
    // applies. The param-scope tracking is narrow: it only
    // exempts writes INSIDE function/arrow bodies whose
    // params shadow the candidate name.
    let src = r#"
    var X = ((p) => (p.A = "a", p))({});
    function mut() { X.B = "b"; }
"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_distinct_param_name_is_tracked() {
    // Param name differs from binding — no shadowing concern,
    // just exercises the admission rule cleanly.
    let src = r#"var Color = ((n) => (n.RED = "red", n))(Color || {});"#;
    assert!(is_plain_data(src, "Color"));
}

#[test]
fn plain_data_ts_enum_iife_computed_numeric_key_is_tracked() {
    // Numeric literal as computed key is admitted as a member
    // access on a fresh data property.
    let src = r#"var X = ((p) => (p[0] = "zero", p[1] = "one", p))({});"#;
    assert!(is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_no_param_function_is_not_tracked() {
    // No parameter — body has no `p` to mutate; reject.
    let src = r#"var X = (() => ({ a: 1 }))();"#;
    // Note: this could be admitted as "returns plain object
    // literal" in a future extension, but the current rule
    // requires the single-param shape and rejects this.
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_numeric_reverse_mapping_is_tracked() {
    // TypeScript's numeric-enum emit produces both a forward
    // map and a reverse map via the nested-assignment shape:
    //
    //     n[(n.A = 0)] = "A"
    //
    // The inner `n.A = 0` writes 0 as a data property under
    // key "A", then evaluates to 0, which the outer assignment
    // uses as the computed key for the reverse map:
    // `n[0] = "A"`. Both writes are data-property only — sound.
    let src = r#"var E = ((n) => (n[(n.A = 0)] = "A", n))(E || {});"#;
    assert!(is_plain_data(src, "E"));
}

#[test]
fn plain_data_ts_enum_iife_numeric_reverse_mapping_string_key_form_is_tracked() {
    // The bracket-key inner form `n[(n["A"] = 0)] = "A"`. Same
    // soundness story, different syntactic shape for the inner
    // forward write.
    let src = r#"var E = ((n) => (n[(n["A"] = 0)] = "A", n))(E || {});"#;
    assert!(is_plain_data(src, "E"));
}

#[test]
fn plain_data_ts_enum_iife_multiple_numeric_keys_is_tracked() {
    // Multi-key numeric-reverse-mapping IIFE — the canonical
    // Vite/TS emit for `enum E { A, B, C }`. Pins that the
    // recursive admission scales beyond a single key.
    let src = r#"var E = ((n) => ((n[(n.A = 0)] = "A"), (n[(n.B = 1)] = "B"), (n[(n.C = 2)] = "C"), n))(E || {});"#;
    assert!(is_plain_data(src, "E"));
}

#[test]
fn plain_data_ts_enum_iife_mixed_string_and_numeric_keys_is_tracked() {
    // The same IIFE can mix forward-only writes (string enum
    // form) and reverse-mapped writes (numeric enum form) as
    // long as every step matches one of the admitted shapes.
    let src = r#"var E = ((n) => (n.a = "a", n[(n.B = 1)] = "B", n))(E || {});"#;
    assert!(is_plain_data(src, "E"));
}

#[test]
fn plain_data_ts_enum_iife_nested_key_non_param_object_is_not_tracked() {
    // The inner assignment must target the SAME param. A nested
    // write on a different object would put data on a different
    // object and use the result as the outer key — sound for
    // X's purity in isolation but not what the rule is meant
    // to admit, and the recursive check naturally rejects it.
    let src = r#"var X = ((n) => (n[(other.X = 0)] = "X", n))({});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_nested_key_non_primitive_rhs_is_not_tracked() {
    // The inner assignment's RHS must be primitive — same
    // stricter rule as the outer case.
    let src = r#"var X = ((n) => (n[(n.A = io())] = "A", n))({});"#;
    assert!(!is_plain_data(src, "X"));
}

#[test]
fn plain_data_ts_enum_iife_inner_param_write_doesnt_disqualify_outer_candidate() {
    // The scanner's param-scope tracking is the key feature
    // making the shadow case work. This test pins the
    // behavior directly: a chunk where the outer X is admitted
    // (via a regular plain-literal init) but a separate IIFE
    // shadows X as a param and writes to that param — the
    // outer X must NOT be disqualified by those inner writes.
    let src = r#"
    var X = { existing: 1 };
    (function (X) { X.A = "a"; })({});
"#;
    // Outer X has its own plain init AND is admitted; the
    // inner anonymous IIFE's writes target the param-bound
    // inner X, not the outer.
    assert!(is_plain_data(src, "X"));
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
    assert!(is_plain_data(src, "m"));
    assert_eq!(fn_purity(src, "readSystemToolId"), Some(true));
    assert_eq!(fn_purity(src, "readDataTypeUrlId"), Some(true));
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
    assert!(is_plain_data(src, "m"));
    assert_eq!(fn_purity(src, "readSystemToolId"), Some(true));
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
    assert!(is_plain_data(src, "X"));
    // A genuine chunk-top read stays pure.
    assert_eq!(fn_purity(src, "readReal"), Some(true));
    // The shadowed read must be impure.
    assert_eq!(fn_purity(src, "f"), Some(false));
}

#[test]
fn plain_data_object_keys_arg_on_param_shadowed_name_is_not_pure() {
    // Same soundness hole on the `Object.keys/values/entries(X)` arg path
    // (`is_pure_plain_data_arg_for`): when `X` is a function param, the
    // arg is the local value, not the plain-data const.
    let src = r#"
    const X = { a: 1 };
    function f(X) { return Object.keys(X); }
    const readReal = () => Object.keys(X);
"#;
    assert!(is_plain_data(src, "X"));
    assert_eq!(fn_purity(src, "readReal"), Some(true));
    assert_eq!(fn_purity(src, "f"), Some(false));
}

#[test]
fn plain_data_real_top_level_m_write_still_disqualifies_after_vite_helper() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    const __vite__mapDeps = (i, m = __vite__mapDeps, d = m.f || (m.f = [0, 1])) => d;
    function mutateRealM() { m.f = [2]; }
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    assert!(!is_plain_data(src, "m"));
    assert_eq!(fn_purity(src, "readSystemToolId"), Some(false));
}

#[test]
fn plain_data_unshadowed_default_param_write_disqualifies_top_level_candidate() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    const helper = (d = (m.f = [0, 1])) => d;
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    assert!(!is_plain_data(src, "m"));
    assert_eq!(fn_purity(src, "readSystemToolId"), Some(false));
}

#[test]
fn plain_data_object_define_property_on_real_top_level_m_still_disqualifies() {
    let src = r#"
    const m = { systemToolWebSearchId: "SYS_FN16" };
    Object.defineProperty(m, "systemToolWebSearchId", { get: () => io() });
    const readSystemToolId = () => m.systemToolWebSearchId;
"#;
    assert!(!is_plain_data(src, "m"));
    assert_eq!(fn_purity(src, "readSystemToolId"), Some(false));
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
    assert!(is_plain_data(src, "envConfig"));
    // `getEnv` is no longer inferred pure: `envConfig[k]` runs
    // ToPropertyKey on the opaque key `k` (user `toString` on an
    // object key) — see `plain_data_computed_read_with_opaque_key_is_not_pure`.
    assert_eq!(fn_purity(src, "getEnv"), Some(false));
}

#[test]
fn plain_data_let_with_non_literal_rhs_assign_disqualifies() {
    // A re-bind whose RHS is not a syntactic plain literal could
    // produce an accessor-bearing value at runtime — `someFn()`
    // might return `Object.defineProperty(...)`-installed
    // getters, `other` might be such a value already, `X || {}`
    // shortcircuits to either operand. Each of these must
    // disqualify.
    assert!(!is_plain_data(
        "let X = { a: 1 }; function reassign() { X = someFn(); }",
        "X"
    ));
    assert!(!is_plain_data(
        "let X = { a: 1 }; function reassign(other) { X = other; }",
        "X"
    ));
    assert!(!is_plain_data(
        "let X = { a: 1 }; function reassign() { X = X || {}; }",
        "X"
    ));
}

#[test]
fn plain_data_let_with_accessor_rhs_assign_disqualifies() {
    // Even though the binding is `let` and the RHS is a literal,
    // the literal carries a getter — assigning it gives X
    // accessor channels. Disqualify.
    assert!(!is_plain_data(
        "let X = { a: 1 }; function bad() { X = { get a() { return io(); } }; }",
        "X"
    ));
}

#[test]
fn plain_data_let_with_member_write_disqualifies() {
    // `let X = {…}; X.k = v` is a member write — installs a
    // property in place. The conservative rule rejects it (the
    // resulting object is still plain-data, but the chunk-wide
    // invariant is simpler if we forbid all member writes
    // uniformly across `const` and `let`).
    assert!(!is_plain_data(
        "let X = { a: 1 }; function mut() { X.b = 2; }",
        "X"
    ));
}

#[test]
fn plain_data_let_with_update_disqualifies() {
    // `X++` rewrites X to a number — definitely not a
    // plain-literal data shape. Reject.
    assert!(!is_plain_data(
        "let X = { a: 1 }; function bad() { X++; }",
        "X"
    ));
}

#[test]
fn plain_data_let_chain_collapses_env_config_walkthrough_shape() {
    // The full gaffer-private chain-of-hints shape, end to end:
    //
    //   let envConfig = { REACT_APP_ENV: …, FUNCTIONS_EMULATOR: false, … };
    //   const applySystemConfigOverrides = (n) => { envConfig = { ...envConfig, ...n }; };
    //   const getEnv = (n) => envConfig[n];
    //   const isEmulatorEnv = () =>
    //       (mr.FUNCTIONS_EMULATOR ? true : getEnv("REACT_APP_ENV") === "emulator");
    //   const getSystemConfig = () => envConfig;
    //
    // The let+mutator extension keeps `envConfig` PlainData and
    // static-prop readers pure. The opaque-key accessor `getEnv`
    // (and its transitive caller `isEmulatorEnv`) are NOT inferred
    // pure under the ToPropertyKey gate — `envConfig[n]` coerces an
    // opaque key — so those two still need `purity: pure` hints in
    // `runtime/environment/{env_config,config}.yaml` when the spec
    // author wants their call sites S-edge-free.
    let src = r#"
    const mr = { FUNCTIONS_EMULATOR: false };
    let envConfig = {
        REACT_APP_ENV: "production",
        REACT_APP_FIREBASE_API_KEY: "x",
    };
    const applySystemConfigOverrides = (n) => {
        envConfig = { ...envConfig, ...n };
    };
    const getEnv = (n) => envConfig[n];
    const isEmulatorEnv = () =>
        (mr.FUNCTIONS_EMULATOR ? true : getEnv("REACT_APP_ENV") === "emulator");
    const getSystemConfig = () => envConfig;
"#;
    assert!(is_plain_data(src, "envConfig"));
    assert!(is_plain_data(src, "mr"));
    assert_eq!(fn_purity(src, "getEnv"), Some(false));
    assert_eq!(fn_purity(src, "isEmulatorEnv"), Some(false));
    assert_eq!(fn_purity(src, "getSystemConfig"), Some(true));
    // `applySystemConfigOverrides` itself is impure (it writes
    // to envConfig); call sites still need to anchor it via
    // S-edges. Confirm the impurity is detected, so the
    // debundler doesn't accidentally classify the mutator pure.
    assert_eq!(fn_purity(src, "applySystemConfigOverrides"), Some(false));
}

#[test]
fn plain_data_const_with_accessor_property_is_not_tracked() {
    // A getter installed in the literal makes `X.a` fire user
    // code. Reject these initializers — even though the binding
    // itself is `const`, reads on it are not pure.
    assert!(!is_plain_data(
        "const X = { get a() { return io(); } };",
        "X"
    ));
    assert!(!is_plain_data("const X = { set a(v) {}, };", "X"));
    assert!(!is_plain_data("const X = { m() { return io(); } };", "X"));
}

#[test]
fn plain_data_const_with_spread_is_tracked() {
    // Object/array spread inside the literal init is admitted:
    // `CopyDataProperties` and array spread both write the
    // source's *values* via `CreateDataPropertyOrThrow`, which
    // produces data descriptors regardless of the source's
    // descriptor shape. The resulting receiver has only data
    // properties, so member reads on it fire no user code.
    // The spread itself fires source getters AT INIT — that
    // impurity belongs to the surrounding statement (classified
    // independently via `ObjectSpread`/`ArraySpread` rules) and
    // is orthogonal to whether subsequent reads on the binding
    // are pure.
    assert!(is_plain_data("const X = { ...other, a: 1 };", "X"));
    assert!(is_plain_data("const X = [1, ...other];", "X"));
}

#[test]
fn plain_data_const_with_computed_key_is_not_tracked() {
    // Computed keys evaluate at-init; the resulting property
    // name can technically be a Symbol that maps to an
    // accessor on the prototype chain. Conservatively reject.
    assert!(!is_plain_data("const X = { [k]: 1 };", "X"));
}

#[test]
fn plain_data_const_with_proto_key_is_not_tracked() {
    // `{__proto__: P}` in an object literal SETS the prototype
    // (ES262 §13.2.5.5). If P has accessor properties, reads on
    // X fire them. Reject the syntactic form. The bracketed
    // `{["__proto__"]: P}` does NOT set the prototype, but it's
    // a computed key (already rejected).
    assert!(!is_plain_data("const X = { __proto__: other, a: 1 };", "X"));
    assert!(!is_plain_data(
        r#"const X = { "__proto__": other, a: 1 };"#,
        "X"
    ));
}

#[test]
fn plain_data_const_function_init_is_not_tracked_as_plain_data() {
    // `const f = () => …` is tracked as `Function`, not
    // `PlainData`. We care about the call-purity of f, not
    // about reading `f.length` etc.
    assert!(!is_plain_data("const f = (x) => x;", "f"));
    assert!(!is_plain_data("const f = function() { return 1; };", "f"));
}

#[test]
fn plain_data_disqualified_by_member_write() {
    // Any code in the chunk that writes `X.k = ...` could in
    // theory go through `Object.defineProperty`-style channels
    // later. The conservative rule disqualifies the binding
    // outright — even direct data-property assignment is
    // rejected.
    assert!(!is_plain_data("const X = { a: 1 }; X.b = 2;", "X"));
    // Even when the write lives inside a function body, the
    // function might be called from anywhere — including before
    // the read site we're trying to classify. Reject.
    assert!(!is_plain_data(
        "const X = { a: 1 }; function mut() { X.a = 9; }",
        "X"
    ));
}

#[test]
fn plain_data_disqualified_by_define_property_call() {
    // `Object.defineProperty(X, ...)` is the canonical channel
    // for installing an accessor on a `const`-bound plain
    // object after init. Any call shape with X as the first arg
    // disqualifies.
    assert!(!is_plain_data(
        r#"const X = { a: 1 }; Object.defineProperty(X, "a", { get: () => io() });"#,
        "X"
    ));
    assert!(!is_plain_data(
        "const X = { a: 1 }; Object.defineProperties(X, descriptors);",
        "X"
    ));
    assert!(!is_plain_data(
        "const X = { a: 1 }; Object.setPrototypeOf(X, P);",
        "X"
    ));
    assert!(!is_plain_data(
        r#"const X = { a: 1 }; Reflect.defineProperty(X, "a", { get: () => io() });"#,
        "X"
    ));
    // Object.assign(target, ...) writes data props to target.
    // Conservatively rejected so the chunk-wide rule reads
    // "the binding cell is never written through, period."
    assert!(!is_plain_data(
        "const X = { a: 1 }; Object.assign(X, src);",
        "X"
    ));
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
    assert!(is_plain_data(src, "TA"));
    assert_eq!(fn_purity(src, "Me"), Some(false));
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
    assert!(is_plain_data(src, "TA"));
    assert_eq!(fn_purity(src, "readFoo"), Some(true));
    assert_eq!(fn_purity(src, "readZero"), Some(true));
}

#[test]
fn plain_data_read_with_static_property_is_pure() {
    // `mr.FUNCTIONS_EMULATOR` is the second leg of the
    // recursive-purity walkthrough: a chunk-local config
    // object accessed via a non-computed property name. With
    // `mr` as PlainData, the read is unconditionally pure (no
    // key sub-expression to validate).
    let src = r#"
    const mr = { FUNCTIONS_EMULATOR: false, FOO: 1 };
    const check = () => mr.FUNCTIONS_EMULATOR;
"#;
    assert_eq!(fn_purity(src, "check"), Some(true));
}

#[test]
fn plain_data_chain_collapses_size_33_walkthrough_shape() {
    // The full chain-of-hints case from
    // `(internal purity research notes)`: a config
    // table `TA`, an accessor `Me`, an env-derived predicate
    // `$i`, and a top-level binding `gF` whose init is an
    // object literal whose values are calls to `Me`.
    //
    // Under the ToPropertyKey gate, the opaque-key accessor
    // `Me = (n) => TA[n]` is NOT inferred pure (an object key
    // fires user `toString`), so the inference-only collapse of
    // this chain no longer happens — the spec author keeps the
    // `purity: pure` hint on `Me` (one hint instead of four:
    // hint-pure `Me` lets `$i` / `vR` / `Oge` infer pure).
    let src = r#"
    const TA = { ENV: "emulator", KEY: "x" };
    const mr = { FUNCTIONS_EMULATOR: false };
    const Me = (n) => TA[n];
    const $i = () => mr.FUNCTIONS_EMULATOR ? true : Me("ENV") === "emulator";
    const vR = (x) => Me(x);
    const Oge = () => vR("KEY");
"#;
    assert_eq!(fn_purity(src, "Me"), Some(false));
    assert_eq!(fn_purity(src, "$i"), Some(false));
    assert_eq!(fn_purity(src, "vR"), Some(false));
    assert_eq!(fn_purity(src, "Oge"), Some(false));
    // With a single `purity: pure` hint on `Me`, the rest of the
    // chain (and the `gF` owner statement) infers pure.
    let hinted: BTreeSet<String> = BTreeSet::from(["Me".to_string()]);
    let full = format!(
        r#"
    {src}
    const gF = {{ apiKey: Me("KEY"), authDomain: Me("ENV"), feat: $i() }};
"#
    );
    let module = parse(&full);
    let facts = analyze_chunk(
        &module,
        &AnalysisHints::from_declared_pure(&hinted),
        None,
        |_| None,
    )
    .facts;
    let gf_fact = facts
        .iter()
        .find(|f| f.declared.contains(&test_id("gF")))
        .expect("gF fact missing from chunk analysis");
    assert!(
        gf_fact.purity.is_pure(),
        "gF should classify pure with a single hint on Me, got {:?}",
        gf_fact.purity,
    );
}

#[test]
fn plain_data_computed_key_impurity_propagates() {
    // `TA[io()]` evaluates the key expression at-init. Even
    // though `TA` is PlainData, the key sub-expression must
    // itself be pure for the member access to classify pure.
    // Confirms the analyzer recurses through the computed key.
    let src = r#"
    const TA = { a: 1 };
    const v = TA[io()];
"#;
    let module = parse(src);
    let facts = analyze_facts(&module);
    let v_fact = facts
        .iter()
        .find(|f| f.declared.contains(&test_id("v")))
        .expect("v fact missing");
    assert!(
        !v_fact.purity.is_pure(),
        "TA[io()] computed-key impurity should bubble up, got {:?}",
        v_fact.purity,
    );
}

#[test]
fn plain_data_disqualified_binding_leaves_member_access_unknown() {
    // If a chunk-local `const TA` is disqualified (e.g. by a
    // member write somewhere in the chunk), member reads on it
    // fall back to `unknown_member` — preserving the
    // soundness-first behavior that previously required the
    // chain-of-hints workaround.
    let src = r#"
    const TA = { a: 1 };
    TA.b = 2;
    const Me = (n) => TA[n];
"#;
    assert!(!is_plain_data(src, "TA"));
    assert_eq!(fn_purity(src, "Me"), Some(false));
}

// --- Call-graph topology: deep chains, isolated nodes ------------------

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
    for name in ["a", "b", "c", "d", "e"] {
        assert_eq!(
            fn_purity(src, name),
            Some(true),
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
    for name in ["a", "b", "c", "d", "e"] {
        assert_eq!(
            fn_purity(src, name),
            Some(false),
            "expected {name} to inherit Impure from `e`"
        );
    }
}

#[test]
fn fn_purity_independent_functions_isolated_in_call_graph() {
    // No edges between `a` / `b` / `c`. Each is its own SCC;
    // classification of each is independent.
    let src = r#"
        function a() { globalThis.touched = true; }
        function b() { return 1; }
        function c() { return 2; }
    "#;
    assert_eq!(fn_purity(src, "a"), Some(false));
    assert_eq!(fn_purity(src, "b"), Some(true));
    assert_eq!(fn_purity(src, "c"), Some(true));
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
    assert_eq!(fn_purity(src, "c"), Some(false));
    assert_eq!(fn_purity(src, "a"), Some(false));
    assert_eq!(fn_purity(src, "b"), Some(false));
}

// --- Body-level shadowing of global tables / chunk graph / annotations --

/// `fn_purity` with an explicit declared-pure set.
fn fn_purity_with_declared_pure(src: &str, name: &str, declared: &[&str]) -> Option<bool> {
    let module = parse(src);
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let declared_pure: BTreeSet<String> = declared.iter().map(|s| (*s).to_string()).collect();
    let graph = ChunkCodeGraph::build(&body, &shadowed, &declared_pure);
    graph.function_purity(name).map(|p| p.is_pure())
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
    assert_eq!(fn_purity(src, "outer"), Some(false));
    assert_eq!(fn_purity(src, "control"), Some(true));
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
    assert_eq!(fn_purity(src, "f"), Some(false));
    assert_eq!(fn_purity(src, "g"), Some(true));
    assert_eq!(fn_purity(src, "h"), Some(false));
}

#[test]
fn body_shadowed_builtin_new_is_not_pure() {
    // SOUNDNESS: a param named `Map` shadows the builtin; `new
    // Map()` constructs the caller-supplied class.
    let src = r#"
    function f(Map) { return new Map(); }
    function g() { return new Map(); }
"#;
    assert_eq!(fn_purity(src, "f"), Some(false));
    assert_eq!(fn_purity(src, "g"), Some(true));
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
    assert_eq!(fn_purity_with_declared_pure(src, "f", &["dp"]), Some(false));
    assert_eq!(fn_purity_with_declared_pure(src, "g", &["dp"]), Some(true));
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
    );
    assert_eq!(graph.function_purity("f").map(|p| p.is_pure()), Some(false));
    assert_eq!(graph.function_purity("g").map(|p| p.is_pure()), Some(true));
}

// --- Parameter evaluation (defaults / destructuring) --------------------

#[test]
fn fn_purity_impure_param_default_makes_function_not_pure() {
    // Default-value expressions evaluate at call time, exactly
    // like body code.
    assert_eq!(
        fn_purity("const f = (x = (globalThis.boom = 1)) => 1;", "f"),
        Some(false)
    );
    assert_eq!(
        fn_purity("function g(x = io()) { return 1; }", "g"),
        Some(false)
    );
}

#[test]
fn fn_purity_pure_param_default_stays_pure() {
    assert_eq!(fn_purity("const f = (x = 1) => x;", "f"), Some(true));
    assert_eq!(fn_purity("const g = (x = []) => x;", "g"), Some(true));
}

#[test]
fn fn_purity_destructuring_param_makes_function_not_pure() {
    // Destructuring a parameter fires getters (object pattern) or
    // the iterator protocol (array pattern) on the argument.
    assert_eq!(fn_purity("const f = ({ a }) => a;", "f"), Some(false));
    assert_eq!(fn_purity("const g = ([x]) => x;", "g"), Some(false));
    assert_eq!(
        fn_purity("function h({ a } = {}) { return a; }", "h"),
        Some(false)
    );
}

#[test]
fn fn_purity_rest_param_stays_pure() {
    // A rest param builds a fresh array from the arguments list —
    // no user-code path.
    assert_eq!(fn_purity("const f = (...xs) => xs;", "f"), Some(true));
}

// --- Iteration statements / destructuring declarators in bodies ---------

#[test]
fn fn_purity_for_of_makes_function_not_pure() {
    // for-of fires the iterated value's `[Symbol.iterator]`.
    assert_eq!(
        fn_purity("function f(xs) { for (const x of xs) {} return 1; }", "f"),
        Some(false)
    );
}

#[test]
fn fn_purity_for_in_makes_function_not_pure() {
    // for-in enumeration fires proxy ownKeys/getOwnPropertyDescriptor
    // traps on the enumerated value.
    assert_eq!(
        fn_purity("function f(o) { for (const k in o) {} return 1; }", "f"),
        Some(false)
    );
}

#[test]
fn fn_purity_plain_loops_stay_pure() {
    // Plain `while` / C-style `for` introduce no protocol firing
    // beyond their (independently classified) sub-expressions.
    assert_eq!(
        fn_purity(
            "function f(flag) { while (flag) { flag = false; } return 1; }",
            "f"
        ),
        Some(false) // the assign is impure — control for the loop itself below
    );
    assert_eq!(
        fn_purity("function g(flag) { while (flag) {} return 1; }", "g"),
        Some(true)
    );
}

#[test]
fn fn_purity_destructuring_declarator_makes_function_not_pure() {
    // `const {a} = o` fires o's getters; `const [x] = o` fires the
    // iterator protocol.
    assert_eq!(
        fn_purity("function f(o) { const { a } = o; return a; }", "f"),
        Some(false)
    );
    assert_eq!(
        fn_purity("function g(o) { const [x] = o; return x; }", "g"),
        Some(false)
    );
    assert_eq!(
        fn_purity("function h(o) { const a = o; return a; }", "h"),
        Some(true)
    );
}

// --- PlainData escape analysis ------------------------------------------

#[test]
fn plain_data_alias_escape_disqualifies() {
    // SOUNDNESS: the write scan is name-based; an alias defeats it
    // (`Object.defineProperty(Y, …)` would install an accessor the
    // scan can't see). Escape itself disqualifies.
    assert!(!is_plain_data("const X = { a: 1 }; const Y = X;", "X"));
    assert!(!is_plain_data(
        r#"const X = { a: 1 }; const Y = X; Object.defineProperty(Y, "a", { get: () => io() });"#,
        "X"
    ));
}

#[test]
fn plain_data_call_arg_escape_disqualifies() {
    assert!(!is_plain_data("const X = { a: 1 }; f(X);", "X"));
    assert!(!is_plain_data("const X = { a: 1 }; new Thing(X);", "X"));
}

#[test]
fn plain_data_container_value_escape_disqualifies() {
    assert!(!is_plain_data("const X = { a: 1 }; const arr = [X];", "X"));
    assert!(!is_plain_data(
        "const X = { a: 1 }; const o = { x: X };",
        "X"
    ));
    assert!(!is_plain_data("const X = { a: 1 }; const o = { X };", "X"));
}

#[test]
fn plain_data_conditional_alias_escape_disqualifies() {
    // `flag ? X : null` evaluates to X itself — a captured alias.
    assert!(!is_plain_data(
        "const X = { a: 1 }; const Y = flag ? X : null;",
        "X"
    ));
    assert!(!is_plain_data(
        "const X = { a: 1 }; const Y = X || {};",
        "X"
    ));
}

#[test]
fn plain_data_non_capturing_reads_keep_admission() {
    // The short list of provably non-capturing positions must NOT
    // disqualify: member receiver, spread source, typeof/!/void,
    // Object.keys-style single arg, return / concise arrow body,
    // export specifier.
    assert!(is_plain_data("const X = { a: 1 }; const v = X.a;", "X"));
    assert!(is_plain_data(
        "const X = { a: 1 }; const Y = { ...X };",
        "X"
    ));
    assert!(is_plain_data("const X = [1]; const Y = [...X];", "X"));
    assert!(is_plain_data(
        "const X = { a: 1 }; const t = typeof X;",
        "X"
    ));
    assert!(is_plain_data("const X = { a: 1 }; const b = !X;", "X"));
    assert!(is_plain_data(
        "const X = { a: 1 }; const ks = Object.keys(X);",
        "X"
    ));
    assert!(is_plain_data(
        "const X = { a: 1 }; function f() { return X; }",
        "X"
    ));
    assert!(is_plain_data("const X = { a: 1 }; const f = () => X;", "X"));
    assert!(is_plain_data("const X = { a: 1 }; export { X };", "X"));
}

#[test]
fn plain_data_object_keys_escape_exemption_requires_unshadowed_object() {
    // With `Object` rebound at chunk top, `Object.keys(X)` calls a
    // user function — the arg is a real escape.
    assert!(!is_plain_data(
        "const userland = 1; const Object = userland; const X = { a: 1 }; const ks = Object.keys(X);",
        "X"
    ));
}
