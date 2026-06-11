use std::collections::{BTreeMap, BTreeSet};

use crate::*;
use swc_common::{FileName, SyntaxContext, sync::Lrc};
use swc_ecma_ast::*;
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

/// Construct an `Id` for a test fixture binding using
/// `SyntaxContext::empty()`. Real chunks would use the chunk's
/// `top_level_mark` via `ids::top_level_id`, but tests don't run
/// through resolver so they use the empty context uniformly.
fn test_id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
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

fn hints_with_decorate_helper(name: &str) -> AnalysisHints {
    AnalysisHints {
        declared_pure: BTreeSet::new(),
        declared_pure_new: BTreeSet::new(),
        declared_pure_members: BTreeMap::new(),
        known_effects: BTreeMap::from([(name.to_string(), KnownEffect::TypescriptDecorateHelper)]),
        local_effect_policy: LocalEffectPolicy::KnownEffectsOnly,
    }
}

fn analyze_facts_with_hints(module: &Module, hints: &AnalysisHints) -> Vec<StatementFacts> {
    analyze_chunk(module, hints, None, |_| None).facts
}

fn analyze_facts(module: &Module) -> Vec<StatementFacts> {
    analyze_facts_with_hints(module, &AnalysisHints::default())
}

/// A direct call `f()` at the chunk top level records `f` in the
/// statement's `at_init_calls` set. Drives at-init call promotion
/// per docs/design.md "At-init call promotion".
#[test]
fn at_init_call_recorded() {
    let module = parse("function f() {} f();");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 2);
    // function decl: callee never recorded for the decl itself.
    assert!(facts[0].at_init_calls.is_empty());
    assert!(facts[0].body_calls.is_empty());
    // call statement: f() is at-init, no body reads.
    assert_eq!(facts[1].at_init_calls, BTreeSet::from([test_id("f")]),);
    assert!(facts[1].body_calls.is_empty());
}

/// A call inside a function body lives in `body_calls`, not
/// `at_init_calls`. The call only fires when the function is
/// invoked, so promotion treats it as a lazy edge of the
/// containing function.
#[test]
fn body_call_recorded() {
    let module = parse("function f() { g(); } function g() {}");
    let facts = analyze_facts(&module);
    // f's decl: its body calls g lazily.
    assert!(facts[0].at_init_calls.is_empty());
    assert_eq!(facts[0].body_calls, BTreeSet::from([test_id("g")]),);
}

/// Indirect calls (`const g = f; g()`) are skipped — the callee
/// isn't a direct Ident on the CallExpr. Conservative: the
/// proposer may miss promotion through this case.
#[test]
fn indirect_call_not_recorded() {
    let module = parse("function f() {} const g = f; g();");
    let facts = analyze_facts(&module);
    // Last statement: `g()` records `g`, not `f`. (The aliasing
    // is unmodeled; callee resolution only sees `g`.)
    assert_eq!(facts[2].at_init_calls, BTreeSet::from([test_id("g")]),);
}

/// Method calls (`obj.method()`) are skipped — callee is a
/// MemberExpr, not an Ident.
#[test]
fn method_call_not_recorded() {
    let module = parse("const obj = {}; obj.method();");
    let facts = analyze_facts(&module);
    // Last statement: no at_init_calls. `obj` is still recorded
    // as an eager read.
    assert!(facts[1].at_init_calls.is_empty());
    assert!(facts[1].eager_reads.contains(&test_id("obj")));
}

/// Class static field initializers fire at-init (class evaluation
/// time). Calls in static initializers go into `at_init_calls`.
#[test]
fn class_static_init_call_is_at_init() {
    let module = parse("function f() {} class C { static x = f(); }");
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].at_init_calls, BTreeSet::from([test_id("f")]),);
    assert!(facts[1].body_calls.is_empty());
}

/// Class instance field initializers fire per-construction, not
/// at class-decl time. Calls inside them are `body_calls`,
/// matching how the existing read collectors treat instance
/// fields as lazy.
#[test]
fn class_instance_field_call_is_lazy() {
    let module = parse("function f() {} class C { x = f(); }");
    let facts = analyze_facts(&module);
    assert!(facts[1].at_init_calls.is_empty());
    assert_eq!(facts[1].body_calls, BTreeSet::from([test_id("f")]),);
}

/// Nested calls in argument positions are still seen by the
/// collector. `console.log(readB())` records both `console` (in
/// eager_reads) and `readB` (in at_init_calls).
#[test]
fn nested_call_arguments_record_inner_callee() {
    let module = parse("function readB() {} console.log(readB());");
    let facts = analyze_facts(&module);
    // The console.log statement: console is an eager read.
    // readB is recorded as an at-init call. console.log is a
    // method call, so it's NOT in at_init_calls.
    assert!(facts[1].eager_reads.contains(&test_id("console")));
    assert_eq!(facts[1].at_init_calls, BTreeSet::from([test_id("readB")]),);
}

/// VarDecl-bound arrow functions participate in body_calls the
/// same way function declarations do. `const f = () => g()` is
/// a function carrier; the `g()` inside is lazy.
#[test]
fn vardecl_arrow_body_call_recorded() {
    let module = parse("function g() {} const f = () => g();");
    let facts = analyze_facts(&module);
    // f's vardecl: g() is a lazy body call.
    assert!(facts[1].at_init_calls.is_empty());
    assert_eq!(facts[1].body_calls, BTreeSet::from([test_id("g")]),);
}

/// A binding rebind inside a function's immediate body
/// shows up in both `lazy_rebinds` and `first_order_lazy_rebinds`.
/// At-init promotion and the direct `lazy_rebind` owner edge
/// both read from the first-order subset.
#[test]
fn first_order_lazy_rebind_in_immediate_body() {
    let module = parse("let s = 0; function f() { s = 1; }");
    let facts = analyze_facts(&module);
    // f's decl: body rebinds s at depth 1.
    assert_eq!(facts[1].lazy_rebinds, BTreeSet::from([test_id("s")]));
    assert_eq!(
        facts[1].first_order_lazy_rebinds,
        BTreeSet::from([test_id("s")])
    );
}

/// A binding rebind inside a class method body sits at depth 1
/// (the method's function body is the immediate body, just like
/// a bare `function f() { ... }`). Must show up in
/// `first_order_lazy_rebinds` so the owner-graph emits a
/// constraining `LazyRebind` edge for it.
#[test]
fn first_order_lazy_rebind_in_class_method_body() {
    let module = parse("let counter = 0; class C { bump(b) { counter = b; } }");
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].lazy_rebinds, BTreeSet::from([test_id("counter")]));
    assert_eq!(
        facts[1].first_order_lazy_rebinds,
        BTreeSet::from([test_id("counter")])
    );
}

/// A binding rebind inside a nested closure (depth ≥ 2) shows
/// up in `lazy_rebinds` but NOT in `first_order_lazy_rebinds`
/// — invoking the outer function synchronously only stashes
/// the closure; the rebind doesn't fire. See
/// `at_init_promotion_nested_closure_test`.
#[test]
fn first_order_lazy_rebind_skips_nested_closure() {
    let module = parse(
        "let s = 0; \
         function f() { globalThis.fire = () => { s = 1; }; }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].lazy_rebinds, BTreeSet::from([test_id("s")]));
    assert!(facts[1].first_order_lazy_rebinds.is_empty());
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
    assert_eq!(facts[1].body_calls, BTreeSet::from([test_id("g")]));
    assert!(facts[1].first_order_body_calls.is_empty());
}

/// A rebind that lexically precedes the first `await` in an async
/// function body runs synchronously when the function is invoked
/// (the engine doesn't suspend until it reaches the await), so it
/// belongs in `first_order_lazy_rebinds`.
#[test]
fn first_order_lazy_rebind_keeps_pre_await_in_async_body() {
    let module = parse(
        "let s = 0; \
         async function f() { s = 1; await Promise.resolve(); }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].lazy_rebinds, BTreeSet::from([test_id("s")]));
    assert_eq!(
        facts[1].first_order_lazy_rebinds,
        BTreeSet::from([test_id("s")])
    );
}

/// A rebind that lexically follows the first `await` in an async
/// function body runs in a microtask after the at-init caller has
/// finished — it doesn't fire synchronously when the function is
/// invoked, so it must not appear in `first_order_lazy_rebinds`.
/// The coarse `lazy_rebinds` still records it (it IS lazy from the
/// chunk's top-level POV). See `at_init_promotion_post_await_test`.
#[test]
fn first_order_lazy_rebind_skips_after_await_in_async_body() {
    let module = parse(
        "let s = 0; \
         async function f() { await Promise.resolve(); s = 1; }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].lazy_rebinds, BTreeSet::from([test_id("s")]));
    assert!(facts[1].first_order_lazy_rebinds.is_empty());
}

/// Same await-boundary distinction for body calls.
#[test]
fn first_order_body_call_skips_after_await_in_async_body() {
    let module = parse(
        "function g() {} \
         async function f() { await Promise.resolve(); g(); }",
    );
    let facts = analyze_facts(&module);
    assert_eq!(facts[1].body_calls, BTreeSet::from([test_id("g")]));
    assert!(facts[1].first_order_body_calls.is_empty());
}

#[test]
fn function_body_reads_are_lazy() {
    let module = parse("function f() { return X; } const Y = 1;");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 2);
    // f() declares "f"; its body reference to X is lazy.
    assert_eq!(facts[0].declared, BTreeSet::from([test_id("f")]));
    assert!(!facts[0].eager_reads.contains(&test_id("X")));
    assert_eq!(facts[0].kind, StatementKind::FnDecl);
    // Y declares "Y"; init is `1` (no reads).
    assert_eq!(facts[1].declared, BTreeSet::from([test_id("Y")]));
    assert!(facts[1].eager_reads.is_empty());
}

#[test]
fn class_extends_clause_eager_read() {
    let module = parse("class B extends A { run() { return X; } }");
    let facts = analyze_facts(&module);
    assert_eq!(facts.len(), 1);
    // extends A is eager; method body reference to X is lazy.
    assert!(facts[0].eager_reads.contains(&test_id("A")));
    assert!(!facts[0].eager_reads.contains(&test_id("X")));
}

#[test]
fn computed_key_eager_read() {
    let module = parse("const M = { [k.foo]: 1 };");
    let facts = analyze_facts(&module);
    // The key expression `k.foo` reads `k` at-init.
    assert!(facts[0].eager_reads.contains(&test_id("k")));
}

#[test]
fn class_static_init_eager_read() {
    let module = parse("class C { static x = Y; }");
    let facts = analyze_facts(&module);
    assert!(facts[0].eager_reads.contains(&test_id("Y")));
}

#[test]
fn class_instance_init_is_lazy() {
    let module = parse("class C { x = Y; }");
    let facts = analyze_facts(&module);
    // Instance field initializer evaluates per-instance, not at
    // class-decl time.
    assert!(!facts[0].eager_reads.contains(&test_id("Y")));
}

#[test]
fn annotated_decorate_helper_call_records_target_local_effect_not_global_sequence() {
    let module = parse(
        r#"console.log("boot");
function Ro(decorators, target, key, flags) {}
const Z = {};
class C {}
Ro([Z.shallow], C.prototype, "x", 2);
console.log("tail");"#,
    );
    let facts = analyze_facts_with_hints(&module, &hints_with_decorate_helper("Ro"));
    assert_eq!(facts[4].local_effects, BTreeSet::from([test_id("C")]));
    assert!(facts[4].purity.is_pure());

    let graph = build_owner_graph(&facts);
    let local_effects: Vec<_> = graph
        .iter_edges()
        .filter(|edge| edge.reason.kind == DepKind::LocalEffect)
        .collect();
    assert_eq!(local_effects.len(), 1);
    assert_eq!(local_effects[0].from, OwnerId(4));
    assert_eq!(local_effects[0].to, OwnerId(3));
    assert_eq!(
        local_effects[0]
            .reason
            .binding
            .as_ref()
            .map(|id| id.0.as_ref()),
        Some("C"),
    );
    assert!(
        graph.iter_edges().all(|edge| {
            edge.reason.kind != DepKind::Sequenced
                || (edge.from != OwnerId(4) && edge.to != OwnerId(4))
        }),
        "recognized decorate helper must not participate in unrelated global S edges: {:#?}",
        graph.iter_edges().collect::<Vec<_>>(),
    );
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

    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let vendor_facts = analyze_facts_with_hints(&module, &hints);
    assert_eq!(
        vendor_facts[1].local_effects,
        BTreeSet::from([test_id("EMPTY")])
    );
    assert!(vendor_facts[1].purity.is_pure());
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

    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let vendor_facts = analyze_facts_with_hints(&module, &hints);
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

    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let vendor_facts = analyze_facts_with_hints(&module, &hints);
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

    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let vendor_facts = analyze_facts_with_hints(&module, &hints);
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
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
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
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
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
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert_eq!(facts[1].local_effects, BTreeSet::from([test_id("ns")]));
    assert!(facts[1].purity.is_pure());
    assert!(facts[1].lazy_reads.contains(&test_id("wrap")));
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
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert_eq!(
        facts[1].local_effects,
        BTreeSet::from([test_id("scheduler")])
    );
}

#[test]
fn vendor_prune_policy_records_local_binding_writes() {
    let module = parse("let assigned;\nconst source = { value: 1 };\nassigned = source.value;");
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert_eq!(
        facts[2].local_effects,
        BTreeSet::from([test_id("assigned")])
    );
    assert!(facts[2].purity.is_pure());
}

#[test]
fn vendor_prune_policy_ignores_undeclared_binding_writes() {
    let module = parse("const source = { value: 1 };\nexternal = source.value;");
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
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
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert_eq!(facts[1].local_effects, BTreeSet::from([test_id("module")]));
    assert!(facts[1].purity.is_pure());
}

/// The policy-independent layer of `analyze_chunk` produces identical
/// per-statement structural facts (declared names, at-init/lazy reads,
/// writes, calls, global accesses, dataflow_summarizable, kind) and
/// top-level-await ordinal regardless of `AnalysisHints`. This is the
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

    let hints_default = AnalysisHints::default();
    let hints_decorate = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        declared_pure: BTreeSet::from(["sideEffect".to_string()]),
        declared_pure_new: BTreeSet::from(["C".to_string()]),
        known_effects: BTreeMap::from([(
            "sideEffect".to_string(),
            KnownEffect::TypescriptDecorateHelper,
        )]),
        ..AnalysisHints::default()
    };

    let a = analyze_chunk(&module, &hints_default, None, |_| None);
    let b = analyze_chunk(&module, &hints_decorate, None, |_| None);
    assert_eq!(a.facts.len(), b.facts.len());
    assert_eq!(a.top_level_await, b.top_level_await);

    // The structural fields must be identical across the two hint sets.
    // The policy-dependent fields (local_effects, purity) may differ
    // and are deliberately not compared.
    for (af, bf) in a.facts.iter().zip(b.facts.iter()) {
        assert_eq!(af.ordinal, bf.ordinal);
        assert_eq!(af.kind, bf.kind);
        assert_eq!(af.declared, bf.declared);
        assert_eq!(af.eager_reads, bf.eager_reads);
        assert_eq!(af.eager_rebinds, bf.eager_rebinds);
        assert_eq!(af.lazy_reads, bf.lazy_reads);
        assert_eq!(af.lazy_rebinds, bf.lazy_rebinds);
        assert_eq!(af.first_order_lazy_reads, bf.first_order_lazy_reads);
        assert_eq!(af.first_order_lazy_rebinds, bf.first_order_lazy_rebinds);
        assert_eq!(af.at_init_calls, bf.at_init_calls);
        assert_eq!(af.body_calls, bf.body_calls);
        assert_eq!(af.first_order_body_calls, bf.first_order_body_calls);
        assert_eq!(af.effects.reads, bf.effects.reads);
        assert_eq!(af.effects.writes, bf.effects.writes);
        assert_eq!(
            af.effects.dataflow_summarizable,
            bf.effects.dataflow_summarizable
        );
    }
}
