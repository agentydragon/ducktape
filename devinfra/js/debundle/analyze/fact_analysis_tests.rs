use std::collections::{BTreeMap, BTreeSet};

use crate::analyze_chunk;
use crate::test_support::{
    analyze_facts, analyze_facts_with_hints, hints_with_decorate_helper, test_id,
};
use analysis::facts::EffectCell;
use analysis::{
    AnalysisAssumptions, AnalysisInputs, KnownEffect, LocalEffectPolicy, StatementFacts,
    StatementKind,
};
use raw_js_test_support::{parse, parse_with_source_map};
use swc_ecma_ast::{ArrayLit, Module};
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Default)]
struct HeapArraySites {
    arrays: Vec<u32>,
}

impl Visit for HeapArraySites {
    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.arrays.push(node.span.lo.0);
        node.visit_children_with(self);
    }
}

fn heap_array_sites(module: &Module) -> HeapArraySites {
    let mut sites = HeapArraySites::default();
    module.visit_with(&mut sites);
    sites
}

#[test]
fn parsed_heap_content_collection_preserves_eager_callable_escape_and_allocations() {
    let module = parse(
        "const eagerArray = [];\
         function touch() {\
           const localArray = [];\
           eagerArray.length; eagerArray[0]; eagerArray.push(1);\
           localArray.length; localArray[0]; opaqueCall(localArray);\
         }\
         touch();",
    );
    let sites = heap_array_sites(&module);
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 3);
    assert_eq!(sites.arrays.len(), 2);

    let eager_array = EffectCell::ArrayContents(sites.arrays[0]);
    let local_array = EffectCell::ArrayContents(sites.arrays[1]);

    assert_eq!(
        facts[0].heap_contents.allocations,
        BTreeSet::from([eager_array.clone()])
    );
    assert_eq!(
        facts[0].heap_one_shot_allocations,
        BTreeSet::from([eager_array.clone()])
    );
    assert_eq!(
        facts[1].deferred_heap_allocations,
        BTreeSet::from([local_array.clone()])
    );

    let invoked = &facts[2]
        .synchronous_calls
        .as_ref()
        .expect("the parsed direct call has synchronous effects")
        .heap_contents;
    assert_eq!(invoked.allocations, BTreeSet::from([local_array.clone()]));
    assert_eq!(
        invoked.reads,
        BTreeSet::from([eager_array.clone(), local_array.clone(),])
    );
    assert_eq!(invoked.writes, BTreeSet::from([eager_array, local_array]));
    assert!(invoked.opaque, "unknown-call escape stays conservative");
}

#[test]
fn parsed_async_and_class_heap_content_boundaries_are_preserved() {
    let async_module = parse(
        "const beforeAwait = [];\
         const afterAwait = [];\
         async function later() {\
           beforeAwait.length;\
           await 0;\
           afterAwait.length;\
         }\
         later();",
    );
    let async_sites = heap_array_sites(&async_module);
    let async_facts = analyze_facts(&async_module);
    assert_eq!(async_sites.arrays.len(), 2);
    let before = EffectCell::ArrayContents(async_sites.arrays[0]);
    let after = EffectCell::ArrayContents(async_sites.arrays[1]);
    let async_prefix = &async_facts[3]
        .synchronous_calls
        .as_ref()
        .expect("the parsed async call has a synchronous prefix")
        .heap_contents;
    assert_eq!(async_prefix.reads, BTreeSet::from([before]));
    assert!(!async_prefix.reads.contains(&after));

    let accessor_module = parse(
        "const getterArray = [];\
         const methodArray = [];\
         const object = {\
           get value() { getterArray.length; },\
           write() { methodArray.push(1); },\
         };\
         object.write();\
         const classGetterArray = [];\
         class Holder {\
           field = [];\
           get value() { classGetterArray.length; }\
         }\
         new Holder();",
    );
    let accessor_sites = heap_array_sites(&accessor_module);
    let accessor_facts = analyze_facts(&accessor_module);
    assert_eq!(accessor_sites.arrays.len(), 4);
    let getter_array = EffectCell::ArrayContents(accessor_sites.arrays[0]);
    let method_array = EffectCell::ArrayContents(accessor_sites.arrays[1]);
    let class_getter_array = EffectCell::ArrayContents(accessor_sites.arrays[2]);
    let class_field_array = EffectCell::ArrayContents(accessor_sites.arrays[3]);

    assert!(accessor_facts[2].heap_contents.reads.is_empty());
    let method_effects = &accessor_facts[3]
        .synchronous_calls
        .as_ref()
        .expect("the parsed object method call has synchronous effects")
        .heap_contents;
    assert!(method_effects.reads.contains(&method_array));
    assert!(method_effects.writes.contains(&method_array));
    assert!(!method_effects.reads.contains(&getter_array));

    assert!(
        accessor_facts[5]
            .deferred_heap_allocations
            .contains(&class_field_array)
    );
    let construction_effects = &accessor_facts[6]
        .synchronous_calls
        .as_ref()
        .expect("the parsed class construction has synchronous effects")
        .heap_contents;
    assert!(!construction_effects.reads.contains(&class_getter_array));
}

/// A direct call `f()` at the chunk top level records `f` in the
/// statement's `calls.eager` set. Drives at-init call promotion
/// per docs/design.md "At-init call promotion".
#[test]
fn at_init_call_recorded() {
    let module = parse("function f() {} f();");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 2);
    // function decl: callee never recorded for the decl itself.
    assert!(facts[0].calls.eager.is_empty());
    assert!(facts[0].calls.lazy.is_empty());
    // call statement: f() is at-init, no body reads.
    assert_eq!(facts[1].calls.eager, BTreeSet::from([test_id("f")]),);
    assert!(facts[1].calls.lazy.is_empty());
}

/// A call inside a function body lives in `calls.lazy`, not
/// `calls.eager`. The call only fires when the function is
/// invoked, so promotion treats it as a lazy edge of the
/// containing function.
#[test]
fn body_call_recorded() {
    let module = parse("function f() { g(); } function g() {}");
    let facts = analyze_facts(&module);
    // f's decl: its body calls g lazily.
    assert!(facts[0].calls.eager.is_empty());
    assert_eq!(facts[0].calls.lazy, BTreeSet::from([test_id("g")]),);
}

/// A local alias preserves the function identity for promotion.
#[test]
fn indirect_call_records_known_target() {
    let module = parse("function f() {} const g = f; g();");
    let facts = analyze_facts(&module);
    assert_eq!(facts[2].calls.eager, BTreeSet::from([test_id("f")]));
}

/// Class static field initializers fire at-init (class evaluation
/// time). Calls in static initializers go into `calls.eager`.
#[test]
fn class_static_init_call_is_at_init() {
    let module = parse("function f() {} class C { static x = f(); }");
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].calls.eager, BTreeSet::from([test_id("f")]),);
    assert!(facts[1].calls.lazy.is_empty());
}

/// Class instance field initializers fire per-construction, not
/// at class-decl time. Calls inside them are `calls.lazy`,
/// matching how the existing read collectors treat instance
/// fields as lazy.
#[test]
fn class_instance_field_call_is_lazy() {
    let module = parse("function f() {} class C { x = f(); }");
    let facts = analyze_facts(&module);
    assert!(facts[1].calls.eager.is_empty());
    assert_eq!(facts[1].calls.lazy, BTreeSet::from([test_id("f")]),);
}

/// Nested calls in argument positions are still seen by the
/// collector. `console.log(readB())` records both `console` (in
/// reads.eager) and `readB` (in calls.eager).
#[test]
fn nested_call_arguments_record_inner_callee() {
    let module = parse("function readB() {} console.log(readB());");
    let facts = analyze_facts(&module);
    // The console.log statement: console is an eager read.
    // readB is recorded as an at-init call. console.log is a
    // method call, so it's NOT in calls.eager.
    assert!(facts[1].reads.eager.contains(&test_id("console")));
    assert_eq!(facts[1].calls.eager, BTreeSet::from([test_id("readB")]),);
}

/// VarDecl-bound arrow functions participate in calls.lazy the
/// same way function declarations do. `const f = () => g()` is
/// a function carrier; the `g()` inside is lazy.
#[test]
fn vardecl_arrow_body_call_recorded() {
    let module = parse("function g() {} const f = () => g();");
    let facts = analyze_facts(&module);
    // f's vardecl: g() is a lazy body call.
    assert!(facts[1].calls.eager.is_empty());
    assert_eq!(facts[1].calls.lazy, BTreeSet::from([test_id("g")]),);
}

/// A binding rebind inside a function's immediate body
/// shows up in both `rebinds.lazy` and `rebinds.first_order_lazy`.
/// At-init promotion and the direct `lazy_rebind` dependency edge
/// both read from the first-order subset.
#[test]
fn first_order_lazy_rebind_in_immediate_body() {
    let module = parse("let s = 0; function f() { s = 1; }");
    let facts = analyze_facts(&module);
    // f's decl: body rebinds s at depth 1.
    assert_eq!(facts[1].rebinds.lazy, BTreeSet::from([test_id("s")]));
    assert_eq!(
        facts[1].rebinds.first_order_lazy,
        BTreeSet::from([test_id("s")])
    );
}

/// A binding rebind inside a class method body sits at depth 1
/// (the method's function body is the immediate body, just like
/// a bare `function f() { ... }`). Must show up in
/// `rebinds.first_order_lazy` so the dependency graph emits a
/// constraining `LazyRebind` edge for it.
#[test]
fn first_order_lazy_rebind_in_class_method_body() {
    let module = parse("let counter = 0; class C { bump(b) { counter = b; } }");
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].rebinds.lazy, BTreeSet::from([test_id("counter")]));
    assert_eq!(
        facts[1].rebinds.first_order_lazy,
        BTreeSet::from([test_id("counter")])
    );
}

/// A binding rebind inside a nested closure (depth ≥ 2) shows
/// up in `rebinds.lazy` but NOT in `rebinds.first_order_lazy`
/// — invoking the outer function synchronously only stashes
/// the closure; the rebind doesn't fire. See
/// `e2e/cases/initialization/nested_closure_{read,rebind}`.
#[test]
fn first_order_lazy_rebind_skips_nested_closure() {
    let module = parse(
        "let s = 0; \
         function f() { globalThis.fire = () => { s = 1; }; }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].rebinds.lazy, BTreeSet::from([test_id("s")]));
    assert!(facts[1].rebinds.first_order_lazy.is_empty());
}

/// Same depth distinction for calls: a call in the immediate
/// body fires when the function is invoked, but a call inside
/// a nested closure only fires when the nested closure fires.
#[test]
fn first_order_body_call_skips_nested_closure() {
    let module = parse(
        "function g() {} \
         function f() { globalThis.fire = () => { g(); }; }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].calls.lazy, BTreeSet::from([test_id("g")]));
    assert!(facts[1].calls.first_order_lazy.is_empty());
}

/// A rebind that lexically precedes the first `await` in an async
/// function body runs synchronously when the function is invoked
/// (the engine doesn't suspend until it reaches the await), so it
/// belongs in `rebinds.first_order_lazy`.
#[test]
fn first_order_lazy_rebind_keeps_pre_await_in_async_body() {
    let module = parse(
        "let s = 0; \
         async function f() { s = 1; await Promise.resolve(); }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].rebinds.lazy, BTreeSet::from([test_id("s")]));
    assert_eq!(
        facts[1].rebinds.first_order_lazy,
        BTreeSet::from([test_id("s")])
    );
}

/// A rebind that lexically follows the first `await` in an async
/// function body runs in a microtask after the at-init caller has
/// finished — it doesn't fire synchronously when the function is
/// invoked, so it must not appear in `rebinds.first_order_lazy`.
/// The coarse `rebinds.lazy` still records it (it IS lazy from the
/// chunk's top-level POV).
#[test]
fn first_order_lazy_rebind_skips_after_await_in_async_body() {
    let module = parse(
        "let s = 0; \
         async function f() { await Promise.resolve(); s = 1; }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].rebinds.lazy, BTreeSet::from([test_id("s")]));
    assert!(facts[1].rebinds.first_order_lazy.is_empty());
}

/// Same await-boundary distinction for body calls.
#[test]
fn first_order_body_call_skips_after_await_in_async_body() {
    let module = parse(
        "function g() {} \
         async function f() { await Promise.resolve(); g(); }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].calls.lazy, BTreeSet::from([test_id("g")]));
    assert!(facts[1].calls.first_order_lazy.is_empty());
}

#[test]
fn promise_reaction_on_async_function_call_does_not_sync_promote_handler() {
    let module = parse(
        "async function setup() { await Promise.resolve(); } \
         function handler() {} \
         function boot() { setup().catch(() => handler()); }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(
        facts[2].calls.first_order_lazy,
        BTreeSet::from([test_id("setup")])
    );
    assert!(facts[2].calls.lazy.contains(&test_id("handler")));
    assert!(
        !facts[2]
            .calls
            .first_order_lazy
            .contains(&test_id("handler"))
    );
    assert!(facts[2].first_order_unresolved_sources.is_empty());
    assert!(!facts[2].first_order_unresolved_inline_fn);
}

#[test]
fn arbitrary_member_catch_promotes_inline_callback_body_precisely() {
    let module = parse(
        "function makeThenable() { return { catch(f) { f(); } }; } \
         function handler() {} \
         function boot() { makeThenable().catch(() => handler()); }",
    );
    let facts = analyze_facts(&module);
    assert!(
        facts[2]
            .first_order_unresolved_sources
            .contains(&test_id("makeThenable"))
    );
    assert_eq!(
        facts[2].calls.first_order_lazy,
        BTreeSet::from([test_id("makeThenable"), test_id("handler")])
    );
    assert!(!facts[2].first_order_unresolved_inline_fn);
}

#[test]
fn first_order_unresolved_inline_callback_collects_only_pre_await_effects() {
    let module = parse(
        "function before() {} \
         function after() {} \
         async function boot(items) { \
           items.map(() => before()); \
           await Promise.resolve(); \
           items.map(() => after()); \
         }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(
        facts[2].calls.first_order_lazy,
        BTreeSet::from([test_id("before")])
    );
    assert!(facts[2].calls.lazy.contains(&test_id("after")));
    assert!(
        !facts[2].calls.first_order_lazy.contains(&test_id("after")),
        "post-await inline callbacks must not be treated as synchronously \
         firing from an at-init caller"
    );
    assert!(!facts[2].first_order_unresolved_inline_fn);
}

#[test]
fn parent_call_after_nested_await_is_not_first_order() {
    let module = parse(
        "function after() {} \
         async function boot(store) { \
           await (await store.keys()).reduce(async () => after(), Promise.resolve({})); \
         }",
    );
    let facts = analyze_facts(&module);
    assert!(facts[1].calls.lazy.contains(&test_id("after")));
    assert!(
        !facts[1].calls.first_order_lazy.contains(&test_id("after")),
        "callbacks passed to a call whose receiver resumes after an inner \
         await must not be promoted as first-order"
    );
    assert!(
        !facts[1].first_order_unresolved_inline_fn,
        "the post-await reduce call must not trigger whole-source_unit inline fallback"
    );
}

#[test]
fn no_sync_callback_member_hint_suppresses_at_init_inline_fallback() {
    let module = parse(
        "const later = 'ready'; \
         const state = {}; \
         state.setPending(() => later);",
    );
    let default_facts = analyze_facts(&module);
    assert!(default_facts[2].at_init_unresolved_inline_fn);

    let hints = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            no_sync_callback_members: BTreeMap::from([(
                "state".to_string(),
                BTreeSet::from(["setPending".to_string()]),
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert!(
        facts[2]
            .at_init_unresolved_sources
            .contains(&test_id("state"))
    );
    assert!(
        !facts[2].at_init_unresolved_inline_fn,
        "audited callback-storing member calls should not promote \
         callback bodies as at-init"
    );
    assert!(facts[2].reads.lazy.contains(&test_id("later")));
}

#[test]
fn no_sync_callback_member_hint_does_not_cover_other_members() {
    let module = parse(
        "const later = 'ready'; \
         const state = {}; \
         state.invokeNow(() => later);",
    );
    let hints = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            no_sync_callback_members: BTreeMap::from([(
                "state".to_string(),
                BTreeSet::from(["setPending".to_string()]),
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert!(facts[2].at_init_unresolved_inline_fn);
}

#[test]
fn function_body_reads_are_lazy() {
    let module = parse("function f() { return X; } const Y = 1;");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 2);
    // f() declares "f"; its body reference to X is lazy.
    assert_eq!(facts[0].declared, BTreeSet::from([test_id("f")]));
    assert!(!facts[0].reads.eager.contains(&test_id("f")));
    assert!(!facts[0].reads.eager.contains(&test_id("X")));
    assert_eq!(facts[0].kind, StatementKind::FnDecl);
    // Y declares "Y"; init is `1` (no reads).
    assert_eq!(facts[1].declared, BTreeSet::from([test_id("Y")]));
    assert!(facts[1].reads.eager.is_empty());
}

#[test]
fn function_name_declarations_do_not_read_their_own_binding() {
    for source in [
        "function recurse(value = seed) { return recurse(value); }",
        "export function recurse(value = seed) { return recurse(value); }",
        "export default function recurse(value = seed) { return recurse(value); }",
    ] {
        let facts = analyze_facts(&parse(source));
        assert_eq!(facts.len(), 1, "{source}");
        assert!(facts[0].declares_direct_function, "{source}");
        assert_eq!(facts[0].declared, BTreeSet::from([test_id("recurse")]));
        assert!(
            !facts[0].reads.eager.contains(&test_id("recurse")),
            "{source}"
        );
        assert!(!facts[0].reads.eager.contains(&test_id("seed")), "{source}");
        assert!(
            facts[0].reads.lazy.contains(&test_id("recurse")),
            "{source}"
        );
        assert!(facts[0].reads.lazy.contains(&test_id("seed")), "{source}");
    }

    let facts = analyze_facts(&parse(
        "const outer = function recurse() { return recurse(); };",
    ));
    assert!(!facts[0].reads.eager.contains(&test_id("recurse")));
    assert!(facts[0].reads.lazy.contains(&test_id("recurse")));
}

#[test]
fn class_extends_clause_eager_read() {
    let module = parse("class B extends A { run() { return X; } }");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 1);
    // extends A is eager; method body reference to X is lazy.
    assert!(facts[0].reads.eager.contains(&test_id("A")));
    assert!(!facts[0].reads.eager.contains(&test_id("X")));
}

#[test]
fn computed_key_eager_read() {
    let module = parse("const M = { [k.foo]: 1 };");
    let facts = analyze_facts(&module);
    // The key expression `k.foo` reads `k` at-init.
    assert!(facts[0].reads.eager.contains(&test_id("k")));
}

#[test]
fn class_static_init_eager_read() {
    let module = parse("class C { static x = Y; }");
    let facts = analyze_facts(&module);
    assert!(facts[0].reads.eager.contains(&test_id("Y")));
}

#[test]
fn class_instance_init_is_lazy() {
    let module = parse("class C { x = Y; }");
    let facts = analyze_facts(&module);
    // Instance field initializer evaluates per-instance, not at
    // class-decl time.
    assert!(!facts[0].reads.eager.contains(&test_id("Y")));
}

#[test]
fn class_names_are_bindings_but_class_evaluation_reads_remain_eager() {
    let reads_symbol = |reads: &std::collections::BTreeSet<swc_ecma_ast::Id>, name: &str| {
        reads.iter().any(|(symbol, _)| symbol.as_ref() == name)
    };
    for source in [
        "class Container { read() { return later; } }",
        "export class Container { read() { return later; } }",
        "export default class Container { read() { return later; } }",
    ] {
        let facts = analyze_facts(&parse(source));
        assert_eq!(facts.len(), 1, "{source}");
        assert!(
            !reads_symbol(&facts[0].reads.eager, "Container"),
            "{source}"
        );
        assert!(facts[0].reads.lazy.contains(&test_id("later")), "{source}");
    }

    let facts = analyze_facts(&parse(
        "const Value = class Inner { read() { return Inner; } };",
    ));
    assert!(!reads_symbol(&facts[0].reads.eager, "Inner"));
    assert!(facts[0].reads.lazy.contains(&test_id("Inner")));

    let facts = analyze_facts(&parse(
        "class Container extends Base { [key]() {} static value = initial; }",
    ));
    for binding in ["Base", "key", "initial"] {
        assert!(
            facts[0].reads.eager.contains(&test_id(binding)),
            "{binding}"
        );
    }
}

#[test]
fn annotated_decorate_helper_supports_class_decorator_shape() {
    let module = parse(
        r#"function Ro(decorators, target) {}
const Z = {};
class C {}
Ro([Z], C);"#,
    );
    let facts = analyze_facts_with_hints(&module, &hints_with_decorate_helper("Ro"));
    assert_eq!(facts[3].local_effects, BTreeSet::from([test_id("C")]));
    assert!(facts[3].purity.is_pure());
}

#[test]
fn unannotated_decorate_helper_call_remains_conservative_side_effect() {
    let module = parse(
        r#"function Ro(decorators, target, key, flags) {}
const Z = {};
class C {}
Ro([Z], C.prototype, "x", 2);"#,
    );
    let facts = analyze_facts(&module);
    assert!(facts[3].local_effects.is_empty());
    assert!(!facts[3].purity.is_pure());
}

#[test]
fn annotated_decorate_helper_rejects_dynamic_shapes() {
    let hints = hints_with_decorate_helper("Ro");
    for source in [
        r#"function Ro() {}
const Z = {};
class C {}
Ro([makeDecorator()], C.prototype, "x", 2);"#,
        r#"function Ro() {}
const Z = {};
class C {}
Ro([Z], C["prototype"], "x", 2);"#,
        r#"function Ro() {}
const Z = {};
class C {}
Ro([Z], C.prototype, dynamicKey, 2);"#,
    ] {
        let module = parse(source);
        let facts = analyze_facts_with_hints(&module, &hints);
        let last = facts.last().expect("fixture has a call statement");
        assert!(
            last.local_effects.is_empty(),
            "dynamic decorate helper shape should fall back: {source}"
        );
        assert!(
            !last.purity.is_pure(),
            "dynamic decorate helper shape should retain conservative side-effect purity: {source}"
        );
    }
}

#[test]
fn vendor_prune_policy_records_static_object_local_effects() {
    let module = parse("const EMPTY = {}; Object.freeze(EMPTY);");
    let default_facts = analyze_facts(&module);
    assert!(default_facts[1].local_effects.is_empty());

    let vendor_facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        vendor_facts[1].local_effects,
        BTreeSet::from([test_id("EMPTY")])
    );
    assert!(vendor_facts[1].purity.is_pure());
}

fn analyze_facts_with_policy(module: &Module, policy: LocalEffectPolicy) -> Vec<StatementFacts> {
    let hints = AnalysisInputs {
        local_effect_policy: policy,
        ..AnalysisInputs::default()
    };
    analyze_facts_with_hints(module, &hints)
}

#[test]
fn local_property_writes_policy_handles_comma_sequences_and_nested_paths() {
    // Minified annotation runs comma-join writes; a nested static
    // path (`C.propTypes.id`) still roots at `C`.
    let module = parse(
        r#"function C() { return 1; } function D() { return 2; }
(C.displayName = "C", D.propTypes.id = 1);"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::LocalPropertyWrites);
    assert_eq!(
        facts[2].local_effects,
        BTreeSet::from([test_id("C"), test_id("D")])
    );
    assert!(facts[2].purity.is_pure());
}

#[test]
fn local_property_writes_policy_rejects_disqualifying_shapes() {
    // Each statement must fall back to the conservative path: compound
    // assignment (reads the property — getter risk on a
    // written-through object), impure RHS, computed non-literal key,
    // `__proto__` segment (prototype rewiring, not a data-property
    // effect), and a write through an IMPORT (mutates another module's
    // value — must stay globally ordered).
    let module = parse(
        r#"import { ext } from "./other.js";
function C() { return 1; }
const key = "k";
C.count += 1;
C.x = io();
C[key] = 1;
C.__proto__.hack = 1;
ext.tag = "t";"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::LocalPropertyWrites);
    for (ordinal, label) in [
        (3, "compound assignment"),
        (4, "impure RHS"),
        (5, "computed non-literal key"),
        (6, "__proto__ segment"),
        (7, "import-rooted write"),
    ] {
        assert!(
            facts[ordinal].local_effects.is_empty(),
            "{label} must not be recognized"
        );
        assert!(
            !facts[ordinal].purity.is_pure(),
            "{label} must stay conservatively impure"
        );
    }
}

#[test]
fn local_property_writes_policy_admits_string_literal_computed_key() {
    // A string-literal computed key (`C["k"]`) is a static name —
    // same admission as the vendor recognizers' `static_member_name`.
    let module = parse(r#"function C() { return 1; } C["k"] = 1;"#);
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::LocalPropertyWrites);
    assert_eq!(facts[1].local_effects, BTreeSet::from([test_id("C")]));
    assert!(facts[1].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_intrinsic_alias_local_effects() {
    let module = parse(
        r#"const assign = Object.assign;
function target() {}
assign(target, { deep: true });"#,
    );
    let default_facts = analyze_facts(&module);
    assert!(default_facts[2].local_effects.is_empty());

    let vendor_facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        vendor_facts[2].local_effects,
        BTreeSet::from([test_id("target")])
    );
    assert!(vendor_facts[2].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_var_init_local_effects() {
    let module = parse(
        "function Base() {}\nfunction Derived() {}\nvar proto = (Derived.prototype = new Base());",
    );
    let default_facts = analyze_facts(&module);
    assert!(default_facts[2].local_effects.is_empty());

    let vendor_facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        vendor_facts[2].local_effects,
        BTreeSet::from([test_id("Derived")])
    );
    assert!(vendor_facts[2].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_object_iteration_local_effects() {
    let module = parse(
        r#"const define = Object.defineProperty;
var methods = {
  clear: function () { return this.splice(0); },
  replace: function (items) { return this.splice(0, this.length, items); }
};
function ObservableArray() {}
Object.entries(methods).forEach(function (entry) {
  var key = entry[0], value = entry[1];
  key !== "concat" && define(ObservableArray.prototype, key, value);
});"#,
    );
    let default_facts = analyze_facts(&module);
    assert!(default_facts[3].local_effects.is_empty());

    let vendor_facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        vendor_facts[3].local_effects,
        BTreeSet::from([test_id("ObservableArray")])
    );
    assert!(vendor_facts[3].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_target_first_wrapper_local_effects() {
    let module = parse(
        r#"function define(target, key, value) {
  Object.defineProperty(target, key, { configurable: true, value });
}
var methods = {
  clear: function () { return this.splice(0); },
};
function ObservableArray() {}
Object.entries(methods).forEach(function (entry) {
  var key = entry[0], value = entry[1];
  key !== "concat" && define(ObservableArray.prototype, key, value);
});"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        facts[3].local_effects,
        BTreeSet::from([test_id("ObservableArray")])
    );
    assert!(facts[3].purity.is_pure());
}

#[test]
fn vendor_prune_policy_keeps_unknown_object_iteration_effects_hard() {
    let module = parse(
        r#"var methods = { clear: function () {} };
Object.entries(methods).forEach(function (entry) {
  sideEffect(entry);
});"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert!(facts[1].local_effects.is_empty());
    assert!(!facts[1].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_namespace_iife_local_effects() {
    let module = parse(
        r#"var ns = {};
(function (target) {
  target.reject = wrap("reject");
  function resolve() {}
  target.resolve = resolve;
})(ns || (ns = {}));"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(facts[1].local_effects, BTreeSet::from([test_id("ns")]));
    assert!(facts[1].purity.is_pure());
    assert!(facts[1].reads.lazy.contains(&test_id("wrap")));
}

#[test]
fn vendor_prune_policy_records_complex_namespace_iife_local_effects() {
    let module = parse(
        r#"var scheduler = {};
(function (target) {
  function push(queue, value) {
queue.push(value);
  }
  var tasks = [];
  if (typeof performance == "object") {
target.unstable_now = function () {
  return performance.now();
};
  }
  target.unstable_scheduleCallback = function (task) {
push(tasks, task);
  };
})(scheduler);"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        facts[1].local_effects,
        BTreeSet::from([test_id("scheduler")])
    );
}

#[test]
fn vendor_prune_policy_records_local_binding_writes() {
    let module = parse("let assigned;\nconst source = { value: 1 };\nassigned = source.value;");
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(
        facts[2].local_effects,
        BTreeSet::from([test_id("assigned")])
    );
    assert!(facts[2].purity.is_pure());
}

#[test]
fn vendor_prune_policy_ignores_undeclared_binding_writes() {
    let module = parse("const source = { value: 1 };\nexternal = source.value;");
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert!(facts[1].local_effects.is_empty());
    assert!(!facts[1].purity.is_pure());
}

#[test]
fn vendor_prune_policy_records_commonjs_module_iife_local_effects() {
    let module = parse(
        r#"var module = { exports: {} };
(function (target) {
  (function () {
var has = {}.hasOwnProperty;
function clsx() {}
target.exports ? ((clsx.default = clsx), (target.exports = clsx)) : (window.classNames = clsx);
  })();
})(module);"#,
    );
    let facts = analyze_facts_with_policy(&module, LocalEffectPolicy::VendorPrune);
    assert_eq!(facts[1].local_effects, BTreeSet::from([test_id("module")]));
    assert!(facts[1].purity.is_pure());
}

/// The policy-independent layer of `analyze_chunk` produces identical
/// per-statement structural facts (declared names, at-init/lazy reads,
/// writes, calls, global accesses, dataflow_summarizable, kind) and
/// top-level-await ordinal regardless of `AnalysisInputs`. This is the
/// whole point of the structural-vs-policy split: the structural pass
/// looks at the module text only.
///
/// We exercise this by running the full `analyze_chunk` under two
/// distinct hint sets and comparing the policy-independent subset of
/// each resulting `StatementFacts` row.
#[test]
fn structural_layer_is_policy_independent() {
    let module = parse(
        r#"
import { x } from "./a";
function f() { return g(x); }
function g(z) { return z + 1; }
class C { static m = f(); }
const A = 1, B = 2;
export const E = A + B;
sideEffect(C, E);
"#,
    );

    let hints_default = AnalysisInputs::default();
    let hints_decorate = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            declared_pure: BTreeSet::from(["sideEffect".to_string()]),
            declared_pure_new: BTreeSet::from(["C".to_string()]),
            known_effects: BTreeMap::from([(
                "sideEffect".to_string(),
                KnownEffect::TypescriptDecorateHelper,
            )]),
            ..AnalysisAssumptions::default()
        },
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisInputs::default()
    };

    let a = analyze_chunk(&module, &hints_default, None);
    let b = analyze_chunk(&module, &hints_decorate, None);
    assert_eq!(a.facts.len(), b.facts.len());
    assert_eq!(a.top_level_await, b.top_level_await);

    // The structural fields must be identical across the two hint sets.
    // The policy-dependent fields (local_effects, purity) may differ
    // and are deliberately not compared.
    for (af, bf) in a.facts.iter().zip(b.facts.iter()) {
        assert_eq!(af.ordinal, bf.ordinal);
        assert_eq!(af.kind, bf.kind);
        assert_eq!(af.declared, bf.declared);
        assert_eq!(af.reads.eager, bf.reads.eager);
        assert_eq!(af.rebinds.eager, bf.rebinds.eager);
        assert_eq!(af.reads.lazy, bf.reads.lazy);
        assert_eq!(af.rebinds.lazy, bf.rebinds.lazy);
        assert_eq!(af.reads.first_order_lazy, bf.reads.first_order_lazy);
        assert_eq!(af.rebinds.first_order_lazy, bf.rebinds.first_order_lazy);
        assert_eq!(af.calls.eager, bf.calls.eager);
        assert_eq!(af.calls.lazy, bf.calls.lazy);
        assert_eq!(af.calls.first_order_lazy, bf.calls.first_order_lazy);
        assert_eq!(af.effects().reads, bf.effects().reads);
        assert_eq!(af.effects().writes, bf.effects().writes);
        // `dataflow_summarizable` is deliberately NOT
        // compared: the opaque-at-init-call bail consults the purity
        // classifier (declared-pure hints exempt a call from the
        // bail), so the bit is policy-DEPENDENT by design. The
        // structural bail shapes (`with`, dynamic global keys,
        // member writes, global-object escape taint) remain
        // policy-independent.
    }
}

#[test]
fn eager_class_parts_retain_untrusted_inline_callback_fallback() {
    for source in [
        "class C extends api.run(() => a) {}",
        "class C { static x = api.run(() => a); }",
        "class C { [api.run(() => a)]() {} }",
        "class C { static { api.run(() => a); } }",
    ] {
        let facts = analyze_facts(&parse(source));
        assert!(
            facts[0].at_init_unresolved_inline_fn,
            "{source}: {facts:#?}"
        );
    }
}

#[test]
fn lazy_class_registration_cannot_suppress_an_eager_callback_source() {
    let hints = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            no_sync_callback_members: BTreeMap::from([(
                "api".to_string(),
                BTreeSet::from(["save".to_string()]),
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    };
    for lazy_part in [
        "constructor() { api.save(provider); }",
        "x = api.save(provider);",
    ] {
        let source = format!("class C {{ static {{ api.run(provider); }} {lazy_part} }}");
        let facts = analyze_facts_with_hints(&parse(&source), &hints);
        assert!(
            facts[0]
                .at_init_unresolved_sources
                .contains(&test_id("provider")),
            "{source}: {facts:#?}"
        );
    }
}

/// Regression for the plain-array at-init fallback: a call-free `map`
/// callback that reads an element member can run that element's getter, so
/// the array stays a fallback root. Elements proven primitive, or used only
/// by identity, keep it out.
#[test]
fn plain_array_fallback_keeps_arrays_whose_callbacks_touch_element_members() {
    let facts = analyze_facts(&parse(
        "let later = 0; \
         const items = [{ get label() { return later; } }]; \
         const labels = items.map((item) => item.label); \
         const kept = items.filter((item) => item); \
         const ids = ['a', 'b']; \
         const sizes = ids.map((id) => id.length + `${id}`);",
    ));
    assert!(
        facts[2]
            .at_init_unresolved_sources
            .contains(&test_id("items"))
    );
    assert!(
        !facts[3]
            .at_init_unresolved_sources
            .contains(&test_id("items"))
    );
    assert!(
        !facts[5]
            .at_init_unresolved_sources
            .contains(&test_id("ids"))
    );
}

/// Regression for plain-array exposure: a `forEach` callback receives the
/// array as its third argument and can replace a primitive element with an
/// accessor, so the array no longer counts as primitive-only.
#[test]
fn plain_array_written_through_a_callback_argument_stays_a_fallback_root() {
    let facts = analyze_facts(&parse(
        "let later = 0; \
         const values = [1]; \
         values.forEach((value, index, array) => { array[index] = { get label() { return later; } }; }); \
         const labels = values.map((value) => value.label);",
    ));
    assert!(
        facts[3]
            .at_init_unresolved_sources
            .contains(&test_id("values"))
    );
}

fn has_side_effect_for(src: &str) -> Vec<bool> {
    let module = parse(src);
    analyze_facts(&module)
        .into_iter()
        .map(|f| !f.purity.is_pure())
        .collect()
}

#[test]
fn function_decl_is_not_side_effecting() {
    assert_eq!(
        has_side_effect_for("function f() { return io(); }"),
        vec![false]
    );
}

#[test]
fn class_decl_pure_without_static_init() {
    assert_eq!(
        has_side_effect_for("class C { m() { return io(); } }"),
        vec![false]
    );
    assert_eq!(
        has_side_effect_for("class C { static x = 1; }"),
        vec![false]
    );
    assert_eq!(
        has_side_effect_for("class C { static x = io(); }"),
        vec![true]
    );
    assert_eq!(has_side_effect_for("class C { static {} }"), vec![true]);
}

#[test]
fn immutable_symbol_const_is_an_inert_computed_class_key() {
    assert_eq!(
        has_side_effect_for("const brand = Symbol('brand'); class C { [brand]() {} }"),
        vec![false, false]
    );
}

#[test]
fn bare_expression_classified_by_purity() {
    // Plain ident-read expression statement: pure.
    assert_eq!(has_side_effect_for("X;"), vec![false]);
    // Function call expression statement: side-effecting.
    assert_eq!(has_side_effect_for("io();"), vec![true]);
}

#[test]
fn multi_declarator_var_decl_is_side_effecting_if_any_init_is() {
    // After the comma-list pre-split, a multi-declarator
    // var-decl becomes one row per declarator. So a
    // mixed-purity comma-list produces both a Pure row and
    // an Impure row, not a single conservative row.
    assert_eq!(
        has_side_effect_for("const A = 1, B = compute();"),
        vec![false, true]
    );
    assert_eq!(
        has_side_effect_for("const A = 1, B = 2, C = 3;"),
        vec![false, false, false]
    );
}

fn statement_kinds(source: &str) -> Vec<StatementKind> {
    let module = parse(source);
    analyze_facts(&module).into_iter().map(|f| f.kind).collect()
}

#[test]
fn non_var_decl_statements_are_not_split() {
    // function / class declarations have no comma-list shape.
    // Mixed source: const + function + class + bare expression.
    assert_eq!(
        statement_kinds("const A = 1; function f() {} class C {} 'side-effecting-string';"),
        vec![
            StatementKind::VarDecl,
            StatementKind::FnDecl,
            StatementKind::ClassDecl,
            StatementKind::SideEffect,
        ]
    );
}

#[test]
fn split_comma_list_assigns_per_declarator_source_ranges() {
    // Each post-split single-declarator source unit must report just its
    // declarator's line range, not the parent statement's (for
    // `export const`, not the `ExportDecl` wrapper's). `AtomicGroupReport`'s
    // `size_lines_estimate` and `source_line_range` are built from these
    // per-unit ranges.
    for source in [
        "const A = 1,\n      B = 2,\n      C = 3;\n",
        "export const A = 1,\n             B = 2,\n             C = 3;\n",
    ] {
        let (module, cm) = parse_with_source_map(source);
        let line_index = js_ast::SourceLineIndex::for_source_map(&cm);
        let analysis = analyze_chunk(
            &module,
            &AnalysisInputs::default(),
            Some(analysis::execution_evidence::SourceLocator {
                source_path: "test.js",
                lines: &line_index,
            }),
        );
        let lines: Vec<(usize, usize)> = analysis
            .facts
            .iter()
            .map(|f| {
                let loc = f
                    .source_location
                    .as_ref()
                    .expect("source_location should be populated");
                (loc.start_line, loc.end_line)
            })
            .collect();
        assert_eq!(
            lines,
            vec![(1, 1), (2, 2), (3, 3)],
            "each declarator should report only its own line for {source:?}",
        );
    }
}
