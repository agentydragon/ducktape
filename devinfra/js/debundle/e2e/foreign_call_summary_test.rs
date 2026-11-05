//! Derived foreign call summaries (SPEC.md § Derived foreign call summaries):
//! a provider export whose body only allocates is no unknown callee at the
//! importer; anything that can run other code stays conservative.

use analysis::analysis_provenance::InferenceRule;
use analysis::execution_evidence::{GlobalReachCause, HavocReach};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn assert_foreign_record_reach(fixture: &Fixture, expected: bool) {
    let graph = fixture.dependency_graph();
    let reached = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|execution| &execution.precision_losses)
        .any(|loss| {
            loss.reach
                == HavocReach::Global {
                    cause: GlobalReachCause::ForeignRecord,
                }
        });
    assert_eq!(reached, expected);
}

#[test]
fn stable_primitive_provider_captures_do_not_retain_foreign_callbacks() {
    for (initializer, expected) in [
        ("'marker'", "string\n"),
        ("Symbol.for('fixture.marker')", "symbol\n"),
    ] {
        let provider = format!(
            "var captured = {initializer}; export function make() {{ return {{ value: captured }}; }}"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { make } from './peer.js'; const result = make(); console.log(typeof result.value);",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let summary = &report["imported_callables"]["make"]["foreign_call"];
        assert!(summary.is_object(), "{report:#}");
        assert!(
            summary["retained_provider_captures"].is_null(),
            "{summary:#}"
        );
        assert_foreign_record_reach(&fixture, false);
    }
}

#[test]
fn unproved_provider_capture_kinds_retain_foreign_callbacks() {
    for (case, prefix) in [
        ("callback", "var captured = () => 7;"),
        (
            "reassigned",
            "var captured = Symbol.for('fixture.marker'); captured = () => 7;",
        ),
        (
            "shadowed-native",
            "var Symbol = { for() { return () => 7; } }; var captured = Symbol.for('fixture.marker');",
        ),
        (
            "mutated-native",
            "Symbol.for = function() { return () => 7; }; var captured = Symbol.for('fixture.marker');",
        ),
        (
            "erased-assignment",
            "var source = () => 7; var captured = (source ||= 'marker');",
        ),
        (
            "redeclared",
            "var captured = 'marker'; var captured = () => 7;",
        ),
    ] {
        let provider =
            format!("{prefix} export function make() {{ return {{ value: captured }}; }}");
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { make } from './peer.js'; const result = make(); console.log(result.value());",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, "7\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let summary = &report["imported_callables"]["make"]["foreign_call"];
        assert_eq!(
            summary["retained_provider_captures"],
            serde_json::json!(["captured"]),
            "{case}: {summary:#}"
        );
        assert_foreign_record_reach(&fixture, true);
    }
}

#[test]
fn provider_capture_kinds_preserve_ancestor_initialization_and_hoisting() {
    const ASSIGN: &str = "api.make = function() { return { value: captured }; };";
    for (body, expected, retained) in [
        (
            format!("var captured = 'marker'; {ASSIGN}"),
            "string\n",
            false,
        ),
        (
            format!("{ASSIGN} return; var captured = 'marker';"),
            "undefined\n",
            false,
        ),
        (
            format!(
                "{ASSIGN} return; var captured = 'marker'; function captured() {{ return 7; }}"
            ),
            "function\n",
            true,
        ),
        (
            format!("for (var captured of [() => 7]) {{}} {ASSIGN}"),
            "function\n",
            true,
        ),
    ] {
        let provider =
            format!("var api = {{}}; function setup() {{ {body} }} setup(); export {{ api }};");
        let policy = [ChunkExportPurityBuilder::new("static/peer")
            .with_namespace_independent_members("api", &["make"])
            .with_source_callable_members("api", &["make"])
            .build()];
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { api as remote } from './peer.js'; const result = remote.make(); console.log(typeof result.value);",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", &provider)])
            .with_chunk_export_purity(&policy),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let summary = &report["imported_callables"]["remote"]["foreign_members"]["make"];
        assert!(summary.is_object(), "{report:#}");
        assert_eq!(
            summary["retained_provider_captures"].is_array(),
            retained,
            "{body}: {summary:#}"
        );
        assert_foreign_record_reach(&fixture, retained);
    }
}

const BOX: &str = "export class Box {\n  #callback;\n  #cache = new WeakMap();\n  constructor(callback, label) { this.#callback = callback; }\n  invoke() { return this.#callback(); }\n}\n";

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn reject(source: &str, provider: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, modules()).with_extra_chunks(&[("static/peer", provider)]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn derived_constructor_retains_its_callback_until_instance_use() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Box } from './peer.js';\nconst wrapped = new Box(() => later, 'label');\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", BOX)]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["Box"]["foreign_call"];
    assert_eq!(summary["invocation"], "construct");
    assert_eq!(summary["retained_arguments"], serde_json::json!([0]));
    assert_eq!(
        summary["native_operations"],
        serde_json::json!([{"invocation": "construct", "intrinsic": "weak_map"}])
    );
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .expect("wrapped owner");
    let execution = wrapped
        .synchronous_execution
        .as_ref()
        .expect("synchronous execution");
    assert!(execution.complete);
    assert!(
        execution
            .inference_uses
            .iter()
            .any(|usage| usage.conclusion == InferenceRule::ForeignCallSummary)
    );
    let purity = serde_json::to_value(&wrapped.purity).unwrap();
    assert_eq!(purity["kind"], "not_pure");
}

#[test]
fn class_fields_keep_typed_empty_keyed_collection_constructions() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Collections } from './peer.js';\nconst collections = new Collections();\nconsole.log(collections instanceof Collections);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export class Collections { #map = new Map(); #set = new Set(); #weakMap = new WeakMap(); #weakSet = new WeakSet(); }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "true\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["Collections"]["foreign_call"];
    assert_eq!(summary["invocation"], "construct");
    assert_eq!(
        summary["native_operations"],
        serde_json::json!([
            {"invocation": "construct", "intrinsic": "map"},
            {"invocation": "construct", "intrinsic": "set"},
            {"invocation": "construct", "intrinsic": "weak_map"},
            {"invocation": "construct", "intrinsic": "weak_set"},
        ])
    );
}

#[test]
fn callable_factories_use_shared_empty_keyed_collection_transfers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { makeCache, makeRecord } from './peer.js';\nfunction localCache() { return new Map(); }\nconst cache = makeCache();\nconst record = makeRecord();\nconsole.log(cache instanceof Map, cache.size, record.nested.cache instanceof Map, record.nested.cache.size, localCache().size);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function makeCache() { const cache = new Map(); return cache; }\nexport function makeRecord() { const cache = new Map(); return { nested: { cache } }; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "true 0 true 0 0\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    for name in ["makeCache", "makeRecord"] {
        let summary = &report["imported_callables"][name]["foreign_call"];
        assert_eq!(
            summary["returns"],
            serde_json::json!([{"kind": "fresh_allocation"}])
        );
        assert_eq!(summary["retained_arguments"], serde_json::json!([]));
        assert_eq!(
            summary["native_operations"],
            serde_json::json!([{"invocation": "construct", "intrinsic": "map"}])
        );
    }
    assert!(fixture.dependency_graph().nodes.iter().any(|node| {
        node.synchronous_execution
            .as_ref()
            .is_some_and(|execution| {
                execution
                    .inference_uses
                    .iter()
                    .any(|usage| usage.conclusion == InferenceRule::ForeignCallSummary)
            })
    }));
}

#[test]
fn regexp_literal_factories_return_fresh_allocations_without_protocol_shape() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { direct, localBound, nested } from './peer.js';\n\
             const first = direct();\nconst second = direct();\n\
             const local = localBound();\nconst record = nested();\n\
             const nestedPattern = record.nested.pattern;\n\
             console.log(first !== second, first.test('a'), first.lastIndex, second.lastIndex, first.test('a'), first.lastIndex, local.test('b'), local.lastIndex, nestedPattern.test('c'), nestedPattern.lastIndex);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "const RegExp = () => { throw new Error('mutable RegExp binding called'); };\n\
             export function direct() { return /a/g; }\n\
             export function localBound() { const pattern = /b/g; return pattern; }\n\
             export function nested() { const pattern = /c/g; return { nested: { pattern } }; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "true true 1 0 false 0 true 1 true 1\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    for name in ["direct", "localBound", "nested"] {
        let summary = &report["imported_callables"][name]["foreign_call"];
        assert_eq!(
            summary["returns"],
            serde_json::json!([{"kind": "fresh_allocation"}])
        );
        assert_eq!(summary["retained_arguments"], serde_json::json!([]));
        assert_eq!(summary["native_operations"], serde_json::json!([]));
    }
}

#[test]
fn regexp_literal_summary_keeps_global_binding_and_prototype_behavior_live() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { literal, construct } from './peer.js';\n\
             globalThis.constructorCalls = 0;\n\
             globalThis.getterCalls = 0;\n\
             globalThis.coercionCalls = 0;\n\
             const originalRegExp = globalThis.RegExp;\n\
             Object.defineProperty(originalRegExp.prototype, 'test', { configurable: true, get() { globalThis.getterCalls++; return function() { return 'getter'; }; } });\n\
             Object.defineProperty(originalRegExp.prototype, Symbol.toPrimitive, { configurable: true, value() { globalThis.coercionCalls++; return 'coerced'; } });\n\
             globalThis.RegExp = function(pattern, flags) { globalThis.constructorCalls++; return new originalRegExp(pattern, flags); };\n\
             const pattern = literal();\n\
             const constructed = construct();\n\
             globalThis.RegExp = originalRegExp;\n\
             console.log(pattern.test('x'), String(pattern), Object.getPrototypeOf(pattern) === originalRegExp.prototype, constructed instanceof originalRegExp, globalThis.getterCalls, globalThis.coercionCalls, globalThis.constructorCalls);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function literal() { return /x/g; }\n\
             export function construct() { return new RegExp('x', 'g'); }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "getter coerced true true 1 1 1\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let literal = &report["imported_callables"]["literal"]["foreign_call"];
    assert_eq!(
        literal["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(literal["native_operations"], serde_json::json!([]));
    assert!(
        report["imported_callables"]["construct"]["foreign_call"].is_null(),
        "a constructor call does not use the RegExp-literal transfer"
    );
}

#[test]
fn regexp_literal_does_not_bypass_unmigrated_method_record_metadata() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { mixed } from './peer.js';\n\
             const result = mixed();\n\
             const first = result.pattern();\n\
             const second = result.pattern();\n\
             console.log(first !== second, first.test('x'), first.lastIndex, second.lastIndex);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function mixed() { return { pattern() { return /x/g; } }; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "true true 1 0\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["mixed"];
    assert_eq!(
        summary["foreign_call"]["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(summary["returned_record"]["members"][0]["name"], "pattern");
}

#[test]
fn callable_native_constructor_refusals_keep_node_behavior() {
    for (provider, entry, expected) in [
        (
            "export function makeCache() { const cache = new Map([[1, 'value']]); return cache; }\n",
            "try { console.log(makeCache().get(1)); } catch (error) { console.log(error.name); }\n",
            "value\n",
        ),
        (
            "export function makeCache() { const cache = new Map(); const Map = null; return cache; }\n",
            "try { makeCache(); } catch (error) { console.log(error.name); }\n",
            "ReferenceError\n",
        ),
        (
            "const NativeMap = Map;\nglobalThis.Map = class extends NativeMap {};\nexport function makeCache() { const cache = new Map(); return cache; }\n",
            "console.log(makeCache() instanceof Map);\n",
            "true\n",
        ),
    ] {
        let source = format!("import {{ makeCache }} from './peer.js';\n{entry}");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(
            report["imported_callables"]["makeCache"]["foreign_call"].is_null(),
            "refused provider must not publish a summary: {provider}"
        );
    }
}

#[test]
fn class_constructor_does_not_treat_a_getter_as_a_data_field() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { GetterReader } from './peer.js';\nglobalThis.getterCalls = 0;\nconst reader = new GetterReader();\nconsole.log(globalThis.getterCalls, reader.invoke());\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export class GetterReader { #read; constructor() { const record = { get value() { globalThis.getterCalls++; return () => 42; } }; this.#read = record.value; } invoke() { return this.#read(); } }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "1 42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert!(
        report["imported_callables"]["GetterReader"]["foreign_call"].is_null(),
        "an accessor read is not a data-field read"
    );
}

#[test]
fn a_constructor_returning_this_keeps_the_legacy_admission_boundary() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { SelfReturning } from './peer.js';\nconst value = new SelfReturning();\nconsole.log(value instanceof SelfReturning);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export class SelfReturning { constructor() { return this; } }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "true\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert!(
        report["imported_callables"]["SelfReturning"]["foreign_call"].is_null(),
        "the former class evaluator rejected object-valued constructor returns"
    );
}

#[test]
fn a_public_field_write_keeps_the_constructor_unknown() {
    reject(
        "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
        "export class Box { constructor(callback) { this.callback = callback; } invoke() { return this.callback(); } }\n",
    );
}

#[test]
fn builder_entry_defers_the_callbacks_in_its_input() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { object } from './peer.js';\nconst wrapped = object({ read: () => later }, 'note');\nconst later = 42;\nconsole.log(wrapped.shape.read());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function object(shape, description) { const kind = 'object'; return { kind, shape }; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn central_direct_return_migration_preserves_literal_input_retention() {
    // Migration controls: these supported cases also pass the former strict
    // source evaluator. They assert the external runtime and summary contract.
    for (returned, projection) in [
        ("{ value }", "wrapped.value()"),
        ("[value]", "wrapped[0]()"),
        ("{ nested: [{ value }] }", "wrapped.nested[0].value()"),
    ] {
        let source = format!(
            "import {{ make }} from './peer.js';\nconst wrapped = make(() => later, () => later);\nconst later = 42;\nconsole.log({projection});\n"
        );
        let provider = format!("export function make(value, unused) {{ return {returned}; }}\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, modules()).with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert_eq!(
            report["imported_callables"]["make"]["foreign_call"]["retained_arguments"],
            serde_json::json!([0])
        );
    }
}

#[test]
fn shared_local_transfer_preserves_local_bound_returned_closure_and_formal_retention() {
    let source = "import { make } from './peer.js';\n\
                  const wrapped = make(() => later, () => 7);\n\
                  const later = 42;\n\
                  console.log(wrapped());\n";
    let provider = "export function make(callback, unused) {\n\
                     const local = { callback };\n\
                     const wrapped = () => local.callback();\n\
                     return wrapped;\n\
                   }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, modules()).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "42\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let callable = &report["imported_callables"]["make"];
    assert_eq!(
        callable["foreign_call"]["retained_arguments"],
        serde_json::json!([0, 1]),
        "the returned closure conservatively retains every formal"
    );
    assert_eq!(
        callable["foreign_call"]["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(
        callable["callback_factory_evidence"],
        serde_json::json!([]),
        "local-bound closures keep generic retention metadata without widening direct-wrapper inference"
    );
}

#[test]
fn returned_closure_can_resolve_provider_module_bindings_without_callback_evidence() {
    let source = "import { makeRest, makeZero } from './peer.js';\n\
                  const rest = makeRest((value) => value + 1, 'unused');\n\
                  const zero = makeZero(() => 42, 'unused');\n\
                  console.log(rest(41), zero());\n";
    let provider = "function moduleCheck(value) { return value !== false; }\n\
                    export function makeRest(callback, unused) {\n\
                      return (...args) => {\n\
                        const value = callback(...args);\n\
                        return moduleCheck(value) ? value : undefined;\n\
                      };\n\
                    }\n\
                    export function makeZero(callback, unused) {\n\
                      return () => moduleCheck(callback()) ? 'ok' : 'no';\n\
                    }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "42 ok\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    for name in ["makeRest", "makeZero"] {
        let callable = &report["imported_callables"][name];
        assert_eq!(
            callable["foreign_call"]["returns"],
            serde_json::json!([{"kind": "fresh_allocation"}]),
            "module-scope reads remain deferred for {name}"
        );
        assert_eq!(
            callable["foreign_call"]["retained_arguments"],
            serde_json::json!([0, 1]),
            "a returned closure conservatively retains every formal: {name}"
        );
        assert_eq!(
            callable["callback_factory_evidence"],
            serde_json::json!([]),
            "a module-scope dependency does not prove deferred callback behavior: {name}"
        );
    }
}

#[test]
fn returned_closure_can_read_a_destructured_catch_binding() {
    let source = "import { make } from './peer.js';\n\
                  const wrapped = make(42, () => { throw Error('unused callback'); });\n\
                  console.log(wrapped(42), wrapped(false));\n";
    let provider = "export function make(value, unused) {\n\
                     return (input) => {\n\
                       try { throw { observed: input }; }\n\
                       catch ({ observed }) { return observed === value; }\n\
                     };\n\
                   }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "true false\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let callable = &report["imported_callables"]["make"];
    assert_eq!(
        callable["foreign_call"]["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(
        callable["foreign_call"]["retained_arguments"],
        serde_json::json!([0, 1]),
        "the returned closure conservatively retains every formal"
    );
    assert_eq!(
        callable["callback_factory_evidence"],
        serde_json::json!([]),
        "a catch-local read does not establish callback factory behavior"
    );
}

#[test]
fn returned_closure_destructuring_locals_stay_deferred() {
    let source = "import { make } from './peer.js';\n\
                  let reads = 0;\n\
                  const input = { get value() { reads++; return 42; } };\n\
                  const wrapped = make();\n\
                  console.log(reads, wrapped(input), reads);\n";
    let provider = "export function make() {\n\
                     return (input) => {\n\
                       const { value } = input;\n\
                       return value;\n\
                     };\n\
                   }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "0 42 1\n");

    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let callable = &report["imported_callables"]["make"];
    assert_eq!(
        callable["foreign_call"]["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(
        callable["foreign_call"]["retained_arguments"],
        serde_json::json!([])
    );
    assert_eq!(callable["callback_factory_evidence"], serde_json::json!([]));
}

#[test]
fn returned_closure_nested_patterns_keep_formals_and_callback_policy() {
    let source = "import { make } from './peer.js';\n\
                  const wrapped = make(() => 42, () => { throw Error('unused'); });\n\
                  console.log(...wrapped({ nested: {}, items: [7, 8] }));\n";
    let provider = "export function make(callback, unused) {\n\
                     const wrapped = (input) => {\n\
                       const { nested: { value = callback() }, ...rest } = input;\n\
                       const [head, ...tail] = rest.items;\n\
                       return [value, head, tail.length];\n\
                     };\n\
                     return wrapped;\n\
                   }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "42 7 1\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let callable = &report["imported_callables"]["make"];
    assert_eq!(
        callable["foreign_call"]["returns"],
        serde_json::json!([{"kind": "fresh_allocation"}])
    );
    assert_eq!(
        callable["foreign_call"]["retained_arguments"],
        serde_json::json!([0, 1])
    );
    assert_eq!(callable["callback_factory_evidence"], serde_json::json!([]));
}

#[test]
fn destructuring_in_factory_or_class_closure_keeps_existing_refusals() {
    let source = "import { make, Box } from './peer.js';\n\
                  const wrapped = make({ value: 42 });\n\
                  const box = new Box();\n\
                  console.log(wrapped(), box.invoke({ value: 7 }));\n";
    let provider = "export function make(input) {\n\
                     const { value } = input;\n\
                     return () => value;\n\
                   }\n\
                   export class Box {\n\
                     #callback;\n\
                     constructor() {\n\
                       this.#callback = (input) => { const { value } = input; return value; };\n\
                     }\n\
                     invoke(input) { return this.#callback(input); }\n\
                   }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "42 7\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    for name in ["make", "Box"] {
        assert!(report["imported_callables"][name]["foreign_call"].is_null());
    }
}

#[test]
fn strict_expression_returns_preserve_runtime_values_and_retained_arguments() {
    for (returned, use_result, expected, retained) in [
        (
            "{ type: input, compare: compare === void 0 ? null : compare }",
            "console.log(make(() => 42).compare === null); const value = make(() => 42, () => 7); console.log(value.type(), value.compare());",
            "true\n42 7\n",
            vec![0, 1],
        ),
        (
            "{ chosen: input ? null : compare }",
            "console.log(make(false, () => 42).chosen()); console.log(make(true, () => 7).chosen);",
            "42\nnull\n",
            vec![1],
        ),
        (
            "{ value: input === compare }",
            "const value = { get hidden() { throw Error('getter'); }, [Symbol.toPrimitive]() { throw Error('coercion'); } }; console.log(make(value, value).value, make(value, {}).value);",
            "true false\n",
            vec![],
        ),
        (
            "input !== compare",
            "const value = { [Symbol.toPrimitive]() { throw Error('coercion'); } }; console.log(make(value, value), make(value, {}));",
            "false true\n",
            vec![],
        ),
        (
            "void input",
            "let calls = 0; console.log(make(() => ++calls) === undefined, calls);",
            "true 0\n",
            vec![],
        ),
    ] {
        let source = format!("import {{ make }} from './peer.js';\n{use_result}\n");
        let provider = format!("export function make(input, compare) {{ return {returned}; }}\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let summary = &report["imported_callables"]["make"]["foreign_call"];
        assert!(!summary.is_null(), "strict return: {returned}");
        assert_eq!(summary["retained_arguments"], serde_json::json!(retained));
        assert!(
            fixture.dependency_graph().nodes.iter().any(|node| node
                .synchronous_execution
                .as_ref()
                .is_some_and(|execution| execution
                    .inference_uses
                    .iter()
                    .any(|usage| usage.conclusion == InferenceRule::ForeignCallSummary))),
            "summary must be consumed: {returned}"
        );
    }
}

#[test]
fn strict_expression_children_keep_observable_effects_and_throws() {
    for (returned, setup, expected) in [
        (
            "input == 1",
            "const input = { valueOf() { ++globalThis.calls; return 1; } };",
            "true 1\n",
        ),
        (
            "+input",
            "const input = { valueOf() { ++globalThis.calls; return 1; } };",
            "1 1\n",
        ),
        (
            "input.value === 1",
            "const input = { get value() { ++globalThis.calls; return 1; } };",
            "true 1\n",
        ),
        (
            "void input()",
            "const input = () => { ++globalThis.calls; };",
            "undefined 1\n",
        ),
        (
            "input() ? null : input",
            "const input = () => { ++globalThis.calls; return true; };",
            "null 1\n",
        ),
        (
            "input ? input() : null",
            "const input = () => { ++globalThis.calls; return 7; };",
            "7 1\n",
        ),
        ("true ? input : input()", "const input = 7;", "7 0\n"),
        (
            "void (globalThis.calls++, input)",
            "const input = 7;",
            "undefined 1\n",
        ),
        (
            "input.value === 1",
            "const input = { get value() { ++globalThis.calls; throw new RangeError(); } };",
            "RangeError 1\n",
        ),
        (
            "missing === input",
            "const input = 7;",
            "ReferenceError 0\n",
        ),
    ] {
        let source = format!(
            "import {{ make }} from './peer.js';\nglobalThis.calls = 0;\n{setup}\ntry {{ console.log(make(input), globalThis.calls); }} catch (error) {{ console.log(error.name, globalThis.calls); }}\n"
        );
        let provider = format!("export function make(input) {{ return {returned}; }}\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(
            report["imported_callables"]["make"]["foreign_call"].is_null(),
            "effectful return: {returned}"
        );
    }
}

#[test]
fn erased_or_uncovered_provider_operations_do_not_get_a_strict_summary() {
    // Conservative coverage controls, not baseline-failing regressions. In
    // particular, a global store can be absent from SynchronousCalls despite
    // a complete outcome; the full canonical body must still reject it.
    for (provider, observed) in [
        (
            "export function make(input) { globalThis.flag = 1; return { input }; }",
            1,
        ),
        (
            "export function make(input) { return (globalThis.flag = 1, { input }); }",
            1,
        ),
        (
            "export function make(input) { return { value: (globalThis.flag = 1) }; }",
            1,
        ),
        ("export function make(input) { return `${input}`; }", 0),
        ("export function make(input) { return +input; }", 0),
        (
            "export function make(input) { return input ? { input } : {}; }",
            0,
        ),
        (
            "export function make(input) { return { value: input.value }; }",
            0,
        ),
        ("export function make(input = {}) { return { input }; }", 0),
        ("export function make({ value }) { return { value }; }", 0),
        ("export async function make(input) { return { input }; }", 0),
        ("export function* make(input) { return { input }; }", 0),
        ("export function make(input) { throw input; }", 0),
        (
            "export function make(input) { try { return { input }; } catch { return {}; } }",
            0,
        ),
        ("export function make(input) { return { [input]: 1 }; }", 0),
        ("export function make(input) { return { ...input }; }", 0),
        (
            "export function make(input) { return { __proto__: input }; }",
            0,
        ),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new("import { make } from './peer.js';\nglobalThis.flag = 0;\ntry { make(1); } catch {}\nconsole.log(globalThis.flag);\n", vec![])
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, &format!("{observed}\n"));
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(
            report["imported_callables"]["make"]["foreign_call"].is_null(),
            "uncovered provider: {provider}"
        );
    }
}

// Preservation controls for live retained inputs. Existing broad fallback can
// already preserve their ordering; these are not baseline-failing regressions.
fn assert_live_retained_input(setup: &str, mutation: &str, use_result: &str) {
    let provider = format!(
        "export function wrap(value) {{ return {{ value }}; }}\nexport function use(box) {{ return {use_result}; }}\n"
    );
    let source = format!(
        r#"import {{ wrap, use }} from './peer.js';
function read() {{ try {{ return later; }} catch (error) {{ return error.name; }} }}
{setup}
const wrapped = wrap(input);
{mutation}
const observed = use(wrapped);
const later = 42;
console.log(observed);
"#
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(&[("static/peer", provider.as_str())])
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    let graph = fixture.dependency_graph();
    let observed = source_unit_for_binding(&graph, "observed");
    let later = source_unit_for_binding(&graph, "later");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == observed
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.constrains_init_order
    }));
}

#[test]
fn returned_result_preserves_live_input_aliases() {
    assert_live_retained_input(
        "const input = {}; const alias = input;",
        "alias.read = read;",
        "box.value.read()",
    );
}

#[test]
fn returned_result_preserves_cyclic_input_reach() {
    assert_live_retained_input(
        "const input = {}; input.self = input;",
        "input.self.read = read;",
        "box.value.self.read()",
    );
}

#[test]
fn returned_result_keeps_array_insertion_conservative() {
    assert_live_retained_input("const input = [];", "input.push(read);", "box.value[0]()");
}

#[test]
fn returned_result_keeps_map_key_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Map();",
        "input.set(read, 1);",
        "[...box.value.keys()][0]()",
    );
}

#[test]
fn returned_result_keeps_map_value_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Map();",
        "input.set('read', read);",
        "box.value.get('read')()",
    );
}

#[test]
fn returned_result_keeps_set_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Set();",
        "input.add(read);",
        "[...box.value][0]()",
    );
}

#[test]
fn equal_retained_inputs_do_not_prove_result_identity_or_shape() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { wrap } from './peer.js';\nconst input = {};\nconst first = wrap(input);\nconst second = wrap(input);\nconst selected = first.value;\nconsole.log(first === second, selected === second.value);\n",
            vec![],
        )
        .with_extra_chunks(&[("static/peer", "export function wrap(value) { return { value }; }\n")]),
    );
    assert_preserved_entry_output(&fixture, "false true\n");
    let graph = fixture.dependency_graph();
    let selected = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "selected")
        })
        .expect("selected owner");
    assert!(
        !selected
            .synchronous_execution
            .as_ref()
            .expect("execution")
            .complete,
        "an opaque returned carrier must not prove an ordinary own-data read"
    );
}

#[test]
fn opaque_result_does_not_certify_an_audited_factory_input() {
    let source = r#"import { wrap, outer } from './peer.js';
const input = { get read() { try { return later; } catch (error) { return error.name; } } };
const intermediate = wrap(input);
const wrapped = outer(intermediate);
const later = 42;
console.log(wrapped.value);
"#;
    let provider = "export function wrap(value) { return { value }; }\nexport function outer(value) { return { value: value.value.read }; }\n";
    let assertion = ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("outer", 1, &[0])
        .build();
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&[("static/peer", provider)])
            .with_chunk_export_purity(std::slice::from_ref(&assertion))
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    // The outer contract excludes accessors in its input graph. An opaque
    // foreign result does not establish that eligibility condition.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("input"),
                        Member::new("intermediate"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[assertion]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_method_on_the_summarized_result_stays_unknown() {
    // The value model has no foreign object identity, so `.describe` is an
    // unknown member call that may run the retained callback.
    reject(
        "import { object } from './peer.js';\nconst wrapped = object({ read: () => later }).describe('note');\nconst later = 42;\nconsole.log(wrapped.shape.read());\n",
        "export function object(shape) { return { shape, describe(text) { return this; } }; }\n",
    );
}

#[test]
fn identity_returns_its_argument_and_drops_the_other() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { keep } from './peer.js';\nconst wrapped = keep({ read: () => later }, () => later);\nconst later = 42;\nconsole.log(wrapped.read());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function keep(value, reset) { return value; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn a_provider_that_may_invoke_its_callback_stays_unknown() {
    reject(
        "import { untracked } from './peer.js';\nconst wrapped = untracked(() => later, false);\nconst later = 42;\nconsole.log(wrapped, later);\n",
        "export function untracked(callback, run) { if (run) callback(); return 0; }\n",
    );
}

#[test]
fn a_provider_reading_exported_state_stays_unknown() {
    // An importer can replace `registry.lazy` before the call.
    reject(
        "import { lazy } from './peer.js';\nconst wrapped = lazy(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
        "export const registry = { lazy(e) { return () => e(); } };\nexport function lazy(e) { return registry.lazy(e); }\n",
    );
}

#[test]
fn a_vendor_marked_provider_keeps_its_summary() {
    let vendor_marks = [(
        "static/peer.js",
        VendorMark {
            identity: "replaced-provider".into(),
            role: Default::default(),
            level: VendorLevel::Suppress,
        },
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", BOX)])
        .with_vendor_marks(&vendor_marks),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn initializer_call_and_record_summary_requires_an_isolated_provider() {
    for (prefix, available) in [("", true), ("const unrelated = 0;\n", false)] {
        let provider = format!(
            "{prefix}export const make = function () {{ const state = new Map(); return {{ get size() {{ return state.size; }} }}; }};\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { make } from './peer.js';\nconst queue = make();\nconsole.log(queue.size);\n",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, "0\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let summary = &report["imported_callables"]["make"];
        assert_eq!(summary["foreign_call"].is_object(), available, "{summary}");
        assert_eq!(
            summary["returned_record"].is_object(),
            available,
            "{summary}"
        );
    }
}

#[test]
fn deferred_descriptor_creation_migration_controls() {
    for (provider, entry, expected, retained, native, members) in [
        (
            "export function make() { let stored = 0; return { get value() { globalThis.events.push('get'); return stored; }, set value(next) { globalThis.events.push('set'); stored = next; }, run() { return this.value; }, tag: 'record' }; }",
            "globalThis.events = []; const record = make(); const descriptor = Object.getOwnPropertyDescriptor(record, 'value'); console.log(events.length, typeof descriptor.get, typeof descriptor.set, Object.hasOwn(descriptor, 'value')); record.value = 7; console.log(record.run(), events.join(','));",
            "0 function function false\n7 set,get\n",
            vec![],
            vec![],
            vec![],
        ),
        (
            "export function make() { const state = new Map(); return { put(key, value) { state.set(key, value); }, get size() { return state.size; }, async settle() { return Promise.all([...state.values()]); }, *values() { yield* state.values(); } }; }",
            "const record = make(); record.put('key', 42); record.settle().then(values => console.log(record.size, [...record.values()].join(','), values.join(',')));",
            "1 42 42\n",
            vec![],
            vec!["map"],
            vec!["put", "size", "settle", "values"],
        ),
        (
            "export function make() { return { read() { return super.value + this.offset; } }; }",
            "const record = make(); Object.setPrototypeOf(record, { value: 40 }); const detached = record.read; console.log(detached.call({ offset: 2 })); Object.setPrototypeOf(record, { value: 41 }); console.log(detached.call({ offset: 2 }));",
            "42\n43\n",
            vec![],
            vec![],
            vec!["read"],
        ),
        (
            "export function make(callback, unused) { return [, callback, () => callback(), { run() { return callback(); } }]; }",
            "const record = make(() => 42, () => { throw Error('unused'); }); console.log(Object.hasOwn(record, 0), Object.hasOwn(record, 1), record[2](), record[3].run());",
            "false true 42 42\n",
            vec![0, 1],
            vec![],
            vec![],
        ),
        (
            "export function make(callback, unused) { const label = ``; var repeated = 1; var repeated; return function recur(count) { return count ? recur(count - 1) : callback() + label.length + repeated; }; const unreachable = 9; }",
            "console.log(make(() => 41, () => { throw Error('unused'); })(2));",
            "42\n",
            vec![0, 1],
            vec![],
            vec![],
        ),
        (
            "export function make(callback, unused) { return { run() { return callback(); }, run: 7, get value() { return callback(); }, set value(next) {}, get value() { return 42; } }; }",
            "const record = make(() => 1, () => { throw Error('unused'); }); console.log(record.run, record.value, typeof Object.getOwnPropertyDescriptor(record, 'value').set);",
            "7 42 function\n",
            vec![0, 1],
            vec![],
            vec![],
        ),
    ] {
        let source = format!("import {{ make }} from './peer.js';\n{entry}\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        let callable = &report["imported_callables"]["make"];
        assert_eq!(
            callable["foreign_call"]["returns"],
            serde_json::json!([{"kind": "fresh_allocation"}]),
            "{provider}"
        );
        assert_eq!(
            callable["foreign_call"]["retained_arguments"],
            serde_json::json!(retained),
            "{provider}"
        );
        assert_eq!(
            callable["foreign_call"]["native_operations"],
            serde_json::json!(native.iter().map(|intrinsic| serde_json::json!({"invocation": "construct", "intrinsic": intrinsic})).collect::<Vec<_>>())
        );
        assert_eq!(callable["callback_factory_evidence"], serde_json::json!([]));
        if members.is_empty() {
            assert!(callable["returned_record"].is_null());
        } else {
            let actual: Vec<_> = callable["returned_record"]["members"]
                .as_array()
                .unwrap()
                .iter()
                .map(|member| member["name"].as_str().unwrap())
                .collect();
            assert_eq!(actual, members);
        }
    }
}

#[test]
fn executable_prefix_retains_unreachable_pattern_and_loop_bindings() {
    for declaration in [
        "var { value } = { value: 42 };",
        "for (var [value] of [[42]]) {}",
    ] {
        let provider = format!(
            "export function make() {{ return {{ read() {{ return value; }} }}; {declaration} }}"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { make } from './peer.js'; console.log(make().read() === undefined);",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, "true\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert_eq!(
            report["imported_callables"]["make"]["foreign_call"]["returns"],
            serde_json::json!([{"kind": "fresh_allocation"}])
        );
    }
    for (declaration, expected) in [
        ("const { value } = { value: 42 };", "ReferenceError\n"),
        ("let [value] = [42];", "ReferenceError\n"),
    ] {
        let provider = format!("export function make() {{ return value; {declaration} }}");
        let fixture = run_fixture(
            FixtureOpts::new("import { make } from './peer.js'; try { make(); } catch (error) { console.log(error.name); }", vec![])
                .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(report["imported_callables"]["make"]["foreign_call"].is_null());
    }
}

#[test]
fn repeated_sparse_factory_keeps_holes_and_old_alias_contents() {
    let fixture = run_fixture(FixtureOpts::new(
        "function make(value) { return [, value]; } const early = make(1); early[0] = 7; const late = make(2); const values = new Set(late); console.log(early[0], Object.hasOwn(late, 0), values.has(undefined), [...late].join(','));",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "7 false true ,2\n");
}

#[test]
fn dead_native_shadow_cannot_borrow_global_constructor_identity() {
    for (declaration, expected) in [
        ("const Map = globalThis.Map;", "ReferenceError\n"),
        ("var { Map } = globalThis;", "TypeError\n"),
        (
            "function Map() { throw Error('local constructor'); }",
            "Error\n",
        ),
        ("class Map {}", "ReferenceError\n"),
    ] {
        let provider = format!(
            "export function make() {{ const cache = new Map(); return {{ read() {{ return cache.size; }} }}; {declaration} }}"
        );
        let fixture = run_fixture(
            FixtureOpts::new("import { make } from './peer.js'; try { make(); } catch (error) { console.log(error.name); }", vec![])
                .with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(report["imported_callables"]["make"]["foreign_call"].is_null());
    }
}

#[test]
fn hoisted_function_values_do_not_reuse_parameter_or_undefined_values() {
    for provider in [
        "export function make(local) { return local; function local() {} }",
        "export function make() { var local; return local; function local() {} }",
        "export const make = function self() { return self; };",
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { make } from './peer.js'; console.log(typeof make(42));",
                vec![],
            )
            .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, "function\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(
            report["imported_callables"]["make"]["foreign_call"].is_null(),
            "an unsupported activation value is not an argument or undefined: {provider}"
        );
    }
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { make } from './peer.js'; console.log(make(42));",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function make(value) { var local = value; return local; function local() {} }",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["imported_callables"]["make"]["foreign_call"]["returns"],
        serde_json::json!([{"kind": "argument", "index": 0}]),
        "an explicit initializer can replace the hoisted binding value"
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { make } from './peer.js'; const earlier = make(41); const later = make(42); console.log(earlier(), later());",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function make(value) { var local = value; return () => local; function local() {} }",
        )]),
    );
    assert_preserved_entry_output(&fixture, "41 42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["imported_callables"]["make"]["foreign_call"]["retained_arguments"],
        serde_json::json!([0]),
        "initialized function-binding cells retain each activation's value"
    );
}
