use analysis_domain::item_views::TopLevelItemView;
use analysis_model::PrimKinds;
use analysis_vocabulary::effect::CoerceSite;
use analysis_vocabulary::effect::Effect;
use analysis_vocabulary::effect::InvokeSite;
use purity::PurityScope;
use purity::classifier::classify_class_static_purity;
use purity::classifier::classify_expr_purity;
use purity::classifier::classify_var_decl_purity;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Decl;
use swc_ecma_ast::Expr;
use swc_ecma_ast::Module;
use swc_ecma_ast::ModuleItem;
use swc_ecma_ast::Stmt;

use analysis::facts::analyze::analyze_chunk_structural;
use analysis::facts::statement_facts::StructuralChunkAnalysis;
use analysis_domain::allocation_roots::tracked_allocation_roots;
use analysis_domain::dynamic_scope::DynamicScopeFact;
use analysis_domain::item_views::{compute_shadowed_globals, top_level_item_views};
use analysis_vocabulary::inputs::{ProofDependencies, ValueInputRef};
use analysis_vocabulary::purity::{Effects, Purity};
use purity::heap::{ChunkHeap, plain_array_bindings};
use purity::*;
use purity::{LocalClassConstructionProof, SetIterableProof};
use swc_common::{FileName, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};
use value_flow::ChunkValueFlow;

use raw_js_test_support::parse;

fn classify(src: &str) -> Vec<Effect> {
    // Wrap the expression in a const so we can parse a module.
    let module = parse(&format!("const _ = {src};"));
    let var = match &module.body[0] {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected `const _ = ...;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(
        init,
        &PurityScope::chunk_top(
            &BTreeSet::new(),
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        ),
    )
    .kinds()
}

/// Parse `{prefix}\nconst _ = {expr_src};` and hand `classify` the chunk-top item
/// views, the shadowed-globals set of the whole module and the last const's
/// initializer.
fn classify_last_const(
    prefix: &str,
    expr_src: &str,
    classify: impl FnOnce(&[TopLevelItemView<'_>], &BTreeSet<&'static str>, &Expr) -> Effects,
) -> Vec<Effect> {
    classify_last_const_effects(prefix, expr_src, classify).kinds()
}

fn classify_last_const_effects(
    prefix: &str,
    expr_src: &str,
    classify: impl FnOnce(&[TopLevelItemView<'_>], &BTreeSet<&'static str>, &Expr) -> Effects,
) -> Effects {
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
) -> Vec<Effect> {
    classify_last_const(prefix, expr_src, |body, shadowed, init| {
        let graph = build(body, shadowed);
        classify_expr_purity(
            init,
            &PurityScope::chunk_top(shadowed, &BTreeSet::new(), &graph),
        )
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
    let set_shapes =
        purity::SetIterableShapes::analyze(&views, &shadowed, &flow, &plain_data, dynamic_scope);
    let class_shapes =
        purity::LocalClassConstructionShapes::analyze(&views, &shadowed, &flow, dynamic_scope);
    let prepared = sync_effects::interpreter::synchronous_calls::prepare_synchronous_call_index(
        &views,
        &flow,
        &analysis_vocabulary::inputs::AnalysisInputs {
            native_integrity: Some(analysis_vocabulary::inputs::NativeIntegrity {
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
        &BTreeSet::new(),
        None,
    )
}

/// Run the classifier against `src` after computing the
/// chunk-top-level shadowed-globals set from a wrapping
/// module. Lets tests check the shadowing fallback.
fn classify_with_module(prefix: &str, expr_src: &str) -> Vec<Effect> {
    classify_last_const(prefix, expr_src, |_, shadowed, init| {
        classify_expr_purity(
            init,
            &PurityScope::chunk_top(shadowed, &BTreeSet::new(), &ChunkCodeGraph::default()),
        )
    })
}

/// `(case, chunk-top prefix, expression, expected effects)`.
type PrefixCase<'a> = (&'a str, &'a str, &'a str, &'a [Effect]);

/// `(case, chunk-top prefix, expression, annotated bindings, expected effects)`.
type AnnotatedCase<'a> = (&'a str, &'a str, &'a str, &'a [&'a str], &'a [Effect]);

/// `(case, expression, imported `(binding, verdict)`, expected effects)`.
type ImportCase<'a> = (&'a str, &'a str, (&'a str, Purity), &'a [Effect]);

/// Check each case with [`classify_with_module`], naming the failing case.
fn assert_module_cases(cases: &[PrefixCase<'_>]) {
    for (case, prefix, src, expected) in cases {
        assert_eq!(
            classify_with_module(prefix, src).as_slice(),
            *expected,
            "{case}"
        );
    }
}

/// Run the classifier against `src` with both shadowing and an
/// explicit declared-pure binding set.
fn classify_with_declared_pure(prefix: &str, expr_src: &str, declared: &[&str]) -> Vec<Effect> {
    let declared_pure = string_set(declared);
    classify_last_const(prefix, expr_src, |_, shadowed, init| {
        classify_expr_purity(
            init,
            &PurityScope::chunk_top(shadowed, &declared_pure, &ChunkCodeGraph::default()),
        )
    })
}

fn classify_with_declared_pure_new(prefix: &str, expr_src: &str, declared: &[&str]) -> Vec<Effect> {
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_with_declared_pure_new(
            body,
            shadowed,
            &BTreeSet::new(),
            &string_set(declared),
            &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
        )
    })
}

fn classify_with_declared_pure_members(
    prefix: &str,
    expr_src: &str,
    binding: &str,
    props: &[&str],
) -> Vec<Effect> {
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
            &BTreeSet::new(),
            &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
        )
    })
}

/// Classify `expr_src` (appearing after `prefix` at chunk top) with
/// `fluent` as the author-asserted fluent root bindings.
fn classify_with_fluent_bindings(prefix: &str, expr_src: &str, fluent: &[&str]) -> Vec<Effect> {
    classify_with_graph(prefix, expr_src, |body, shadowed| {
        ChunkCodeGraph::build_full(
            body,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &string_set(fluent),
            &BTreeSet::new(),
            &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
        )
    })
}

const NONE: [Effect; 0] = [];
const CALL: Effect = Effect::Invoke(InvokeSite::Call);
const CONSTRUCT: Effect = Effect::Invoke(InvokeSite::Construct);
const TAGGED_TEMPLATE: Effect = Effect::Invoke(InvokeSite::TaggedTemplate);
const MEMBER_READ: Effect = Effect::Invoke(InvokeSite::MemberRead);
const CLASS_DEFINITION: Effect = Effect::Invoke(InvokeSite::ClassDefinition);
const COERCE_OPERAND: Effect = Effect::Coerce(CoerceSite::Operator);
const COERCE_KEY: Effect = Effect::Coerce(CoerceSite::PropertyKey);
const ARRAY_SPREAD: Effect = Effect::Coerce(CoerceSite::ArraySpread);
const OBJECT_SPREAD: Effect = Effect::Coerce(CoerceSite::ObjectSpread);

fn assert_no_effects(srcs: &[&str]) {
    for src in srcs {
        assert_eq!(classify(src), NONE, "{src}");
    }
}

fn assert_each_has_effects(srcs: &[&str], expected: &[Effect]) {
    for src in srcs {
        assert_eq!(classify(src), expected, "{src}");
    }
}

#[test]
fn pure_unary_and_binary_have_no_effects() {
    assert_no_effects(&[
        "-1",
        "!FOO",
        "typeof FOO",
        "void FOO",
        "A && B",
        "A || B",
        "A ?? B",
        "A === B",
        "A !== B",
        "A ? B : C",
        // Coercing operators on statically-primitive operands: the engine's
        // ToPrimitive of a primitive runs no user code. Nested coercing
        // results are themselves primitive.
        "1 + 2",
        "'a' + 1",
        "1 + 2 + 3",
        "1e3 * 60 * 60",
        "-(1 + 2)",
        "~0",
        "(typeof A) + '!'",
        "(A === B) | 0",
    ]);
}

#[test]
fn coercing_operators_on_possibly_object_operands_coerce() {
    // SOUNDNESS: ToPrimitive / ToNumber / ToString on an object operand fires
    // user `valueOf` / `toString` / `[Symbol.toPrimitive]`, and an opaque
    // identifier can hold an object. The shadowable global `undefined` is an
    // ordinary binding the shadow pass doesn't track, so it is not admitted as
    // a primitive operand either.
    assert_each_has_effects(
        &[
            "A + 1",
            "A - 1",
            "A * B",
            "A < 10",
            "A >= B",
            "A == null",
            "A != B",
            "A & 1",
            "A << 2",
            "+A",
            "-A",
            "~A",
            "A == undefined",
            "undefined + 1",
        ],
        &[COERCE_OPERAND],
    );
}

#[test]
fn in_and_instanceof_coerce_their_operands() {
    // `in` fires the proxy `has` trap on its RHS; `instanceof` fires
    // `@@hasInstance` / reads `.prototype` on its RHS. The interesting RHS is
    // always an object, so no primitive-operand gate can admit these.
    assert_each_has_effects(
        &["'k' in o", "x instanceof C", "'k' in { k: 1 }"],
        &[COERCE_OPERAND],
    );
}

#[test]
fn assignment_and_update_write() {
    assert_each_has_effects(&["(x = 1)", "x++"], &[Effect::Write]);
}

#[test]
fn call_new_tagged_template_invoke_unknown_code() {
    assert_eq!(classify("foo()"), [CALL]);
    assert_eq!(classify("new Foo()"), [CONSTRUCT]);
    assert_eq!(classify("tag`hi ${x}`"), [TAGGED_TEMPLATE]);
}

#[test]
fn member_access_may_run_a_getter() {
    assert_each_has_effects(&["o.x", "o[k]", "o?.x"], &[MEMBER_READ]);
}

#[test]
fn object_literal_effects_come_from_its_parts() {
    assert_no_effects(&[
        "({ a: 1, b: 'x' })",
        // Computed keys run ToPropertyKey on the key VALUE; pure only for
        // statically-primitive keys and well-known symbols.
        "({ ['k']: 1 })",
        "({ [1 + 2]: 1 })",
        "({ [Symbol.iterator]: 1 })",
        // Method definitions define, not call.
        "({ m() { return io(); } })",
    ]);
    // An opaque Ident can hold an object whose `toString` fires.
    assert_eq!(classify("({ [k]: 1 })"), [COERCE_KEY]);
    assert_eq!(classify("({ [k.x]: 1 })"), [MEMBER_READ, COERCE_KEY]);
    // Spread of an arbitrary expr: a getter could fire.
    assert_eq!(classify("({ ...other })"), [OBJECT_SPREAD]);
}

#[test]
fn array_literal_effects_come_from_its_elements() {
    assert_no_effects(&["[1, 2, 'x']", "[A, B]"]);
    assert_eq!(classify("[1, foo()]"), [CALL]);
    // Arbitrary iterable spread can call a user-defined `[Symbol.iterator]`.
    assert_eq!(classify("[...other]"), [ARRAY_SPREAD]);
}

#[test]
fn spreading_a_fresh_array_literal_has_no_effects() {
    assert_no_effects(&[
        "[...[1, 2, 'x']]",
        "[...(flag ? ['a'] : [])]",
        "[...[() => io(), function () { globalThis.x = 1; }]]",
    ]);
    assert_eq!(classify("[...[io()]]"), [CALL]);
    assert_eq!(classify("[...(flag ? ['a'] : other)]"), [ARRAY_SPREAD]);
}

#[test]
fn callbacks_inside_literals_are_values_not_init_calls() {
    assert_no_effects(&[
        "({ cb: () => io(), nested: [function () { console.log('x'); }] })",
        "[() => io(), function () { globalThis.touched = true; }]",
    ]);
}

#[test]
fn function_and_arrow_definitions_have_no_effects() {
    assert_no_effects(&["function () { return io(); }", "() => io()"]);
}

#[test]
fn class_expr_static_initializers_are_class_definition_effects() {
    assert_no_effects(&["class { m() { return io(); } }", "class { static x = 1 }"]);
    assert_each_has_effects(
        &["class { static x = io() }", "class { static {} }"],
        &[CLASS_DEFINITION],
    );
}

#[test]
fn template_interpolation_requires_primitive_values() {
    // Interpolation runs ToString on the value.
    assert_no_effects(&[
        "`a${1 + 2}b${'x'}c`",
        "`a${`inner${1}`}b`",
        "`a${typeof A}b`",
    ]);
    assert_eq!(classify("`a${A}b`"), [COERCE_OPERAND]);
    assert_eq!(classify("`a${foo()}`"), [CALL, COERCE_OPERAND]);
}

#[test]
fn strict_equality_keeps_operand_effects() {
    assert_eq!(classify("foo() === 1"), [CALL]);
    assert_eq!(classify("1 !== foo()"), [CALL]);
}

#[test]
fn sequence_keeps_every_operand_effect() {
    assert_no_effects(&["(A, B, C)"]);
    assert_eq!(classify("(A, foo(), C)"), [CALL]);
    assert_eq!(classify("(A, x = 1, C)"), [Effect::Write]);
}

#[test]
fn builtin_call_arguments_keep_their_effects() {
    // Evaluating the argument fires user code even though `Array.isArray`
    // itself is a pure operation.
    assert_eq!(classify("Array.isArray(io())"), [CALL]);
    assert_eq!(classify("Number.isNaN(o.x)"), [MEMBER_READ]);
}

#[test]
fn function_valued_property_calls_with_opaque_arguments_invoke() {
    // Reading a function-valued data property is pure; calling it is
    // summarized only by its row, and argument purity alone never admits it.
    assert_each_has_effects(
        &[
            "Object.defineProperty(t, 'k', { value: 1 })",
            "Object.freeze(o)",
            "Object.values(o)",
            "Object.keys(o)",
        ],
        &[CALL],
    );
}

#[test]
fn boolean_is_pure_on_any_value() {
    // ToBoolean fires no user code on any value.
    assert_no_effects(&["Boolean(x)", "Boolean(0)", "Boolean({})"]);
}

#[test]
fn string_conversion_operands_must_be_primitive() {
    // `ToString` completes without user code on any result-primitive operand.
    assert_no_effects(&[
        "Symbol(`description`)",
        "Symbol(1 + 2)",
        "new URLSearchParams(`a=1`)",
    ]);
    assert_eq!(classify("Symbol(x)"), [COERCE_OPERAND]);
    assert_eq!(classify("Symbol({})"), [COERCE_OPERAND]);
    assert_eq!(
        classify("new URLSearchParams(q)"),
        [CONSTRUCT, COERCE_OPERAND]
    );
}

#[test]
fn chunk_top_bindings_shadow_builtin_rows() {
    // SOUNDNESS: `compute_shadowed_globals` must track every global a builtin
    // row names, not just member receivers. A chunk-top decl or import
    // specifier (A8) rebinding the name makes the rows fall back to the
    // generic member read / call / construct: the user-bound value can be
    // anything, so `<binding>.<prop>` may fire a getter and `new <binding>()`
    // may run arbitrary code.
    assert_module_cases(&[
        (
            "const Object, function-valued read",
            "const Object = userland;",
            "Object.defineProperty",
            &[MEMBER_READ],
        ),
        (
            "imported Object, function-valued read",
            r#"import { Object } from "./userland.js";"#,
            "Object.freeze",
            &[MEMBER_READ],
        ),
        (
            "const Object, call",
            "const Object = userland;",
            "Object.entries({a: 1})",
            &[CALL],
        ),
        (
            "const Math",
            "const Math = userland;",
            "Math.PI",
            &[MEMBER_READ],
        ),
        (
            "function Math",
            "function Math() {}",
            "Math.E",
            &[MEMBER_READ],
        ),
        (
            "const Array",
            "const Array = X;",
            "Array.isArray(x)",
            &[CALL],
        ),
        ("let Number", "let Number = X;", "Number.isNaN(x)", &[CALL]),
        ("const Boolean", "const Boolean = X;", "Boolean(x)", &[CALL]),
        (
            "named import Math",
            r#"import { Math } from "./userland.js";"#,
            "Math.PI",
            &[MEMBER_READ],
        ),
        (
            "default import Boolean",
            r#"import Boolean from "./userland.js";"#,
            "Boolean(x)",
            &[CALL],
        ),
        (
            "namespace import Number",
            r#"import * as Number from "./userland.js";"#,
            "Number.isNaN(x)",
            &[CALL],
        ),
        (
            "renamed import Array",
            r#"import { something as Array } from "./userland.js";"#,
            "Array.isArray(x)",
            &[CALL],
        ),
        (
            "const Map class",
            "const Map = class { constructor() { globalThis.boom = 1; } };",
            "new Map()",
            &[CONSTRUCT],
        ),
        (
            "function Set",
            "function Set() {}",
            "new Set()",
            &[CONSTRUCT],
        ),
        (
            "named import Map",
            r#"import { Map } from "./userland.js";"#,
            "new Map([['k', 1]])",
            &[CONSTRUCT],
        ),
        (
            "const Symbol, global callable",
            "const Symbol = userland;",
            "Symbol('x')",
            &[CALL],
        ),
        (
            "const Proxy",
            "const Proxy = userland;",
            "new Proxy({}, {})",
            &[CONSTRUCT],
        ),
        (
            "function Proxy",
            "function Proxy() {}",
            "new Proxy({}, {})",
            &[CONSTRUCT],
        ),
        (
            "default import Proxy",
            r#"import Proxy from "userland";"#,
            "new Proxy({}, {})",
            &[CONSTRUCT],
        ),
        // Positive anchor: only same-named shadowing disables the rows.
        (
            "unrelated chunk-top binding",
            "const other = userland;",
            "Math.PI",
            &[],
        ),
    ]);
    assert_no_effects(&["new Map()"]);
    assert_eq!(classify("Math.unknownProp"), [MEMBER_READ]);
}

#[test]
fn fresh_proxy_object_literals_have_no_effects() {
    assert_no_effects(&[
        "new Proxy({}, {})",
        "new Proxy({ value: 1 }, { get(target, key) { return target[key]; } })",
        "new Proxy({ nested: { [1]() {} } }, {})",
    ]);
}

#[test]
fn proxy_constructor_rejects_unproved_args() {
    // ProxyCreate requires both arguments to be Objects. The narrow
    // constructor rule accepts only two syntactically fresh literals, and
    // argument evaluation remains subject to the ordinary checks.
    assert_each_has_effects(
        &[
            "new Proxy()",
            "new Proxy({})",
            "new Proxy(null, {})",
            "new Proxy({}, null)",
            "new Proxy(target, {})",
            "new Proxy({}, {}, {})",
            "new Proxy(...[{}, {}])",
        ],
        &[CONSTRUCT],
    );
    assert_eq!(
        classify("new Proxy({ value: effect() }, {})"),
        [CONSTRUCT, CALL]
    );
    assert_each_has_effects(
        &[
            "new Proxy({}, { [effect()]() {} })",
            "new Proxy({ nested: { [effect()]() {} } }, {})",
            "new Proxy({ nested: { get [effect()]() {} } }, {})",
            "new Proxy({ nested: { set [effect()](value) {} } }, {})",
        ],
        &[CONSTRUCT, CALL, COERCE_KEY],
    );
}

#[test]
fn computed_method_and_accessor_keys_are_evaluated() {
    // Computed names run when the object literal is created, even though the
    // method/accessor body is deferred until a later operation.
    assert_each_has_effects(
        &[
            "({ [effect()]() {} })",
            "({ get [effect()]() {} })",
            "({ set [effect()](value) {} })",
        ],
        &[CALL, COERCE_KEY],
    );
}

#[test]
fn body_local_proxy_binding_shadows_the_intrinsic() {
    let local_shadowed = BTreeSet::from(["Proxy".to_string()]);
    let effects = classify_last_const("", "new Proxy({}, {})", |_, shadowed, init| {
        classify_expr_purity(
            init,
            &PurityScope::function_body(
                shadowed,
                &local_shadowed,
                &BTreeSet::new(),
                &ChunkCodeGraph::default(),
            ),
        )
    });
    assert_eq!(
        effects,
        [CONSTRUCT],
        "a body-local Proxy binding must shadow the intrinsic"
    );
}

#[test]
fn class_extends_expr_effects_propagate() {
    assert_eq!(classify("class extends io() {}"), [CLASS_DEFINITION]);
    // A plain Ident superclass is admitted: reading `.prototype` off an
    // ordinary constructor fires no user code (A11 covers the exotic-object
    // case).
    assert_no_effects(&["class extends B {}"]);
}

#[test]
fn class_computed_keys_require_safe_primitive_keys() {
    // Computed member keys evaluate eagerly AND run ToPropertyKey on the value.
    assert_eq!(classify("class { [io()]() {} }"), [CALL, COERCE_KEY]);
    assert_eq!(classify("class { [k]() {} }"), [COERCE_KEY]);
    assert_each_has_effects(
        &["class { [k] = 1; }", "class { static [k] = 1; }"],
        &[CLASS_DEFINITION],
    );
    // Primitive and well-known-symbol keys are safe.
    assert_no_effects(&[
        "class { ['m']() {} }",
        "class { [Symbol.iterator]() {} }",
        "class { [Symbol.dispose]() {} }",
        "class { [Symbol.asyncDispose]() {} }",
        "class { ['x'] = io(); }", // instance init is lazy
    ]);
    assert_eq!(
        classify_with_module(
            "const Symbol = { dispose: io() };",
            "class { [Symbol.dispose]() {} }"
        ),
        [MEMBER_READ, COERCE_KEY]
    );
}

#[test]
fn immutable_symbol_const_is_safe_only_as_a_computed_key() {
    let classify_key: fn(&str, &str) -> Vec<Effect> = classify_built;
    assert_eq!(
        classify_key("const brand = Symbol('brand');", "class { [brand]() {} }"),
        NONE
    );
    assert_eq!(
        classify_key(
            "const brand = Symbol('brand'); const alias = brand;",
            "class { [alias]() {} }"
        ),
        NONE
    );
    assert_eq!(
        classify_key("const brand = Symbol('brand');", "`${brand}`"),
        [COERCE_OPERAND]
    );
    assert_eq!(
        classify_key("let brand = Symbol('brand');", "class { [brand]() {} }"),
        [COERCE_KEY]
    );
    assert_eq!(
        classify_key("const brand = Symbol(value);", "class { [brand]() {} }"),
        [COERCE_KEY]
    );
    assert_eq!(
        classify_key(
            "const Symbol = () => ({ toString() { return 'x'; } }); const brand = Symbol('brand');",
            "class { [brand]() {} }"
        ),
        [COERCE_KEY]
    );
    assert_eq!(
        classify_key(
            "Symbol = make; const brand = Symbol('brand');",
            "class { [brand]() {} }"
        ),
        [COERCE_KEY]
    );
    for mutation in [
        "({ Symbol } = provider);",
        "globalThis['Symbol'] = make;",
        "const globalAlias = globalThis; globalAlias.Symbol = make;",
        "Object.defineProperty(globalThis, 'Symbol', { value: make });",
        "Reflect.set(globalThis, 'Symbol', make);",
    ] {
        let source = format!("{mutation} const brand = Symbol('brand');");
        assert_eq!(
            classify_key(&source, "class { [brand]() {} }"),
            [COERCE_KEY],
            "visible Symbol replacement must disable the key proof: {mutation}"
        );
    }
}

/// Parse a single class declaration with decorators /
/// auto-accessors enabled and return `classify_class_static_purity`'s effects
/// for it with empty shadow/annotation context.
fn class_effects(src: &str) -> Vec<Effect> {
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
    classify_class_static_purity(
        class,
        &PurityScope::chunk_top(
            &BTreeSet::new(),
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        ),
    )
    .kinds()
}

#[test]
fn class_decorators_are_observable() {
    // Decorator application CALLS the decorator function at class-definition
    // time: any decorator (class-level or member-level) is observable.
    for src in [
        "@dec class C {}",
        "class C { @dec m() {} }",
        "class C { @dec x = 1; }",
        "class C { @dec accessor x = 1; }",
    ] {
        assert_eq!(class_effects(src), [CLASS_DEFINITION], "{src}");
    }
    // Positive control: same members without decorators.
    assert_eq!(class_effects("class C { m() {} x = 1; }"), NONE);
}

#[test]
fn class_static_auto_accessor_initializer_is_observable() {
    // `static accessor x = <expr>` runs the initializer at class-definition
    // time, like a static field.
    assert_eq!(
        class_effects("class C { static accessor x = io(); }"),
        [CLASS_DEFINITION]
    );
    assert_eq!(class_effects("class C { static accessor x = 1; }"), NONE);
    // Instance auto-accessor initializers are lazy.
    assert_eq!(class_effects("class C { accessor x = io(); }"), NONE);
}

#[test]
fn declared_pure_ident_calls_trust_the_binding_but_classify_arguments() {
    // A spec member with `purity: "pure"` populates the declared-pure set. A
    // call whose callee is the bound Ident has no effects regardless of the
    // body (author trust; the validator does not re-verify), and the
    // annotation wins over the builtin rows' shadowing fallback. The
    // contract covers the function value; argument evaluation is
    // independent.
    let cases: [AnnotatedCase<'_>; 4] = [
        (
            "primitive argument",
            "function f(x) { return x; }",
            "f(42)",
            &["f"],
            &[],
        ),
        (
            "fresh literal argument",
            "function f(x) { return x; }",
            "f({ k: 'v' })",
            &["f"],
            &[],
        ),
        (
            "impure argument",
            "function f(x) { return x; } function io() { return 1; }",
            "f(io())",
            &["f"],
            &[CALL],
        ),
        (
            "overrides global shadowing",
            r#"import { Boolean } from "./userland.js";"#,
            "Boolean(x)",
            &["Boolean"],
            &[],
        ),
    ];
    for (case, prefix, src, declared, expected) in cases {
        assert_eq!(
            classify_with_declared_pure(prefix, src, declared).as_slice(),
            expected,
            "{case}"
        );
    }
}

#[test]
fn declared_pure_new_admits_only_construction() {
    let cases: [PrefixCase<'_>; 2] = [
        (
            "construction with pure args",
            "class PureBox { constructor(value) { globalThis.notAnalyzed = value; } }",
            "new PureBox({ value: 1, later() { globalThis.later = true; } })",
            &[],
        ),
        (
            "plain call is not covered",
            "function PureBox(value) { globalThis.value = value; return { value }; }",
            "PureBox(1)",
            &[CALL],
        ),
    ];
    for (case, prefix, src, expected) in cases {
        assert_eq!(
            classify_with_declared_pure_new(prefix, src, &["PureBox"]).as_slice(),
            expected,
            "{case}"
        );
    }
}

#[test]
fn declared_pure_members_admit_only_static_calls_on_the_named_binding() {
    // `pure_members: [forwardRef]` on `b` admits `<b>.forwardRef(args)` even
    // though the binding's body is unknown (the vendor namespace shape). Args
    // are still classified independently. The trust does not bleed to other
    // props or bindings; computed access `b[expr]` may evaluate to a
    // different property at runtime, breaking the author's contract; `?.`
    // only adds a null/undefined short-circuit.
    let cases: [PrefixCase<'_>; 5] = [
        (
            "annotated member call",
            r#"import * as b from "vendor";"#,
            "b.forwardRef(function () {})",
            &[],
        ),
        (
            "other prop",
            r#"import * as b from "vendor";"#,
            "b.unknownMethod(1)",
            &[CALL],
        ),
        (
            "other binding",
            r#"import * as b from "vendor"; import * as c from "other";"#,
            "c.forwardRef(1)",
            &[CALL],
        ),
        (
            "computed access",
            r#"import * as b from "vendor"; const key = "forwardRef";"#,
            "b[key](1)",
            &[CALL],
        ),
        (
            "optional chain call",
            r#"import * as b from "vendor";"#,
            "b?.forwardRef(1)",
            &[],
        ),
    ];
    for (case, prefix, src, expected) in cases {
        assert_eq!(
            classify_with_declared_pure_members(prefix, src, "b", &["forwardRef"]).as_slice(),
            expected,
            "{case}"
        );
    }
}

#[test]
fn fluent_roots_cover_the_api_but_not_caller_arguments() {
    // The fluent contract follows a chain from an asserted root to any depth:
    // receivers like `.optional()` / `.describe()` are call RESULTS, not
    // bindings, so no binding-keyed arm (`pure_members`, `imported_purities`)
    // can admit them. SOUNDNESS: it covers the API's own functions and their
    // results, NOT caller-supplied argument evaluation. Computed access would
    // additionally require a ToPropertyKey-safe key, so the chain stops there.
    // `const` derivations become roots (zod base-schema reuse); a `let` cell
    // can be rebound to an untrusted value later.
    let cases: [AnnotatedCase<'_>; 9] = [
        (
            "direct root call (zod-style builders are invoked directly)",
            r#"import { e4 as k } from "vendor";"#,
            "k({ a: 1 })",
            &["k"],
            &[],
        ),
        (
            "same call without the assertion",
            r#"import { e4 as k } from "vendor";"#,
            "k({ a: 1 })",
            &[],
            &[CALL],
        ),
        (
            "chained method calls",
            r#"import { e4 as k } from "vendor";"#,
            r#"k.object({ a: 1 }).optional().describe("docs")"#,
            &["k"],
            &[],
        ),
        (
            "impure inner-link argument",
            r#"import { e4 as k } from "vendor"; function io() { globalThis.x = 1; return {}; }"#,
            r#"k.object(io()).describe("docs")"#,
            &["k"],
            &[CALL],
        ),
        (
            "member read on a derived value",
            r#"import { e4 as k } from "vendor";"#,
            r#"k.string().description"#,
            &["k"],
            &[],
        ),
        (
            "computed member",
            r#"import { e4 as k } from "vendor"; const key = "object";"#,
            r#"k[key]({ a: 1 })"#,
            &["k"],
            &[CALL],
        ),
        (
            "optional chaining",
            r#"import { e4 as k } from "vendor";"#,
            r#"k.object({ a: 1 })?.describe?.("docs")"#,
            &["k"],
            &[],
        ),
        (
            "const derivation",
            r#"import { e4 as k } from "vendor"; const S = k.object({ a: 1 });"#,
            r#"S.extend({ b: 2 })"#,
            &["k"],
            &[],
        ),
        (
            "let derivation",
            r#"import { e4 as k } from "vendor"; let S = k.object({ a: 1 });"#,
            r#"S.extend({ b: 2 })"#,
            &["k"],
            &[CALL],
        ),
    ];
    for (case, prefix, src, fluent, expected) in cases {
        assert_eq!(
            classify_with_fluent_bindings(prefix, src, fluent).as_slice(),
            expected,
            "{case}"
        );
    }
}

#[test]
fn fluent_root_body_local_shadow_defeats_trust() {
    // A body-local binding of the same name is a different value than the
    // annotated import; the trust contract doesn't cover it.
    let local_shadowed = BTreeSet::from(["k".to_string()]);
    let effects = classify_last_const(
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
                &BTreeSet::new(),
                &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
            );
            classify_expr_purity(
                init,
                &PurityScope::function_body(shadowed, &local_shadowed, &BTreeSet::new(), &graph),
            )
        },
    );
    assert_eq!(effects, [CALL]);
}

#[test]
fn object_builtins_admit_only_accessor_free_fresh_arguments() {
    let cases: [(&str, &str, &[Effect]); 14] = [
        // `EnumerableOwnPropertyNames` only does `[[OwnPropertyKeys]]`,
        // `[[GetOwnProperty]]` and (for values) `[[Get]]`; a plain literal has
        // only data properties, so no accessor fires.
        ("keys of plain literal", "Object.keys({a: 1, b: 2})", &[]),
        (
            "values of plain literal",
            "Object.values({a: 1, b: 2})",
            &[],
        ),
        (
            "freeze of vite namespace facade",
            r#"Object.freeze(Object.defineProperty(
                { __proto__: null, run: handler },
                Symbol.toStringTag,
                { value: "Module" }
            ))"#,
            &[],
        ),
        (
            "namespace facade on a non-fresh target",
            r#"Object.defineProperty(target, Symbol.toStringTag, { value: "Module" })"#,
            &[CALL],
        ),
        (
            "namespace facade with accessor descriptor",
            r#"Object.defineProperty(
                { __proto__: null, run: handler },
                Symbol.toStringTag,
                { get: () => handler }
            )"#,
            &[CALL],
        ),
        // Admission requires `Prop::KeyValue` / `Prop::Shorthand` only.
        (
            "values of literal with method",
            r#"Object.values({ m() { return 1; } })"#,
            &[CALL],
        ),
        // An arbitrary expression could be a Proxy or carry user accessors.
        (
            "entries of call result",
            "Object.entries(somefn())",
            &[CALL],
        ),
        ("entries of opaque ident", "Object.entries(x)", &[CALL]),
        // `{__proto__: …}` in an object literal sets the prototype, which
        // `is_plain_data_prop` rejects.
        (
            "freeze of literal with __proto__",
            r#"Object.freeze({ __proto__: x, a: 1 })"#,
            &[CALL],
        ),
        // `Object.fromEntries({...})` is a TypeError at runtime (objects
        // aren't iterable); entries that aren't 2-element Array literals don't
        // qualify since the indexed [0]/[1] reads must be own data properties.
        (
            "fromEntries of object",
            r#"Object.fromEntries({a: 1})"#,
            &[CALL],
        ),
        (
            "fromEntries of array-like entry",
            r#"Object.fromEntries([{ "0": "k", "1": "v" }])"#,
            &[CALL],
        ),
        (
            "fromEntries of 3-element entry",
            r#"Object.fromEntries([["a", 1, "extra"]])"#,
            &[CALL],
        ),
        // Single-argument form only; an accidental extra argument falls back
        // rather than being silently admitted.
        (
            "freeze with extra arg",
            r#"Object.freeze({a: 1}, true)"#,
            &[CALL],
        ),
        (
            "keys with extra arg",
            r#"Object.keys({a: 1}, "extra")"#,
            &[CALL],
        ),
    ];
    for (case, src, expected) in cases {
        assert_eq!(classify(src).as_slice(), expected, "{case}");
    }
}

#[test]
fn object_freeze_on_object_with_spread_keeps_the_spread_effect() {
    // The plain-data call rule gates on the whole literal having no effects,
    // and the classifier does not track "fresh-literal source => pure" for
    // spreads.
    assert_each_has_effects(
        &[
            r#"Object.freeze({ ...{a: 1}, b: 2 })"#,
            r#"Object.freeze({ ...src, b: 2 })"#,
        ],
        &[OBJECT_SPREAD],
    );
}

/// The `ChunkCodeGraph` for `structural` with its stable numeric-`var`
/// candidates and no declared-pure, imported or fluent inputs.
fn stable_var_graph(structural: &StructuralChunkAnalysis<'_>) -> ChunkCodeGraph {
    ChunkCodeGraph::build_full(
        &structural.body,
        &structural.shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::new(),
        &structural.stable_var_number_candidates,
        &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
    )
}

/// Classify `expr_src` against a fully built `ChunkCodeGraph` for the
/// wrapping `prefix` module, so chunk-top binding facts — including the
/// primitive binding facts — are populated.
fn classify_built(prefix: &str, expr_src: &str) -> Vec<Effect> {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let structural = analyze_chunk_structural(&module, None);
    let graph = stable_var_graph(&structural);
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(result))) = module.body.last().unwrap() else {
        panic!("expected final initializer");
    };
    classify_expr_purity(
        result.decls[0].init.as_deref().unwrap(),
        &PurityScope::chunk_top(&structural.shadowed, &BTreeSet::new(), &graph),
    )
    .kinds()
}

#[test]
fn chunk_top_binding_facts_gate_coercion_and_plain_data_reads() {
    let cases: [PrefixCase<'_>; 13] = [
        // A chunk-top `const` bound to a primitive is immutable and carries no
        // user accessors, so ToString / ToNumber on a reference fires no user
        // code.
        (
            "primitive const interpolated",
            "const s = \"x\";",
            "`v=${s}`",
            &[],
        ),
        ("primitive const negated", "const n = 5;", "-n", &[]),
        (
            "primitive consts added",
            "const a = 1; const b = 2;",
            "a + b",
            &[],
        ),
        (
            "transitive primitive const",
            "const a = \"x\"; const b = `${a}!`;",
            "`${b}`",
            &[],
        ),
        (
            "primitive const computed key",
            "const k = \"id\";",
            "({ [k]: 1 })",
            &[],
        ),
        // Alias resolution only sees earlier declarations.
        (
            "alias of earlier const",
            "const value = 1; const alias = value;",
            "`${alias}`",
            &[],
        ),
        (
            "alias of later const",
            "const alias = value; const value = 1;",
            "`${alias}`",
            &[COERCE_OPERAND],
        ),
        (
            "alias of later symbol",
            "const alias = brand; const brand = Symbol('brand');",
            "class { [alias]() {} }",
            &[COERCE_KEY],
        ),
        // `let` / `var` can be rebound to an object later.
        (
            "let primitive",
            "let s = \"x\";",
            "`${s}`",
            &[COERCE_OPERAND],
        ),
        (
            "var primitive",
            "var s = \"x\";",
            "`${s}`",
            &[COERCE_OPERAND],
        ),
        // A non-primitive const runs user `toString` / `[Symbol.toPrimitive]`;
        // a const whose init is not statically result-primitive (a bare
        // import/member read) is not admitted either.
        ("object const", "const o = {};", "`${o}`", &[COERCE_OPERAND]),
        (
            "member-read const",
            "const o = globalThis.x;",
            "`${o}`",
            &[COERCE_OPERAND],
        ),
        // `plain` is bound to a plain-data shape: accessor-free because its
        // allocation stays private and clean (`ChunkHeap`). Same admission as
        // a fresh literal.
        (
            "Object.keys of plain-data binding",
            "const plain = { a: 1 };",
            "Object.keys(plain)",
            &[],
        ),
    ];
    for (case, prefix, src, expected) in cases {
        assert_eq!(classify_built(prefix, src).as_slice(), expected, "{case}");
    }
}

#[test]
fn stable_numeric_var_candidates_are_narrow_and_keep_throwing_arithmetic_unknown() {
    let numeric_chain = "var retry = 20000, delay = 15000, base = 5000, period = base, total = delay + period + 2 * retry;";
    assert_eq!(classify_built(numeric_chain, "total + 1"), NONE);
    assert_eq!(classify_built("var base = 2;", "base + 2 * base"), NONE);
    assert_eq!(classify_built("var value = 1;", "`${value}`"), NONE);
    assert_eq!(classify_built("var value = 1;", "value + 'x'"), NONE);
    assert_eq!(
        classify_built("var value = 1;", "value + object"),
        [COERCE_OPERAND]
    );
    assert_eq!(
        classify_built("var value = 1;", "value + (flag ? 1n : 2)"),
        [Effect::Throws]
    );

    let module = parse("var value = 1;");
    let graph = stable_var_graph(&analyze_chunk_structural(&module, None));
    assert!(graph.primitive_bindings.contains("value"));
    assert_eq!(
        graph.binding_prims.kinds.get("value"),
        Some(&PrimKinds::NUMBER.union(PrimKinds::UNDEFINED)),
        "a stable var is Number after its initializer and undefined before it"
    );

    let redeclared_module = parse("var value = 1; function value() {}");
    let redeclared_graph = stable_var_graph(&analyze_chunk_structural(&redeclared_module, None));
    assert!(!redeclared_graph.primitive_bindings.contains("value"));

    for prefix in [
        "var value = 1; value = object;",
        "var value = 1; function replace() { value = object; }",
        "var value = 1; eval('value = object');",
        "var value = 1; var value;",
        "var value = 1n + 1;",
    ] {
        assert_eq!(
            classify_built(prefix, "value + 1"),
            [COERCE_OPERAND],
            "{prefix}"
        );
    }
    // A BigInt operand makes numeric arithmetic throw rather than coerce.
    assert_eq!(
        classify_built("var value = 1;", "value + 1n"),
        [Effect::Throws]
    );
    assert_eq!(
        classify_built(
            "var value = 1; const bigint = 1n; const alias = bigint;",
            "value + alias"
        ),
        [Effect::Throws]
    );
}

#[test]
fn stable_numeric_var_facts_do_not_cross_function_shadowing() {
    let module = parse("var value = 1; function read(value) { return value + 1; }");
    let graph = stable_var_graph(&analyze_chunk_structural(&module, None));
    assert!(
        graph
            .function_purity("read")
            .is_some_and(|purity| !purity.is_pure())
    );
}

/// Classify `expr_src` with a populated cross-module imported-purity
/// map (as the program-level oracle would supply). Each `(name,
/// purity)` records the verdict for an imported function binding.
fn classify_with_imported_purities(
    prefix: &str,
    expr_src: &str,
    imports: &[(&str, Purity)],
) -> Vec<Effect> {
    imported_purity_effects(prefix, expr_src, imports).kinds()
}

fn imported_purity_effects(prefix: &str, expr_src: &str, imports: &[(&str, Purity)]) -> Effects {
    let imported_purities: BTreeMap<String, std::sync::Arc<Purity>> = imports
        .iter()
        .map(|(name, purity)| ((*name).to_string(), std::sync::Arc::new(purity.clone())))
        .collect();
    classify_last_const_effects(prefix, expr_src, |body, shadowed, init| {
        let graph = ChunkCodeGraph::build_full(
            body,
            shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            &imported_purities,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
        );
        classify_expr_purity(
            init,
            &PurityScope::chunk_top(shadowed, &BTreeSet::new(), &graph),
        )
    })
}

fn impure_verdict() -> Purity {
    Effects::of(Effect::Invoke(InvokeSite::Call), swc_common::DUMMY_SP).project()
}

#[test]
fn imported_purity_verdicts_resolve_calls_but_not_arguments() {
    // The program-level oracle resolved each imported function binding. A pure
    // callee makes the call pure when the args are pure; args are classified
    // independently, so a possibly-object operand coerced in one keeps the
    // call impure.
    let cases: [ImportCase<'_>; 4] = [
        ("pure callee", "memo(x)", ("memo", Purity::pure()), &[]),
        (
            "pure callee, function argument",
            "memo(() => {})",
            ("memo", Purity::pure()),
            &[],
        ),
        (
            "impure callee",
            "run(x)",
            ("run", impure_verdict()),
            &[CALL],
        ),
        (
            "pure callee, coercing argument",
            "memo(A + 1)",
            ("memo", Purity::pure()),
            &[COERCE_OPERAND],
        ),
    ];
    for (case, src, import, expected) in cases {
        assert_eq!(
            classify_with_imported_purities("", src, &[import]).as_slice(),
            expected,
            "{case}"
        );
    }
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
            &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph)
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
        &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
    )
    .project();
    assert_eq!(purity.dependencies(), Some(&dependencies));
    assert_eq!(
        classify_var_decl_purity(
            result,
            &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
            &FreshRecordDestructuringContext {
                flow: &ChunkValueFlow::default(),
                dynamic_scope: DynamicScopeFact::default(),
                rebindings: &BTreeSet::new(),
            }
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
        &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
    )
    .project();
    assert_eq!(certified.dependencies(), Some(&dependencies));

    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(independent))) = &module.body[1] else {
        panic!("expected independent Set declaration");
    };
    let independent = classify_expr_purity(
        independent.decls[0].init.as_deref().unwrap(),
        &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
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
        let purity = classify_last_const_effects(
            "const values = [1]; function make() { return new Set(values); }",
            expression,
            |_, _, init| {
                classify_expr_purity(
                    init,
                    &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
                )
            },
        )
        .project();
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
    let class_shapes = purity::LocalClassConstructionShapes::analyze(
        &views,
        &shadowed,
        &flow,
        DynamicScopeFact::analyze(&views),
    );
    let prepared = sync_effects::interpreter::synchronous_calls::prepare_synchronous_call_index(
        &views,
        &flow,
        &analysis_vocabulary::inputs::AnalysisInputs {
            native_integrity: Some(analysis_vocabulary::inputs::NativeIntegrity {
                consumed_value_inputs: dependencies.value_inputs.clone(),
                ..Default::default()
            }),
            ..Default::default()
        },
        &plain_arrays,
        &class_shapes,
        purity::SetIterableShapes::default(),
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
        &BTreeSet::new(),
        None,
    );
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(result))) = module.body.last().unwrap() else {
        panic!("expected final initializer");
    };
    let purity = classify_expr_purity(
        result.decls[0].init.as_deref().unwrap(),
        &PurityScope::chunk_top(&shadowed, &BTreeSet::new(), &graph),
    )
    .project();
    assert!(purity.is_pure());
    assert_eq!(purity.dependencies(), Some(&dependencies));
}

#[test]
fn imported_pure_callee_preserves_conditioned_dependencies() {
    let dependencies = value_input_dependencies("static/provider.js", 4);
    let purity = imported_purity_effects(
        "",
        "memo(x)",
        &[("memo", Purity::pure().with_dependencies(&dependencies))],
    )
    .project();
    assert_eq!(purity.dependencies(), Some(&dependencies));
}

#[test]
fn string_raw_tagged_templates_require_global_tag_and_primitive_substitutions() {
    assert_no_effects(&["String.raw`plain`", "String.raw`value: ${1}`"]);
    assert_eq!(
        classify_built("const value = 1;", "String.raw`${value}`"),
        NONE
    );
    assert_eq!(
        classify("String.raw`${{ toString() { return 'x'; } }}`"),
        [COERCE_OPERAND]
    );
    assert_eq!(classify("String.raw`${unknown}`"), [COERCE_OPERAND]);
    assert_eq!(classify("String.raw`${io()}`"), [CALL, COERCE_OPERAND]);
    assert_eq!(classify("other`plain`"), [TAGGED_TEMPLATE]);
    assert_eq!(classify("String['raw']`plain`"), [TAGGED_TEMPLATE]);
    assert_eq!(
        classify_with_module("const String = local;", "String.raw`plain`"),
        [TAGGED_TEMPLATE]
    );
}
