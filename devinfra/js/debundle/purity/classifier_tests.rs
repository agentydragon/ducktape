use std::collections::{BTreeMap, BTreeSet};

use super::*;
use crate::facts::{compute_shadowed_globals, top_level_item_views};
use swc_common::{FileName, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

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
}

/// Run the classifier against `src` after computing the
/// chunk-top-level shadowed-globals set from a wrapping
/// module. Lets tests check the shadowing fallback.
fn classify_with_module(prefix: &str, expr_src: &str) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(
        init,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &ChunkCodeGraph::default(),
    )
}

/// Run the classifier against `src` with both shadowing and an
/// explicit declared-pure binding set.
fn classify_with_declared_pure(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let declared_pure: BTreeSet<String> = declared.iter().map(|s| (*s).to_string()).collect();
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(
        init,
        &shadowed,
        &BTreeSet::new(),
        &declared_pure,
        &ChunkCodeGraph::default(),
    )
}

fn classify_with_declared_pure_new(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let declared_pure_new: BTreeSet<String> = declared.iter().map(|s| (*s).to_string()).collect();
    let graph = ChunkCodeGraph::build_with_declared_pure_new(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &declared_pure_new,
    );
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
}

fn classify_with_declared_pure_members(
    prefix: &str,
    expr_src: &str,
    binding: &str,
    props: &[&str],
) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let declared_pure_members: BTreeMap<String, BTreeSet<String>> = BTreeMap::from([(
        binding.to_string(),
        props.iter().map(|s| (*s).to_string()).collect(),
    )]);
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &declared_pure_members,
        &BTreeMap::new(),
        &BTreeSet::new(),
    );
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
}

/// Classify `expr_src` (appearing after `prefix` at chunk top) with
/// `fluent` as the author-asserted fluent root bindings.
fn classify_with_fluent_bindings(prefix: &str, expr_src: &str, fluent: &[&str]) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let fluent_bindings: BTreeSet<String> = fluent.iter().map(|s| (*s).to_string()).collect();
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &fluent_bindings,
    );
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
}

#[test]
fn classify_literal_kinds_are_pure() {
    assert!((classify("42")).is_pure());
    assert!((classify("\"hi\"")).is_pure());
    assert!((classify("true")).is_pure());
    assert!((classify("null")).is_pure());
    assert!((classify("/foo/g")).is_pure());
    assert!((classify("`literal`")).is_pure());
}

#[test]
fn classify_ident_read_is_pure() {
    assert!((classify("FOO")).is_pure());
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
fn classify_delete_is_impure() {
    assert!(!(classify("delete o.x")).is_pure());
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
fn classify_sequence_takes_worst() {
    assert!((classify("(A, B, C)")).is_pure());
    assert!(!(classify("(A, foo(), C)")).is_pure());
    assert!(!(classify("(A, x = 1, C)")).is_pure());
}

// --- Whitelist: pure static property reads -------------------------------

#[test]
fn whitelist_static_props_are_pure() {
    // Math / Number / Symbol constants: pure internal-slot
    // reads, no coercion.
    assert!((classify("Math.PI")).is_pure());
    assert!((classify("Math.E")).is_pure());
    assert!((classify("Math.SQRT2")).is_pure());
    assert!((classify("Number.EPSILON")).is_pure());
    assert!((classify("Number.MAX_SAFE_INTEGER")).is_pure());
    assert!((classify("Symbol.iterator")).is_pure());
    assert!((classify("Symbol.toStringTag")).is_pure());
}

#[test]
fn whitelist_misses_fall_back_to_unknown() {
    // Same receivers, properties that aren't on the whitelist:
    // could be a getter / a coercing call. Stays Unknown.
    assert!(!(classify("Math.unknownProp")).is_pure());
    assert!(!(classify("Number.unknownProp")).is_pure());
    assert!(!(classify("Symbol.unknownProp")).is_pure());
}

// --- Whitelist: pure calls -----------------------------------------------

#[test]
fn whitelist_static_calls_are_pure_regardless_of_arg() {
    // Type predicates do not coerce or read user props on the
    // argument, so any Pure-classified arg keeps the call Pure.
    assert!((classify("Array.isArray(x)")).is_pure());
    assert!((classify("Array.isArray([1, 2, 3])")).is_pure());
    assert!((classify("Number.isNaN(x)")).is_pure());
    assert!((classify("Number.isFinite(x)")).is_pure());
    assert!((classify("Number.isInteger(x)")).is_pure());
    assert!((classify("Number.isSafeInteger(x)")).is_pure());
    // Object.is performs SameValue (ECMA-262 §20.1.2.13) with no
    // coercion of either argument — fires no user code on any type.
    assert!((classify("Object.is(a, b)")).is_pure());
}

#[test]
fn whitelist_static_calls_unknown_arg_infects() {
    // An argument whose evaluation may itself fire user code
    // poisons the whole call: even though `Array.isArray` is
    // a pure operation, evaluating `io()` first is not.
    assert!(!(classify("Array.isArray(io())")).is_pure());
    assert!(!(classify("Number.isNaN(o.x)")).is_pure());
}

// --- PURE_STATIC_FUNCTION_REFS: read-vs-call distinction ---------------

#[test]
fn static_function_ref_aliases_are_pure() {
    // Bare member READS access own data properties of the
    // built-in receiver per ECMA-262 — no getter fires, no
    // observable side effect. Aliasing the function value into a
    // binding stays pure (the value isn't called). Table-driven
    // over the whole whitelist so every entry (current and
    // future) carries its positive direction.
    for &(recv, prop) in PURE_STATIC_FUNCTION_REFS {
        let src = format!("{recv}.{prop}");
        assert!(
            classify(&src).is_pure(),
            "expected function-ref read `{src}` to classify Pure"
        );
    }
}

#[test]
fn static_function_ref_calls_remain_unknown() {
    // The CALL form of each function-ref entry is unsafe on
    // arbitrary args (see `PURE_STATIC_FUNCTION_REFS`
    // doc-comment for why each is excluded from
    // `PURE_STATIC_CALLS`). The function-ref entry only opens
    // the read path; the general (opaque-binding-arg) call must
    // stay Unknown so the soundness contract holds. Table-driven
    // over the whole whitelist; entries that are also in
    // `PURE_STATIC_CALLS` (currently `Object.is`) are call-pure
    // by their own admission contract and skipped here.
    //
    // Note: a subset of these calls (`Object.{keys, values,
    // entries, freeze, fromEntries}`) becomes Pure when called
    // with a syntactically plain-data argument — pinned in the
    // `object_*` tests below. The opaque-Ident args used here
    // fall outside that narrow shape.
    for &(recv, prop) in PURE_STATIC_FUNCTION_REFS {
        if PURE_STATIC_CALLS.contains(&(recv, prop)) {
            continue;
        }
        let src = format!("{recv}.{prop}(o, p)");
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
    // `Object` joins WHITELIST_RECEIVERS in this PR; if the
    // chunk shadows it (via a top-level decl OR an import
    // specifier per A8), the function-ref read must fall back
    // to Unknown — `Object.X` then resolves through the
    // user-bound value.
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
fn whitelist_global_callables_are_pure() {
    // Boolean(x) is `ToBoolean(x)`; per spec, no path fires
    // user code (objects → true unconditionally; primitives
    // are case-analysed structurally).
    assert!((classify("Boolean(x)")).is_pure());
    assert!((classify("Boolean(0)")).is_pure());
    assert!((classify("Boolean({})")).is_pure());
}

#[test]
fn unsafe_global_callables_stay_unknown() {
    // ToNumber / ToString / ToPrimitive can call user
    // `valueOf` / `toString` / `[Symbol.toPrimitive]` on
    // object args; we don't track types, so these remain
    // Unknown to keep the whitelist sound.
    assert!(!(classify("Number(x)")).is_pure());
    assert!(!(classify("String(x)")).is_pure());
    assert!(!(classify("Symbol(x)")).is_pure());
    assert!(!(classify("parseInt(x, 10)")).is_pure());
    assert!(!(classify("parseFloat(x)")).is_pure());
    assert!(!(classify("isNaN(x)")).is_pure());
    assert!(!(classify("isFinite(x)")).is_pure());
}

#[test]
fn unsafe_static_calls_stay_unknown() {
    // Anything that coerces / iterates / fires getters /
    // mutates / reads through proxies is *not* on the
    // whitelist. These all stay Unknown.
    for src in [
        "Array.from(x)",
        "Array.of(1, 2, 3)",
        "Math.abs(x)",
        "Math.max(1, 2)",
        "Math.floor(x)",
        "Math.round(x)",
        "Math.sqrt(x)",
        "Object.keys(x)",
        "Object.values(x)",
        "Object.entries(x)",
        "Object.freeze(x)",
        "Object.assign({}, x)",
        "Object.fromEntries(x)",
        "Object.getOwnPropertyDescriptor(x, 'k')",
        "Object.hasOwn(x, 'k')",
        "JSON.parse(x)",
        "JSON.stringify(x)",
        "Number.parseInt(x)",
        "Number.parseFloat(x)",
        "String.fromCharCode(65)",
        "String.fromCodePoint(65)",
        "Symbol.for('k')",
        "Symbol.keyFor(s)",
    ] {
        assert!(
            !(classify(src)).is_pure(),
            "expected {src} to stay Unknown (would fire user code)"
        );
    }
}

// --- Whitelist: shadowing fallback ---------------------------------------

#[test]
fn shadowed_receiver_disables_whitelist() {
    // A chunk-top-level binding for `Math` makes `Math.PI` no
    // longer reach the global; the whitelist must fall back
    // to Unknown.
    assert!(!(classify_with_module("const Math = userland;", "Math.PI")).is_pure());
    assert!(!(classify_with_module("function Math() {}", "Math.E")).is_pure());
    assert!(!(classify_with_module("const Array = X;", "Array.isArray(x)")).is_pure());
    assert!(!(classify_with_module("let Number = X;", "Number.isNaN(x)")).is_pure());
    assert!(!(classify_with_module("const Boolean = X;", "Boolean(x)")).is_pure());
}

#[test]
fn unshadowed_receiver_keeps_whitelist() {
    // A chunk that declares an unrelated binding leaves the
    // whitelist active — only same-named shadowing disables.
    assert!((classify_with_module("const other = userland;", "Math.PI")).is_pure());
}

#[test]
fn import_specifier_locals_shadow_whitelist() {
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

// --- Whitelist: shadow tracking covers every table -----------------------

#[test]
fn unshadowed_builtin_new_is_pure() {
    // Positive control for the shadow-tracking fix: with no
    // chunk-top rebind, the `new <Container>()` whitelists fire.
    assert!((classify("new Map()")).is_pure());
    assert!((classify("new Set()")).is_pure());
    assert!((classify("new Set(['a', 'b'])")).is_pure());
}

#[test]
fn shadowed_builtin_new_falls_back_to_unknown() {
    // SOUNDNESS: `compute_shadowed_globals` must track every name
    // any whitelist table keys on (SHADOW_TRACKED_GLOBALS — the
    // derived union), not just WHITELIST_RECEIVERS. A chunk-top
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
    assert!((classify("class { ['x'] = io(); }")).is_pure()); // instance init is lazy
}

/// Parse a single class declaration with decorators /
/// auto-accessors enabled and run `class_has_static_observable`
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
    class_has_static_observable(
        class,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeSet::new(),
        &ChunkCodeGraph::default(),
    )
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
    // over both the whitelist's shadowing fallback and the
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
fn declared_pure_does_not_bleed_to_unannotated_callees() {
    // Only the listed binding is treated pure. A call to a
    // sibling that wasn't annotated stays subject to the
    // normal classifier path (Unknown for opaque idents).
    assert!(
        !(classify_with_declared_pure(
            "function pure(x) { return x; } function impure(x) { return x; }",
            "impure(x)",
            &["pure"]
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
fn declared_pure_new_requires_pure_args() {
    assert!(
        !(classify_with_declared_pure_new(
            "class PureBox { constructor(value) { this.value = value; } }",
            "new PureBox(makeValue())",
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
fn declared_pure_member_call_with_impure_arg_inherits_arg_purity() {
    // The declared-member-purity contract covers the function
    // value; arg evaluation is independent. An impure arg makes
    // the whole call Unknown.
    assert!(
        !(classify_with_declared_pure_members(
            r#"import * as b from "vendor"; function io() { globalThis.x = 1; return 1; }"#,
            "b.forwardRef(io())",
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
    let module = parse(r#"import { e4 as k } from "vendor";"#);
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeSet::from(["k".to_string()]),
    );
    let shadowing_call = parse(r#"const _ = k.object({ a: 1 });"#);
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = &shadowing_call.body[0] else {
        panic!("expected var decl");
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    let local_shadowed = BTreeSet::from(["k".to_string()]);
    assert!(
        !classify_expr_purity(init, &shadowed, &local_shadowed, &BTreeSet::new(), &graph).is_pure()
    );
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
fn object_entries_on_plain_object_literal_classifies_pure() {
    assert!((classify(r#"Object.entries({a: 1, b: 2})"#)).is_pure());
}

#[test]
fn object_freeze_on_plain_object_literal_classifies_pure() {
    // `Object.freeze({a: 1})` — SetIntegrityLevel does no
    // `[[Get]]`, only rewrites descriptors. Fresh literal has
    // no aliases, so mutation is unobservable from outside the
    // call.
    assert!((classify(r#"Object.freeze({a: 1})"#)).is_pure());
}

#[test]
fn object_keys_on_plain_array_literal_classifies_pure() {
    // Array literals are ordinary objects with integer-index
    // own data properties — same admission as object literals.
    assert!((classify("Object.keys([1, 2, 3])")).is_pure());
}

#[test]
fn object_freeze_on_object_with_getter_stays_unknown() {
    // Getter property would fire `[[Get]]` if subsequently
    // read — and even for freeze, the rule must not admit
    // accessor-carrying literals because the syntactic check
    // is shared with values/entries which do `[[Get]]`. The
    // strict `is_plain_data_prop` predicate rejects getters.
    assert!(!(classify(r#"Object.freeze({ get x() { return 1; } })"#)).is_pure());
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
    // accessor-free by `collect_plain_data_bindings` /
    // `PlainDataWriteScanner` invariants. Same admission as
    // a fresh literal.
    let module = parse("const plain = { a: 1 }; const _ = Object.keys(plain);");
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    assert!(
        classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph).is_pure()
    );
}

#[test]
fn object_from_entries_on_array_of_pair_literals_classifies_pure() {
    // `Object.fromEntries([[k, v], …])` — same admission as
    // `new Map([[k, v], …])`. Array literal of 2-element Array
    // literals with pure values.
    assert!((classify(r#"Object.fromEntries([["a", 1], ["b", 2]])"#)).is_pure());
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
/// primitive-`const` set — are populated.
fn classify_built(prefix: &str, expr_src: &str) -> Purity {
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
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
fn coercion_of_let_or_var_binding_stays_unknown() {
    // `let` / `var` can be rebound to an object later, so the binding is
    // not provably primitive and the coercion stays conservative.
    assert!(!classify_built("let s = \"x\";", "`${s}`").is_pure());
    assert!(!classify_built("var s = \"x\";", "`${s}`").is_pure());
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
    let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let imported_purities: BTreeMap<String, Purity> = imports
        .iter()
        .map(|(name, purity)| ((*name).to_string(), purity.clone()))
        .collect();
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &BTreeSet::new(),
        &BTreeSet::new(),
        &BTreeMap::new(),
        &imported_purities,
        &BTreeSet::new(),
    );
    let var = match module.body.last().expect("non-empty body") {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
    };
    let init = var.decls[0].init.as_deref().expect("init expected");
    classify_expr_purity(init, &shadowed, &BTreeSet::new(), &BTreeSet::new(), &graph)
}

fn impure_verdict() -> Purity {
    Purity::from_reason(PurityRule::UnknownCall, swc_common::DUMMY_SP)
}

#[test]
fn imported_pure_callee_makes_call_pure() {
    // The oracle resolved `memo` (an imported function) as pure, so
    // `memo(arg)` classifies pure when the args are pure.
    assert!(classify_with_imported_purities("", "memo(x)", &[("memo", Purity::Pure)]).is_pure());
    assert!(
        classify_with_imported_purities("", "memo(() => {})", &[("memo", Purity::Pure)]).is_pure()
    );
}

#[test]
fn imported_impure_callee_keeps_call_impure() {
    assert!(!classify_with_imported_purities("", "run(x)", &[("run", impure_verdict())]).is_pure());
}

#[test]
fn imported_callee_without_verdict_stays_unknown() {
    // No oracle entry → the call stays `unknown_call`, today's behavior.
    assert!(!classify_with_imported_purities("", "f(x)", &[]).is_pure());
}

#[test]
fn imported_pure_callee_still_classifies_arguments() {
    // `memo` is pure, but a possibly-object operand coerced in the arg
    // keeps the whole call impure — args are classified independently.
    assert!(
        !classify_with_imported_purities("", "memo(A + 1)", &[("memo", Purity::Pure)]).is_pure()
    );
}
