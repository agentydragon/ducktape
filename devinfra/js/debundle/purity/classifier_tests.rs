use std::collections::{BTreeMap, BTreeSet};

use super::*;
use crate::analysis_inputs::{ProofDependencies, ValueInputRef};
use crate::facts::abstract_domain::heap::{
    ChunkHeap, plain_array_bindings, tracked_allocation_roots,
};
use crate::facts::{
    ChunkValueFlow, DynamicScopeFact, LocalClassConstructionProof, SetIterableProof,
    analyze_chunk_structural, compute_shadowed_globals, top_level_item_views,
};
use swc_common::{FileName, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

use raw_js_test_support::parse;

fn classify(src: &str) -> Purity {
    // Wrap the expression in a const so we can parse a module.
    let module = parse(&format!("const _ = {src};"));
    let var = match &module.body[0] {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected `const _ = ...;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(
        init,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeSet::new(),
        &ChunkCodeGraph::default(),
    )
    .project()
}

/// Parse `{prefix}\nconst _ = {expr_src};` and hand `classify` the chunk-top item
/// views, the shadowed-globals set of the whole module and the last const's
/// initializer.
fn classify_last_const(
    prefix: &str,
    expr_src: &str,
    classify: impl FnOnce(&[TopLevelItemView<'_>], &BTreeSet<&'static str>, &Expr) -> Purity,
) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify(&body, &shadowed, init)
}

/// Like [`classify_last_const`], classifying against the `ChunkCodeGraph` that
/// `build` makes from the module's chunk-top facts.
fn classify_with_graph(
    prefix: &str,
    expr_src: &str,
    build: impl FnOnce(&[TopLevelItemView<'_>], &BTreeSet<&'static str>) -> ChunkCodeGraph,
) -> Purity {
    classify_last_const(prefix, expr_src, |body, shadowed, init| {
        let graph = build(body, shadowed);
        classify_expr_purity(init, shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph).project()
    })
}

fn string_set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_string()).collect()
}

fn value_input_dependencies(provider: &str, input_index: usize) -> ProofDependencies {
    ProofDependencies {
        value_inputs: BTreeSet::from([ValueInputRef {
            defining_chunk: provider.to_string(),
            input_index,
        }]),
        ..Default::default()
    }
}

fn graph_with_conditioned_set_proof(
    module: &Module,
    dependencies: &ProofDependencies,
    declared_pure_new: &BTreeSet<String>,
) -> ChunkCodeGraph {
    let views = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&views);
    let flow = ChunkValueFlow::analyze_with_candidates(&views, &tracked_allocation_roots(&views));
    let plain_data = ChunkHeap::analyze(&views, &shadowed, &flow, None)
        .plain_data_bindings()
        .bindings;
    let plain_arrays = plain_array_bindings(&views, &shadowed);
    let dynamic_scope = DynamicScopeFact::analyze(&views);
    let set_shapes = crate::facts::SetIterableShapes::analyze(
        &views,
        &shadowed,
        &flow,
        &plain_data,
        dynamic_scope,
    );
    let class_shapes = crate::facts::LocalClassConstructionShapes::analyze(
        &views,
        &shadowed,
        &flow,
        dynamic_scope,
    );
    let prepared =
        crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index(
            &views,
            &flow,
            &crate::analysis_inputs::AnalysisInputs {
                native_integrity: Some(crate::analysis_inputs::NativeIntegrity {
                    consumed_value_inputs: dependencies.value_inputs.clone(),
                    ..Default::default()
                }),
                ..Default::default()
            },
            &plain_arrays,
            &class_shapes,
            set_shapes,
            &shadowed,
        );
    let set_proof = prepared.set_iterable_proof();
    ChunkCodeGraph::build_full_with_prepared_plain_data(
        &views,
        &shadowed,
        &BTreeSet::new(),
        declared_pure_new,
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        set_proof,
        &LocalClassConstructionProof::default(),
        &plain_data,
        &BTreeMap::new(),
        &BTreeMap::new(),
        &plain_arrays,
        &BTreeSet::new(),
        None,
        None,
    )
}

/// Run the classifier against `src` after computing the
/// chunk-top-level shadowed-globals set from a wrapping
/// module. Lets tests check the shadowing fallback.
fn classify_with_module(prefix: &str, expr_src: &str) -> Purity {
    classify_last_const(prefix, expr_src, |_, shadowed, init| {
        classify_expr_purity(
            init,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
        .project()
    })
}

/// Run the classifier against `src` with both shadowing and an
/// explicit declared-pure binding set.
fn classify_with_declared_pure(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
    let declared_pure = string_set(declared);
    classify_last_const(prefix, expr_src, |_, shadowed, init| {
        classify_expr_purity(
            init,
            shadowed,
            &BTreeSet::new(),
            &declared_pure,
            &ChunkCodeGraph::default(),
        )
        .project()
    })
}

fn classify_with_declared_pure_new(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_with_declared_pure_new(
            body,
            shadowed,
            &BTreeSet::new(),
            &string_set(declared),
        )
    })
}

fn classify_with_declared_pure_members(
    prefix: &str,
    expr_src: &str,
    binding: &str,
    props: &[&str],
) -> Purity {
    let declared_pure_members = BTreeMap::from([(binding.to_string(), string_set(props))]);
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_full(
            body,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &declared_pure_members,
            &BTreeMap::new(),
            &BTreeSet::new(),
        )
    })
}

/// Classify `expr_src` (appearing after `prefix` at chunk top) with
/// `fluent` as the author-asserted fluent root bindings.
fn classify_with_fluent_bindings(prefix: &str, expr_src: &str, fluent: &[&str]) -> Purity {
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_full(
            body,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &string_set(fluent),
        )
    })
}

#[test]
fn classify_pure_unary_and_binary() {
    assert!((classify("-1")).is_pure());
    assert!((classify("!FOO")).is_pure());
    assert!((classify("typeof FOO")).is_pure());
    assert!((classify("void FOO")).is_pure());
    assert!((classify("A && B")).is_pure());
    assert!((classify("A || B")).is_pure());
    assert!((classify("A ?? B")).is_pure());
    assert!((classify("A === B")).is_pure());
    assert!((classify("A !== B")).is_pure());
    assert!((classify("A ? B : C")).is_pure());
    // Coercing operators on statically-primitive operands stay
    // pure: the engine's ToPrimitive of a primitive runs no user
    // code. Nested coercing results are themselves primitive.
    assert!((classify("1 + 2")).is_pure());
    assert!((classify("'a' + 1")).is_pure());
    assert!((classify("1 + 2 + 3")).is_pure());
    assert!((classify("1e3 * 60 * 60")).is_pure());
    assert!((classify("-(1 + 2)")).is_pure());
    assert!((classify("~0")).is_pure());
    assert!((classify("(typeof A) + '!'")).is_pure());
    assert!((classify("(A === B) | 0")).is_pure());
}

#[test]
fn classify_coercing_operators_on_possibly_object_operands_are_not_pure() {
    // SOUNDNESS: ToPrimitive / ToNumber / ToString on an object
    // operand fires user `valueOf` / `toString` /
    // `[Symbol.toPrimitive]`. An opaque Ident can hold an object,
    // so coercing operators only classify pure when every coerced
    // operand is statically primitive-valued. (`A + 1` was
    // previously pinned pure — deliberate conservative shift.)
    assert!(!(classify("A + 1")).is_pure());
    assert!(!(classify("A - 1")).is_pure());
    assert!(!(classify("A * B")).is_pure());
    assert!(!(classify("A < 10")).is_pure());
    assert!(!(classify("A >= B")).is_pure());
    assert!(!(classify("A == null")).is_pure());
    assert!(!(classify("A != B")).is_pure());
    assert!(!(classify("A & 1")).is_pure());
    assert!(!(classify("A << 2")).is_pure());
    assert!(!(classify("+A")).is_pure());
    assert!(!(classify("-A")).is_pure());
    assert!(!(classify("~A")).is_pure());
    // The shadowable global `undefined` is an ordinary binding the
    // shadow pass doesn't track — deliberately NOT admitted as a
    // primitive operand.
    assert!(!(classify("A == undefined")).is_pure());
    assert!(!(classify("undefined + 1")).is_pure());
}

#[test]
fn classify_in_and_instanceof_are_not_pure() {
    // `in` fires the proxy `has` trap on its RHS; `instanceof`
    // fires `@@hasInstance` / reads `.prototype` on its RHS. The
    // interesting RHS is always an object, so no primitive-operand
    // gate can admit these.
    assert!(!(classify("'k' in o")).is_pure());
    assert!(!(classify("x instanceof C")).is_pure());
    assert!(!(classify("'k' in { k: 1 }")).is_pure());
}

#[test]
fn classify_assignment_and_update_are_impure() {
    assert!(!(classify("(x = 1)")).is_pure());
    assert!(!(classify("x++")).is_pure());
}

#[test]
fn classify_call_new_tagged_template_are_unknown() {
    assert!(!(classify("foo()")).is_pure());
    assert!(!(classify("new Foo()")).is_pure());
    assert!(!(classify("tag`hi ${x}`")).is_pure());
}

#[test]
fn classify_member_access_is_unknown() {
    assert!(!(classify("o.x")).is_pure());
    assert!(!(classify("o[k]")).is_pure());
    assert!(!(classify("o?.x")).is_pure());
}

#[test]
fn classify_object_literal_pure_when_props_pure() {
    assert!((classify("({ a: 1, b: 'x' })")).is_pure());
    // Computed keys run ToPropertyKey on the key VALUE — an opaque
    // Ident can hold an object whose `toString` fires. Pure only
    // for statically-primitive keys and well-known symbols.
    assert!((classify("({ ['k']: 1 })")).is_pure());
    assert!((classify("({ [1 + 2]: 1 })")).is_pure());
    assert!((classify("({ [Symbol.iterator]: 1 })")).is_pure());
    assert!(!(classify("({ [k]: 1 })")).is_pure());
    // Computed key with member access — getter could fire.
    assert!(!(classify("({ [k.x]: 1 })")).is_pure());
    // Spread of an arbitrary expr — iterator could fire.
    assert!(!(classify("({ ...other })")).is_pure());
    // Method definitions are pure (defining, not calling).
    assert!((classify("({ m() { return io(); } })")).is_pure());
}

#[test]
fn classify_array_literal_pure_when_elements_pure() {
    assert!((classify("[1, 2, 'x']")).is_pure());
    assert!((classify("[A, B]")).is_pure());
    assert!(!(classify("[1, foo()]")).is_pure());
    // Arbitrary iterable spread stays Unknown: it can call a
    // user-defined `[Symbol.iterator]`.
    assert!(!(classify("[...other]")).is_pure());
}

#[test]
fn classify_fresh_array_spread_sources_are_pure() {
    assert!((classify("[...[1, 2, 'x']]")).is_pure());
    assert!((classify("[...(flag ? ['a'] : [])]")).is_pure());
    assert!((classify("[...[() => io(), function () { globalThis.x = 1; }]]")).is_pure());
    assert!(!(classify("[...[io()]]")).is_pure());
    assert!(!(classify("[...(flag ? ['a'] : other)]")).is_pure());
}

#[test]
fn classify_callbacks_inside_literals_are_values_not_init_calls() {
    assert!(
        (classify("({ cb: () => io(), nested: [function () { console.log('x'); }] })")).is_pure()
    );
    assert!((classify("[() => io(), function () { globalThis.touched = true; }]")).is_pure());
}

#[test]
fn classify_function_and_arrow_are_pure() {
    assert!((classify("function () { return io(); }")).is_pure());
    assert!((classify("() => io()")).is_pure());
}

#[test]
fn classify_class_expr_pure_without_static_init() {
    assert!((classify("class { m() { return io(); } }")).is_pure());
    assert!((classify("class { static x = 1 }")).is_pure());
    assert!(!(classify("class { static x = io() }")).is_pure());
    assert!(!(classify("class { static {} }")).is_pure());
}

#[test]
fn classify_template_interpolation_requires_primitive_values() {
    // Interpolation runs ToString on the value — pure only when
    // the interpolated expression is statically primitive-valued.
    assert!((classify("`a${1 + 2}b${'x'}c`")).is_pure());
    assert!((classify("`a${`inner${1}`}b`")).is_pure());
    assert!((classify("`a${typeof A}b`")).is_pure());
    // An opaque Ident can hold an object whose `toString` fires.
    assert!(!(classify("`a${A}b`")).is_pure());
    assert!(!(classify("`a${foo()}`")).is_pure());
}

#[test]
fn classify_strict_equality_inherits_operand_impurity() {
    assert!(!(classify("foo() === 1")).is_pure());
    assert!(!(classify("1 !== foo()")).is_pure());
}

#[test]
fn classify_sequence_takes_worst() {
    assert!((classify("(A, B, C)")).is_pure());
    assert!(!(classify("(A, foo(), C)")).is_pure());
    assert!(!(classify("(A, x = 1, C)")).is_pure());
}

// --- Builtin table: intrinsic data property reads ------------------------

#[test]
fn intrinsic_data_property_reads_are_pure() {
    // Every data property in the table, primitive- or function-valued, is a
    // non-accessor own property: reading it runs no getter.
    for property in builtins::DATA_PROPERTIES {
        let src = format!("{}.{}", property.global, property.name);
        assert!(
            classify(&src).is_pure(),
            "expected static read `{src}` to classify Pure"
        );
    }
}

#[test]
fn unlisted_properties_fall_back_to_unknown() {
    // Same receivers, properties that aren't in the table:
    // could be a getter / a coercing call. Stays Unknown.
    assert!(!(classify("Math.unknownProp")).is_pure());
    assert!(!(classify("Number.unknownProp")).is_pure());
    assert!(!(classify("Symbol.unknownProp")).is_pure());
}

// --- Builtin table: calls -----------------------------------------------

#[test]
fn rows_admitting_any_operands_are_pure_regardless_of_arg() {
    // Type predicates, `Object.is` and `Boolean` neither coerce nor read
    // user props on the argument, so any Pure-classified args (an
    // identifier and an array literal here, two arguments to cover
    // `Object.is`) keep the call Pure.
    let mut checked = 0;
    for row in builtins::ROWS {
        if !row.invocation.contains(&Invocation::Call) || !admits_any_operands(row) {
            continue;
        }
        let src = match row.op.callee() {
            (global, Some(member)) => format!("{global}.{member}(x, [1, 2, 3])"),
            (global, None) => format!("{global}(x, [1, 2, 3])"),
        };
        assert!(
            classify(&src).is_pure(),
            "expected call `{src}` to classify Pure"
        );
        checked += 1;
    }
    assert!(checked > 0, "some row admits any operands");
}

#[test]
fn builtin_calls_unknown_arg_infects() {
    // An argument whose evaluation may itself fire user code
    // poisons the whole call: even though `Array.isArray` is
    // a pure operation, evaluating `io()` first is not.
    assert!(!(classify("Array.isArray(io())")).is_pure());
    assert!(!(classify("Number.isNaN(o.x)")).is_pure());
}

// --- Function-valued data properties: read-vs-call distinction ----------

#[test]
fn function_valued_property_calls_remain_unknown() {
    // Reading a function-valued data property is pure; calling it is
    // summarized only by its row. Every such function whose row does not
    // admit any operands (or that has no row) must stay Unknown on opaque
    // args. Some rows (`Object.{keys, values, entries, freeze, fromEntries}`)
    // admit a syntactically plain-data argument — pinned in the `object_*`
    // tests below; the opaque-Ident args used here fall outside that shape.
    for property in builtins::DATA_PROPERTIES {
        if property.value != builtins::PropertyValue::Function
            || builtins::lookup(property.global, Some(property.name), Invocation::Call)
                .is_some_and(admits_any_operands)
        {
            continue;
        }
        let src = format!("{}.{}(o, p)", property.global, property.name);
        assert!(
            !classify(&src).is_pure(),
            "expected function-ref CALL `{src}` to stay Unknown"
        );
    }
    // Representative shapes with literal extra args, pinning that
    // arg purity alone never admits the call.
    assert!(!(classify("Object.defineProperty(t, 'k', { value: 1 })")).is_pure());
    assert!(!(classify("Object.freeze(o)")).is_pure());
    assert!(!(classify("Object.values(o)")).is_pure());
    assert!(!(classify("Object.keys(o)")).is_pure());
}

#[test]
fn static_function_ref_object_shadowed_falls_back_to_unknown() {
    // If the chunk shadows `Object` (via a top-level decl OR an import
    // specifier per A8), the function-ref read must fall back to
    // Unknown — `Object.X` then resolves through the user-bound value.
    assert!(!(classify_with_module("const Object = userland;", "Object.defineProperty")).is_pure());
    assert!(
        !(classify_with_module(
            r#"import { Object } from "./userland.js";"#,
            "Object.freeze"
        ))
        .is_pure()
    );
}

#[test]
fn boolean_is_pure_on_any_value() {
    // ToBoolean fires no user code on any value: an identifier, a
    // primitive and an object literal all stay Pure.
    for arg in ["x", "0", "{}"] {
        let src = format!("Boolean({arg})");
        assert!(
            classify(&src).is_pure(),
            "expected global call `{src}` to classify Pure"
        );
    }
}

#[test]
fn to_string_rows_admit_only_result_primitive_operands() {
    // `ToString` completes without user code on any result-primitive
    // operand; other operands keep a coercion reason.
    for src in [
        "Symbol(`description`)",
        "Symbol(1 + 2)",
        "new URLSearchParams(`a=1`)",
    ] {
        assert!(classify(src).is_pure(), "expected `{src}` to classify Pure");
    }
    for src in ["Symbol(x)", "Symbol({})", "new URLSearchParams(q)"] {
        assert!(!classify(src).is_pure(), "expected `{src}` to stay unknown");
    }
}

#[test]
fn rows_without_assumed_identity_stay_unknown() {
    // These callees' identities need the census or an author input
    // (`native_operation_inputs`), which syntactic purity does not consult.
    for src in [
        "Symbol.for('key')",
        "Math.round(1.5)",
        "Object.getOwnPropertyNames({ a: 1 })",
        "new RegExp('a+')",
        "new FinalizationRegistry(() => {})",
        "Object.defineProperties({}, {})",
    ] {
        assert!(!classify(src).is_pure(), "expected `{src}` to stay unknown");
    }
}

#[test]
fn unsafe_global_callables_stay_unknown() {
    // `Number(x)` runs ToNumber, so a user `valueOf` can fire; `Symbol(x)`
    // is admitted only for primitive-literal args (the non-literal gate).
    for src in ["Number(x)", "Symbol(x)"] {
        assert!(
            !classify(src).is_pure(),
            "expected {src} to stay Unknown (would fire user code)"
        );
    }
}

#[test]
fn unsafe_static_calls_stay_unknown() {
    // One representative per way an unlisted call fires user code:
    // coercion (`Math.abs`), iteration (`Array.from`), `toJSON`
    // (`JSON.stringify`, a receiver outside the table). `Object.*` calls
    // are covered by `function_valued_property_calls_remain_unknown`.
    for src in ["Math.abs(x)", "Array.from(x)", "JSON.stringify(x)"] {
        assert!(
            !classify(src).is_pure(),
            "expected {src} to stay Unknown (would fire user code)"
        );
    }
}

// --- Builtin table: shadowing fallback ----------------------------------

#[test]
fn shadowed_receiver_disables_builtin_rows() {
    // A chunk-top-level binding for `Math` makes `Math.PI` no
    // longer reach the global; its rows must fall back to
    // Unknown.
    assert!(!(classify_with_module("const Math = userland;", "Math.PI")).is_pure());
    assert!(!(classify_with_module("function Math() {}", "Math.E")).is_pure());
    assert!(!(classify_with_module("const Array = X;", "Array.isArray(x)")).is_pure());
    assert!(!(classify_with_module("let Number = X;", "Number.isNaN(x)")).is_pure());
    assert!(!(classify_with_module("const Boolean = X;", "Boolean(x)")).is_pure());
}

#[test]
fn unshadowed_receiver_keeps_builtin_rows() {
    // A chunk that declares an unrelated binding leaves the
    // rows active — only same-named shadowing disables.
    assert!((classify_with_module("const other = userland;", "Math.PI")).is_pure());
}

#[test]
fn import_specifier_locals_shadow_builtin_rows() {
    // Import bindings are top-level lexical decls and shadow
    // the global the same way `const Math = …` does. The
    // classifier must reach the same Unknown fallback. (Soundness
    // matters: the imported value can be anything, so
    // `<imported>.<prop>` is a property read that may fire a
    // user-defined getter.)
    assert!(
        !(classify_with_module(r#"import { Math } from "./userland.js";"#, "Math.PI")).is_pure()
    );
    assert!(
        !(classify_with_module(r#"import Boolean from "./userland.js";"#, "Boolean(x)")).is_pure()
    );
    assert!(
        !(classify_with_module(
            r#"import * as Number from "./userland.js";"#,
            "Number.isNaN(x)"
        ))
        .is_pure()
    );
    assert!(
        !(classify_with_module(
            r#"import { something as Array } from "./userland.js";"#,
            "Array.isArray(x)"
        ))
        .is_pure()
    );
}

// --- Builtin table: shadow tracking covers every global ------------------

#[test]
fn builtin_constructors_without_args_are_pure() {
    for name in [
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Array",
        "TextDecoder",
        "TextEncoder",
        "URLSearchParams",
    ] {
        let src = format!("new {name}()");
        assert!(
            classify(&src).is_pure(),
            "expected `{src}` to classify Pure"
        );
    }
}

#[test]
fn url_search_params_string_literal_arg_is_pure() {
    // The string branch of `new URLSearchParams(init)` parses
    // application/x-www-form-urlencoded, which is total over
    // arbitrary strings (never throws) and fires no user code.
    assert!((classify(r#"new URLSearchParams("a=1&b=2")"#)).is_pure());
}

#[test]
fn url_search_params_non_literal_args_stay_unknown() {
    // A non-literal string-valued expression would need value-class
    // tracking to prove it can't be an object whose ToString fires
    // user code; the record/iterable form fires [[Get]]/iterator
    // protocol on user values.
    assert!(!(classify("new URLSearchParams(q)")).is_pure());
    assert!(!(classify("new URLSearchParams({ a: 1 })")).is_pure());
    assert!(!(classify("new URLSearchParams(`a=${x}`)")).is_pure());
}

#[test]
fn fresh_proxy_object_literals_are_pure() {
    assert!(classify("new Proxy({}, {})").is_pure());
    assert!(
        classify("new Proxy({ value: 1 }, { get(target, key) { return target[key]; } })").is_pure()
    );
    assert!(classify("new Proxy({ nested: { [1]() {} } }, {})").is_pure());
}

#[test]
fn proxy_constructor_rejects_unproved_args() {
    // ProxyCreate requires both arguments to be Objects. The narrow
    // constructor rule accepts only two syntactically fresh literals,
    // and argument evaluation remains subject to the ordinary checks.
    for src in [
        "new Proxy()",
        "new Proxy({})",
        "new Proxy(null, {})",
        "new Proxy({}, null)",
        "new Proxy(target, {})",
        "new Proxy({}, {}, {})",
        "new Proxy(...[{}, {}])",
        "new Proxy({ value: effect() }, {})",
        "new Proxy({}, { [effect()]() {} })",
        "new Proxy({ nested: { [effect()]() {} } }, {})",
        "new Proxy({ nested: { get [effect()]() {} } }, {})",
        "new Proxy({ nested: { set [effect()](value) {} } }, {})",
    ] {
        assert!(!classify(src).is_pure(), "expected `{src}` to stay unknown");
    }
}

#[test]
fn computed_method_and_accessor_keys_are_evaluated() {
    // Computed names run when the object literal is created, even though
    // the method/accessor body is deferred until a later operation.
    for src in [
        "({ [effect()]() {} })",
        "({ get [effect()]() {} })",
        "({ set [effect()](value) {} })",
    ] {
        assert!(!classify(src).is_pure(), "expected `{src}` to stay unknown");
    }
}

#[test]
fn shadowed_proxy_constructor_falls_back_to_unknown() {
    // Proxy is in `builtins::GLOBALS`, so all
    // chunk-top binding forms suppress the intrinsic rule.
    for prefix in [
        "const Proxy = userland;",
        "function Proxy() {}",
        r#"import Proxy from "userland";"#,
    ] {
        assert!(
            !classify_with_module(prefix, "new Proxy({}, {})").is_pure(),
            "expected a chunk-top Proxy binding in `{prefix}` to shadow the intrinsic"
        );
    }

    let local_shadowed = BTreeSet::from(["Proxy".to_string()]);
    let purity = classify_last_const("", "new Proxy({}, {})", |_, shadowed, init| {
        classify_expr_purity(
            init,
            shadowed,
            &local_shadowed,
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
        .project()
    });
    assert!(
        !purity.is_pure(),
        "a body-local Proxy binding must shadow the intrinsic"
    );
}

#[test]
fn shadowed_whatwg_constructor_falls_back_to_unknown() {
    // Host constructors join `builtins::GLOBALS`, so a chunk-top rebind
    // disables their rows.
    assert!(
        !(classify_with_module(
            "const TextDecoder = class { constructor() { globalThis.boom = 1; } };",
            "new TextDecoder()"
        ))
        .is_pure()
    );
    assert!(
        !(classify_with_module(
            "function URLSearchParams() {}",
            r#"new URLSearchParams("a=1")"#
        ))
        .is_pure()
    );
}

#[test]
fn shadowed_builtin_new_falls_back_to_unknown() {
    // SOUNDNESS: `compute_shadowed_globals` must track every global a
    // builtin row names (`builtins::GLOBALS`), not just member receivers.
    // A chunk-top
    // `const Map = class { … }` rebinds the name; `new Map()` then
    // constructs the user class, which can run arbitrary code.
    assert!(
        !(classify_with_module(
            "const Map = class { constructor() { globalThis.boom = 1; } };",
            "new Map()"
        ))
        .is_pure()
    );
    assert!(!(classify_with_module("function Set() {}", "new Set()")).is_pure());
    assert!(
        !(classify_with_module(
            r#"import { Map } from "./userland.js";"#,
            "new Map([['k', 1]])"
        ))
        .is_pure()
    );
    // Shadowed global callables fall back the same way.
    assert!(!(classify_with_module("const Symbol = userland;", "Symbol('x')")).is_pure());
}

// --- Class definition eager-evaluation effects ---------------------------

#[test]
fn classify_class_extends_expr_effects_propagate() {
    // `extends <expr>` evaluates at class-definition time.
    assert!(!(classify("class extends io() {}")).is_pure());
    // A plain Ident superclass is admitted (reading `.prototype`
    // off an ordinary constructor fires no user code; A11 covers
    // the exotic-object case).
    assert!((classify("class extends B {}")).is_pure());
}

#[test]
fn classify_class_computed_keys_require_safe_primitive_keys() {
    // Computed member keys evaluate eagerly AND run ToPropertyKey
    // on the value — any member kind, static or not.
    assert!(!(classify("class { [io()]() {} }")).is_pure());
    assert!(!(classify("class { [k]() {} }")).is_pure());
    assert!(!(classify("class { [k] = 1; }")).is_pure());
    assert!(!(classify("class { static [k] = 1; }")).is_pure());
    // Primitive and well-known-symbol keys are safe.
    assert!((classify("class { ['m']() {} }")).is_pure());
    assert!((classify("class { [Symbol.iterator]() {} }")).is_pure());
    assert!((classify("class { [Symbol.dispose]() {} }")).is_pure());
    assert!((classify("class { [Symbol.asyncDispose]() {} }")).is_pure());
    assert!(
        !(classify_with_module(
            "const Symbol = { dispose: io() };",
            "class { [Symbol.dispose]() {} }"
        ))
        .is_pure()
    );
    assert!((classify("class { ['x'] = io(); }")).is_pure()); // instance init is lazy
}

#[test]
fn immutable_symbol_const_is_safe_only_as_a_computed_key() {
    let classify_key: fn(&str, &str) -> Purity = classify_built;
    assert!(classify_key("const brand = Symbol('brand');", "class { [brand]() {} }").is_pure());
    assert!(
        classify_key(
            "const brand = Symbol('brand'); const alias = brand;",
            "class { [alias]() {} }"
        )
        .is_pure()
    );
    assert!(!classify_key("const brand = Symbol('brand');", "`${brand}`").is_pure());
    assert!(!classify_key("let brand = Symbol('brand');", "class { [brand]() {} }").is_pure());
    assert!(!classify_key("const brand = Symbol(value);", "class { [brand]() {} }").is_pure());
    assert!(
        !classify_key(
            "const Symbol = () => ({ toString() { return 'x'; } }); const brand = Symbol('brand');",
            "class { [brand]() {} }"
        )
        .is_pure()
    );
    assert!(
        !classify_key(
            "Symbol = make; const brand = Symbol('brand');",
            "class { [brand]() {} }"
        )
        .is_pure()
    );
    for mutation in [
        "({ Symbol } = provider);",
        "globalThis['Symbol'] = make;",
        "const globalAlias = globalThis; globalAlias.Symbol = make;",
        "Object.defineProperty(globalThis, 'Symbol', { value: make });",
        "Reflect.set(globalThis, 'Symbol', make);",
    ] {
        let source = format!("{mutation} const brand = Symbol('brand');");
        assert!(
            !classify_key(&source, "class { [brand]() {} }").is_pure(),
            "visible Symbol replacement must disable the key proof: {mutation}"
        );
    }
}

/// Parse a single class declaration with decorators /
/// auto-accessors enabled and run `classify_class_static_purity`
/// on it with empty shadow/annotation context.
fn class_observable(src: &str) -> bool {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(FileName::Custom("test.js".into()).into(), src.to_string());
    let lexer = Lexer::new(
        Syntax::Es(swc_ecma_parser::EsSyntax {
            decorators: true,
            auto_accessors: true,
            ..Default::default()
        }),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    let module = Parser::new_from(lexer).parse_module().unwrap();
    let class = module
        .body
        .iter()
        .find_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(cls))) => Some(&cls.class),
            _ => None,
        })
        .expect("expected a class declaration");
    !classify_class_static_purity(
        class,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeSet::new(),
        &ChunkCodeGraph::default(),
    )
    .project()
    .is_pure()
}

#[test]
fn class_decorators_are_observable() {
    // Decorator application CALLS the decorator function at
    // class-definition time — any decorator (class-level or
    // member-level) is observable.
    assert!(class_observable("@dec class C {}"));
    assert!(class_observable("class C { @dec m() {} }"));
    assert!(class_observable("class C { @dec x = 1; }"));
    assert!(class_observable("class C { @dec accessor x = 1; }"));
    // Positive control: same members without decorators.
    assert!(!class_observable("class C { m() {} x = 1; }"));
}

#[test]
fn class_static_auto_accessor_initializer_is_observable() {
    // `static accessor x = <expr>` runs the initializer at
    // class-definition time, like a static field.
    assert!(class_observable("class C { static accessor x = io(); }"));
    assert!(!class_observable("class C { static accessor x = 1; }"));
    // Instance auto-accessor initializers are lazy.
    assert!(!class_observable("class C { accessor x = io(); }"));
}

// --- Declared purity (spec annotation) ---------------------------------

#[test]
fn declared_pure_ident_call_classifies_pure() {
    // A spec member with `purity: "pure"` populates the
    // declared-pure set. A call whose callee is the bound
    // Ident classifies Pure regardless of the body content
    // (the validator does not re-verify; author trust). Args
    // are still evaluated normally — pure args here, so the
    // whole call is Pure.
    assert!(
        (classify_with_declared_pure("function f(x) { return x; }", "f(42)", &["f"])).is_pure()
    );
    assert!(
        (classify_with_declared_pure("function f(x) { return x; }", "f({ k: 'v' })", &["f"]))
            .is_pure()
    );
}

#[test]
fn declared_pure_call_with_impure_arg_inherits_arg_purity() {
    // The declared-purity contract covers the function value;
    // arg evaluation is independent. An impure arg makes the
    // whole call Unknown.
    assert!(
        !(classify_with_declared_pure(
            "function f(x) { return x; } function io() { return 1; }",
            "f(io())",
            &["f"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_overrides_global_shadowing() {
    // Author trust contract: a declared-pure annotation wins
    // over both the builtin rows' shadowing fallback and the
    // body's actual contents. The validator does not
    // second-guess.
    assert!(
        (classify_with_declared_pure(
            r#"import { Boolean } from "./userland.js";"#,
            "Boolean(x)",
            &["Boolean"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_new_ident_new_classifies_pure_with_pure_args() {
    assert!(
        (classify_with_declared_pure_new(
            "class PureBox { constructor(value) { globalThis.notAnalyzed = value; } }",
            "new PureBox({ value: 1, later() { globalThis.later = true; } })",
            &["PureBox"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_new_does_not_apply_to_plain_call() {
    assert!(
        !(classify_with_declared_pure_new(
            "function PureBox(value) { globalThis.value = value; return { value }; }",
            "PureBox(1)",
            &["PureBox"]
        ))
        .is_pure()
    );
}

// --- Declared pure members (`pure_members`) ----------------------------

#[test]
fn declared_pure_member_call_classifies_pure() {
    // A spec member with `pure_members: [forwardRef]` admits
    // `<binding>.forwardRef(args)` as pure with pure args, even
    // though the binding's body is unknown to the analyzer
    // (the vendor namespace shape we target). Args still
    // classified independently.
    assert!(
        (classify_with_declared_pure_members(
            r#"import * as b from "vendor";"#,
            "b.forwardRef(function () {})",
            "b",
            &["forwardRef"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_member_does_not_bleed_to_other_props() {
    // Only the listed property is treated pure. A sibling
    // property call stays Unknown.
    assert!(
        !(classify_with_declared_pure_members(
            r#"import * as b from "vendor";"#,
            "b.unknownMethod(1)",
            "b",
            &["forwardRef"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_member_does_not_bleed_to_other_bindings() {
    // Only the listed binding is treated pure. A call on a
    // different binding stays Unknown.
    assert!(
        !(classify_with_declared_pure_members(
            r#"import * as b from "vendor"; import * as c from "other";"#,
            "c.forwardRef(1)",
            "b",
            &["forwardRef"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_member_computed_access_falls_back_to_unknown() {
    // Computed access (`b[expr]`) is intentionally not
    // admitted — `expr` may evaluate to a different property
    // at runtime, breaking the spec author's trust contract.
    assert!(
        !(classify_with_declared_pure_members(
            r#"import * as b from "vendor"; const key = "forwardRef";"#,
            "b[key](1)",
            "b",
            &["forwardRef"]
        ))
        .is_pure()
    );
}

#[test]
fn declared_pure_member_optional_chain_call_classifies_pure() {
    // Optional chaining (`b?.forwardRef(...)`) only adds a
    // null/undefined short-circuit — the call itself still
    // qualifies under the same admission rule.
    assert!(
        (classify_with_declared_pure_members(
            r#"import * as b from "vendor";"#,
            "b?.forwardRef(1)",
            "b",
            &["forwardRef"]
        ))
        .is_pure()
    );
}

// --- Fluent-trusted chains (`fluent_exports`) ---------------------------

#[test]
fn fluent_root_direct_call_classifies_pure() {
    // The root itself is callable under the deep-purity contract
    // (`k({...})` — zod-style builders are invoked directly too).
    assert!(
        classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor";"#,
            "k({ a: 1 })",
            &["k"]
        )
        .is_pure()
    );
    // Without the assertion the same call is an unknown imported
    // callee.
    assert!(
        !classify_with_fluent_bindings(r#"import { e4 as k } from "vendor";"#, "k({ a: 1 })", &[])
            .is_pure()
    );
}

#[test]
fn fluent_chain_method_calls_classify_pure() {
    // The receivers of `.optional()` / `.describe()` are call
    // RESULTS, not bindings — no binding-keyed arm
    // (`pure_members`, `imported_purities`) can ever admit them.
    // The fluent contract follows the chain through arbitrary
    // depth.
    assert!(
        classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor";"#,
            r#"k.object({ a: 1 }).optional().describe("docs")"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_chain_impure_inner_argument_surfaces() {
    // Soundness pin: the assertion covers the API's own functions
    // and their results, NOT caller-supplied argument evaluation.
    // An impure argument to an INNER chain link must keep the whole
    // expression impure even though the outer call's args are pure.
    assert!(
        !classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor"; function io() { globalThis.x = 1; return {}; }"#,
            r#"k.object(io()).describe("docs")"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_chain_member_read_classifies_pure() {
    // Reading a property off a derived value (`.description` on a
    // schema) is covered by the same deep contract.
    assert!(
        classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor";"#,
            r#"k.string().description"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_chain_computed_member_breaks_the_chain() {
    // Computed access would additionally require a
    // ToPropertyKey-safe key; the chain conservatively stops there.
    assert!(
        !classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor"; const key = "object";"#,
            r#"k[key]({ a: 1 })"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_chain_optional_chaining_classifies_pure() {
    // `?.` only adds a null/undefined short-circuit on top of the
    // member/call evaluation the contract already covers.
    assert!(
        classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor";"#,
            r#"k.object({ a: 1 })?.describe?.("docs")"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_root_body_local_shadow_defeats_trust() {
    // A body-local binding of the same name is a different value
    // than the annotated import — the trust contract doesn't cover
    // it (same rule as every other author-trust arm).
    let local_shadowed = BTreeSet::from(["k".to_string()]);
    let purity = classify_last_const(
        r#"import { e4 as k } from "vendor";"#,
        "k.object({ a: 1 })",
        |body, shadowed, init| {
            let graph = ChunkCodeGraph::build_full(
                body,
                shadowed,
                &BTreeSet::new(),
                &BTreeSet::new(),
                &BTreeMap::new(),
                &BTreeMap::new(),
                &string_set(&["k"]),
            );
            classify_expr_purity(init, shadowed, &local_shadowed, &BTreeSet::new(), &graph)
                .project()
        },
    );
    assert!(!purity.is_pure());
}

#[test]
fn fluent_trust_propagates_through_const_derivations() {
    // `const S = k.object({...})` makes `S` a fluent root too —
    // `S.extend({...})` downstream is the standard zod base-schema
    // reuse pattern.
    assert!(
        classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor"; const S = k.object({ a: 1 });"#,
            r#"S.extend({ b: 2 })"#,
            &["k"]
        )
        .is_pure()
    );
}

#[test]
fn fluent_trust_does_not_propagate_through_let_rebindable_cells() {
    // A `let` cell can be rebound to an untrusted value later, so
    // derivation-closure is `const`-only.
    assert!(
        !classify_with_fluent_bindings(
            r#"import { e4 as k } from "vendor"; let S = k.object({ a: 1 });"#,
            r#"S.extend({ b: 2 })"#,
            &["k"]
        )
        .is_pure()
    );
}

// --- Object.{entries,keys,values,freeze,fromEntries} on plain data -----

#[test]
fn object_keys_on_plain_object_literal_classifies_pure() {
    // `Object.keys({a: 1, b: 2})` — fresh plain object literal,
    // no accessors. Spec: §20.1.2.17 calls
    // `EnumerableOwnPropertyNames(O, "key")` which only does
    // `[[OwnPropertyKeys]]` and `[[GetOwnProperty]]` — no
    // user code fires.
    assert!((classify(r#"Object.keys({a: 1, b: 2})"#)).is_pure());
}

#[test]
fn object_values_on_plain_object_literal_classifies_pure() {
    // `Object.values({a: 1, b: 2})` — same as keys, plus
    // `[[Get]]` on each own key. Plain literal has only data
    // properties, so no accessor fires.
    assert!((classify(r#"Object.values({a: 1, b: 2})"#)).is_pure());
}

#[test]
fn object_freeze_on_vite_namespace_facade_classifies_pure() {
    assert!(
        (classify(
            r#"Object.freeze(Object.defineProperty(
                { __proto__: null, run: handler },
                Symbol.toStringTag,
                { value: "Module" }
            ))"#,
        ))
        .is_pure()
    );
}

#[test]
fn object_define_property_namespace_facade_requires_fresh_target() {
    assert!(
        !(classify(r#"Object.defineProperty(target, Symbol.toStringTag, { value: "Module" })"#))
            .is_pure()
    );
}

#[test]
fn object_define_property_namespace_facade_rejects_accessor_descriptor() {
    assert!(
        !(classify(
            r#"Object.defineProperty(
                { __proto__: null, run: handler },
                Symbol.toStringTag,
                { get: () => handler }
            )"#,
        ))
        .is_pure()
    );
}

#[test]
fn object_values_on_object_with_method_stays_unknown() {
    // A method property (`{m() {}}`) is also rejected by the
    // strict shape predicate — admission requires
    // `Prop::KeyValue` / `Prop::Shorthand` only.
    assert!(!(classify(r#"Object.values({ m() { return 1; } })"#)).is_pure());
}

#[test]
fn object_entries_on_non_literal_arg_stays_unknown() {
    // Arbitrary expression: could be a Proxy or carry user
    // accessors. Stay Unknown.
    assert!(!(classify("Object.entries(somefn())")).is_pure());
    assert!(!(classify("Object.entries(x)")).is_pure());
}

#[test]
fn object_freeze_on_object_with_proto_stays_unknown() {
    // `{__proto__: …}` in an object literal sets the
    // prototype — rejected by `is_plain_data_prop`.
    assert!(!(classify(r#"Object.freeze({ __proto__: x, a: 1 })"#)).is_pure());
}

#[test]
fn object_freeze_on_object_with_spread_falls_back_to_unknown() {
    // Object spread in an object literal is classified
    // `ObjectSpread`-impure by the existing classifier (it
    // doesn't track "fresh-literal source ⇒ pure" for spreads),
    // so a literal carrying a spread doesn't classify pure
    // overall. The plain-data call rule gates on the overall
    // literal being pure, so the spread form falls back to
    // Unknown until the existing spread classifier is
    // refined.
    assert!(!(classify(r#"Object.freeze({ ...{a: 1}, b: 2 })"#)).is_pure());
    assert!(!(classify(r#"Object.freeze({ ...src, b: 2 })"#)).is_pure());
}

#[test]
fn object_keys_on_plain_data_binding_classifies_pure() {
    // `Object.keys(plain)` where `plain` is a chunk-top
    // `const plain = {…}` bound to a plain-data shape —
    // accessor-free because its allocation stays private and
    // clean (`ChunkHeap`). Same admission as a fresh literal.
    assert!(classify_built("const plain = { a: 1 };", "Object.keys(plain)").is_pure());
}

#[test]
fn object_from_entries_on_object_literal_stays_unknown() {
    // `Object.fromEntries({...})` is a TypeError at runtime
    // (objects aren't iterable). Stay Unknown to avoid the
    // observable throw.
    assert!(!(classify(r#"Object.fromEntries({a: 1})"#)).is_pure());
}

#[test]
fn object_from_entries_on_non_pair_entries_stays_unknown() {
    // Entries that aren't 2-element Array literals don't
    // qualify — Map/fromEntries semantics rely on indexed
    // [0]/[1] reads on the entry being own data properties.
    assert!(!(classify(r#"Object.fromEntries([{ "0": "k", "1": "v" }])"#)).is_pure());
    assert!(!(classify(r#"Object.fromEntries([["a", 1, "extra"]])"#)).is_pure());
}

#[test]
fn object_calls_with_too_many_args_stay_unknown() {
    // Single-argument form only. Multi-arg call (e.g. an
    // accidental third arg) falls back to Unknown rather than
    // silently admitting.
    assert!(!(classify(r#"Object.freeze({a: 1}, true)"#)).is_pure());
    assert!(!(classify(r#"Object.keys({a: 1}, "extra")"#)).is_pure());
}

#[test]
fn object_calls_shadowed_receiver_stays_unknown() {
    // A chunk-top local `Object` re-bind shadows the global
    // and the rule must fall back to Unknown — the resolved
    // value isn't the built-in.
    assert!(
        !(classify_with_module("const Object = userland;", "Object.entries({a: 1})")).is_pure()
    );
}

/// Classify `expr_src` against a fully built `ChunkCodeGraph` for the
/// wrapping `prefix` module, so chunk-top binding facts — including the
/// primitive binding facts — are populated.
fn classify_built(prefix: &str, expr_src: &str) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let structural = analyze_chunk_structural(&module, None, |_| None);
    let body = &structural.body;
    let shadowed = &structural.shadowed;
    let graph = ChunkCodeGraph::build_full_with_locations_and_stable_vars(
        body,
        shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &structural.stable_var_number_candidates,
        None,
    );
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(result))) = module.body.last().unwrap() else {
        panic!("expected final initializer");
    };
    classify_expr_purity(
        result.decls[0].init.as_deref().unwrap(),
        shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &graph,
    )
    .project()
}

#[test]
fn coercion_of_primitive_const_binding_is_pure() {
    // A chunk-top `const` bound to a primitive is immutable and carries
    // no user accessors, so ToString / ToNumber on a reference fires no
    // user code — the coercion is pure.
    assert!(classify_built("const s = \"x\";", "`v=${s}`").is_pure());
    assert!(classify_built("const n = 5;", "-n").is_pure());
    assert!(classify_built("const a = 1; const b = 2;", "a + b").is_pure());
    // Transitive through another primitive const, and a computed key.
    assert!(classify_built("const a = \"x\"; const b = `${a}!`;", "`${b}`").is_pure());
    assert!(classify_built("const k = \"id\";", "({ [k]: 1 })").is_pure());
}

#[test]
fn const_value_inference_does_not_resolve_aliases_from_later_declarations() {
    assert!(classify_built("const value = 1; const alias = value;", "`${alias}`").is_pure());
    assert!(!classify_built("const alias = value; const value = 1;", "`${alias}`").is_pure());
    assert!(
        !classify_built(
            "const alias = brand; const brand = Symbol('brand');",
            "class { [alias]() {} }"
        )
        .is_pure()
    );
}

#[test]
fn coercion_of_let_or_var_binding_stays_unknown() {
    // `let` / `var` can be rebound to an object later, so the binding is
    // not provably primitive and the coercion stays conservative.
    assert!(!classify_built("let s = \"x\";", "`${s}`").is_pure());
    assert!(!classify_built("var s = \"x\";", "`${s}`").is_pure());
}

#[test]
fn stable_numeric_var_candidates_are_narrow_and_keep_throwing_arithmetic_unknown() {
    let numeric_chain = "var retry = 20000, delay = 15000, base = 5000, period = base, total = delay + period + 2 * retry;";
    assert!(classify_built(numeric_chain, "total + 1").is_pure());
    assert!(classify_built("var base = 2;", "base + 2 * base").is_pure());
    assert!(classify_built("var value = 1;", "`${value}`").is_pure());
    assert!(classify_built("var value = 1;", "value + 'x'").is_pure());
    assert!(!classify_built("var value = 1;", "value + object").is_pure());
    assert!(!classify_built("var value = 1;", "value + (flag ? 1n : 2)").is_pure());

    let module = parse("var value = 1;");
    let structural = analyze_chunk_structural(&module, None, |_| None);
    let graph = ChunkCodeGraph::build_full_with_locations_and_stable_vars(
        &structural.body,
        &structural.shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &structural.stable_var_number_candidates,
        None,
    );
    assert!(graph.primitive_bindings.contains("value"));
    assert_eq!(
        graph.binding_prims.kinds.get("value"),
        Some(&PrimKinds::NUMBER.union(PrimKinds::UNDEFINED)),
        "a stable var is Number after its initializer and undefined before it"
    );

    let redeclared_module = parse("var value = 1; function value() {}");
    let redeclared_structure = analyze_chunk_structural(&redeclared_module, None, |_| None);
    let redeclared_graph = ChunkCodeGraph::build_full_with_locations_and_stable_vars(
        &redeclared_structure.body,
        &redeclared_structure.shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &redeclared_structure.stable_var_number_candidates,
        None,
    );
    assert!(!redeclared_graph.primitive_bindings.contains("value"));

    assert!(!classify_built("var value = 1; value = object;", "value + 1").is_pure());
    assert!(
        !classify_built(
            "var value = 1; function replace() { value = object; }",
            "value + 1"
        )
        .is_pure()
    );
    assert!(!classify_built("var value = 1; eval('value = object');", "value + 1").is_pure());
    assert!(!classify_built("var value = 1; var value;", "value + 1").is_pure());
    assert!(!classify_built("var value = 1;", "value + 1n").is_pure());
    assert!(
        !classify_built(
            "var value = 1; const bigint = 1n; const alias = bigint;",
            "value + alias"
        )
        .is_pure()
    );
    assert!(!classify_built("var value = 1n + 1;", "value + 1").is_pure());
}

#[test]
fn stable_numeric_var_facts_do_not_cross_function_shadowing() {
    let module = parse("var value = 1; function read(value) { return value + 1; }");
    let structural = analyze_chunk_structural(&module, None, |_| None);
    let graph = ChunkCodeGraph::build_full_with_locations_and_stable_vars(
        &structural.body,
        &structural.shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &structural.stable_var_number_candidates,
        None,
    );
    assert!(
        graph
            .function_purity("read")
            .is_some_and(|purity| !purity.is_pure())
    );
}

#[test]
fn coercion_of_non_primitive_const_stays_unknown() {
    // `const o = {}` is a const but not primitive — interpolating it runs
    // user `toString` / `[Symbol.toPrimitive]`.
    assert!(!classify_built("const o = {};", "`${o}`").is_pure());
    // A const whose init is not statically result-primitive (a bare
    // import/member read) is not admitted either.
    assert!(!classify_built("const o = globalThis.x;", "`${o}`").is_pure());
}

/// Classify `expr_src` with a populated cross-module imported-purity
/// map (as the program-level oracle would supply). Each `(name,
/// purity)` records the verdict for an imported function binding.
fn classify_with_imported_purities(
    prefix: &str,
    expr_src: &str,
    imports: &[(&str, Purity)],
) -> Purity {
    let imported_purities: BTreeMap<String, std::sync::Arc<Purity>> = imports
        .iter()
        .map(|(name, purity)| ((*name).to_string(), std::sync::Arc::new(purity.clone())))
        .collect();
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_full(
            body,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            &imported_purities,
            &BTreeSet::new(),
        )
    })
}

fn impure_verdict() -> Purity {
    Effects::of(Effect::Invoke(InvokeSite::Call), swc_common::DUMMY_SP).project()
}

#[test]
fn imported_pure_callee_makes_call_pure() {
    // The oracle resolved `memo` (an imported function) as pure, so
    // `memo(arg)` classifies pure when the args are pure.
    assert!(classify_with_imported_purities("", "memo(x)", &[("memo", Purity::pure())]).is_pure());
    assert!(
        classify_with_imported_purities("", "memo(() => {})", &[("memo", Purity::pure())])
            .is_pure()
    );
}

#[test]
fn conditioned_constructor_dependencies_flow_through_local_calls_and_bindings() {
    let module = parse(
        "const values = [1];\n\
         class Box { static value = new Set(values); }\n\
         function make() { return new Set(values); }\n\
         function wrap() { return make(); }\n\
         function left() { return right(); }\n\
         function right() { return new Set(values) && left(); }\n\
         const made = wrap();",
    );
    let views = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&views);
    let dependencies = value_input_dependencies("static/provider.js", 0);
    let graph = graph_with_conditioned_set_proof(&module, &dependencies, &BTreeSet::new());

    let ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) = &module.body[1] else {
        panic!("expected static class declaration");
    };
    assert_eq!(
        classify_class_static_purity(
            &class.class,
            &shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &graph,
        )
        .project()
        .dependencies(),
        Some(&dependencies),
        "safe eager class fields must retain their conditioned proof premise"
    );

    for function in ["make", "wrap", "left", "right"] {
        let purity = graph.function_purity(function).expect("function indexed");
        assert!(purity.is_pure(), "{function} remains pure");
        assert_eq!(purity.dependencies(), Some(&dependencies), "{function}");
    }
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(result))) = module.body.last().unwrap() else {
        panic!("expected final initializer");
    };
    let initializer = result.decls[0].init.as_deref().unwrap();
    let purity = classify_expr_purity(
        initializer,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &graph,
    )
    .project();
    assert_eq!(purity.dependencies(), Some(&dependencies));
    assert_eq!(
        classify_var_decl_purity(
            result,
            &shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &graph,
            &FreshRecordDestructuringContext {
                flow: &ChunkValueFlow::default(),
                dynamic_scope: DynamicScopeFact::default(),
                rebindings: &BTreeSet::new(),
            },
        )
        .project()
        .dependencies(),
        Some(&dependencies),
        "the declaration-level binding initializer retains its premise"
    );
}

#[test]
fn conditioned_dependencies_follow_the_exact_direct_literal_set_certificate() {
    let module = parse(
        "const certified = new Set(['a', 'b']);\n\
         const independent = new Set([{ value: 'c' }]);",
    );
    let views = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&views);
    let dependencies = value_input_dependencies("static/provider.js", 4);
    let graph = graph_with_conditioned_set_proof(&module, &dependencies, &BTreeSet::new());

    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(certified))) = &module.body[0] else {
        panic!("expected certified Set declaration");
    };
    let certified = classify_expr_purity(
        certified.decls[0].init.as_deref().unwrap(),
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &graph,
    )
    .project();
    assert_eq!(certified.dependencies(), Some(&dependencies));

    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(independent))) = &module.body[1] else {
        panic!("expected independent Set declaration");
    };
    let independent = classify_expr_purity(
        independent.decls[0].init.as_deref().unwrap(),
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &graph,
    )
    .project();
    assert!(independent.is_pure());
    assert_eq!(
        independent.dependencies(),
        Some(&ProofDependencies::default()),
        "the independent literal-iterable row must not inherit unrelated input refs"
    );
}

#[test]
fn nested_conditioned_arguments_keep_dependencies_across_pure_builtins() {
    let module = parse(
        "const values = [1];\n\
         function make() { return new Set(values); }\n\
         const proxy = new Proxy({ value: make() }, {});\n\
         const mapped = new Map([[make(), 'value']]);\n\
         const entries = Object.entries({ value: make() });\n\
         const facade = Object.defineProperty(\
             { __proto__: null, value: make() },\
             Symbol.toStringTag,\
             { value: make() },\
         );\n\
         const declared = new Container(make());",
    );
    let dependencies = value_input_dependencies("static/provider.js", 5);
    let graph = graph_with_conditioned_set_proof(
        &module,
        &dependencies,
        &BTreeSet::from(["Container".to_string()]),
    );
    let views = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&views);
    for expression in [
        "new Proxy({ value: make() }, {})",
        "new Map([[make(), 'value']])",
        "Object.entries({ value: make() })",
        "Object.defineProperty({ __proto__: null, value: make() }, Symbol.toStringTag, { value: make() })",
        "new Container(make())",
    ] {
        let purity = classify_last_const(
            "const values = [1]; function make() { return new Set(values); }",
            expression,
            |_, _, init| {
                classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
                    .project()
            },
        );
        assert!(purity.is_pure(), "{expression} remains pure");
        assert_eq!(purity.dependencies(), Some(&dependencies), "{expression}");
    }
}

#[test]
fn conditioned_local_class_dependencies_reach_the_construction_expression() {
    let module = parse("class Box { state = new Set(); }\nconst instance = new Box();");
    let views = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&views);
    let flow = ChunkValueFlow::analyze_with_candidates(&views, &tracked_allocation_roots(&views));
    let plain_data = ChunkHeap::analyze(&views, &shadowed, &flow, None)
        .plain_data_bindings()
        .bindings;
    let plain_arrays = plain_array_bindings(&views, &shadowed);
    let dependencies = value_input_dependencies("static/provider.js", 2);
    let class_shapes = crate::facts::LocalClassConstructionShapes::analyze(
        &views,
        &shadowed,
        &flow,
        DynamicScopeFact::analyze(&views),
    );
    let prepared =
        crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index(
            &views,
            &flow,
            &crate::analysis_inputs::AnalysisInputs {
                native_integrity: Some(crate::analysis_inputs::NativeIntegrity {
                    consumed_value_inputs: dependencies.value_inputs.clone(),
                    ..Default::default()
                }),
                ..Default::default()
            },
            &plain_arrays,
            &class_shapes,
            crate::facts::SetIterableShapes::default(),
            &shadowed,
        );
    let class_proof = prepared.local_class_construction_proof();
    let graph = ChunkCodeGraph::build_full_with_prepared_plain_data(
        &views,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &SetIterableProof::default(),
        class_proof,
        &plain_data,
        &BTreeMap::new(),
        &BTreeMap::new(),
        &plain_arrays,
        &BTreeSet::new(),
        None,
        None,
    );
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(result))) = module.body.last().unwrap() else {
        panic!("expected final initializer");
    };
    let purity = classify_expr_purity(
        result.decls[0].init.as_deref().unwrap(),
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &graph,
    )
    .project();
    assert!(purity.is_pure());
    assert_eq!(purity.dependencies(), Some(&dependencies));
}

#[test]
fn imported_pure_callee_preserves_conditioned_dependencies() {
    let dependencies = value_input_dependencies("static/provider.js", 4);
    let purity = classify_with_imported_purities(
        "",
        "memo(x)",
        &[("memo", Purity::pure().with_dependencies(&dependencies))],
    );
    assert_eq!(purity.dependencies(), Some(&dependencies));
}

#[test]
fn imported_impure_callee_keeps_call_impure() {
    assert!(!classify_with_imported_purities("", "run(x)", &[("run", impure_verdict())]).is_pure());
}

#[test]
fn imported_pure_callee_still_classifies_arguments() {
    // `memo` is pure, but a possibly-object operand coerced in the arg
    // keeps the whole call impure — args are classified independently.
    assert!(
        !classify_with_imported_purities("", "memo(A + 1)", &[("memo", Purity::pure())]).is_pure()
    );
}

#[test]
fn string_raw_tagged_templates_require_global_tag_and_primitive_substitutions() {
    assert!(classify("String.raw`plain`").is_pure());
    assert!(classify("String.raw`value: ${1}`").is_pure());
    assert!(classify_built("const value = 1;", "String.raw`${value}`").is_pure());
    assert!(!(classify("String.raw`${{ toString() { return 'x'; } }}`")).is_pure());
    assert!(!(classify("String.raw`${unknown}`")).is_pure());
    assert!(!(classify("String.raw`${io()}`")).is_pure());
    assert!(!(classify("other`plain`")).is_pure());
    assert!(!(classify("String['raw']`plain`")).is_pure());
    assert!(!(classify_with_module("const String = local;", "String.raw`plain`")).is_pure());
}
