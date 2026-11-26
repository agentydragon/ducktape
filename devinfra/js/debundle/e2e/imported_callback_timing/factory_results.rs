//! Imported, inferred and inline callback factories defer callback reads while their results stay observable.

use super::{SOURCE, modules, peer_api_callback_factories};
use analysis_vocabulary::report_schema::CallbackFactoryUseKind;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

#[test]
fn namespace_factory_keeps_unrelated_optional_member_values_independent() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const input = { message: 'ready' };\n\
             const message = input?.message;\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(message, wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["input", "message", "wrapped"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "ready 42\n");
}

#[test]
fn optional_namespace_member_aliases_still_disable_factory_deferral() {
    for (holder, lookup) in [
        ("{ api }", "holder?.api"),
        ("{ api }", "holder?.['api']"),
        ("{ get api() { return api; } }", "holder?.api"),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             let later = 0;\n\
             const holder = {holder};\n\
             const alias = {lookup};\n\
             const eager = callback => {{ globalThis.observed = callback(); return {{ invoke: () => callback() }}; }};\n\
             alias.make = eager;\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\n\
             console.log(globalThis.observed, wrapped.invoke());\n"
        );
        let peer = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let purity = [peer_api_callback_factories(&["make"]).build()];
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &binding_members(["later", "holder", "alias", "eager", "wrapped"]),
                    &["alias.make = eager;", "later = 42;"],
                )],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
        );
        assert_preserved_entry_output(&colocated, "0 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["holder", "alias", "eager", "wrapped"]),
                        &["alias.make = eager;"],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn returned_wrapper_does_not_invoke_its_callback_at_creation() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let evidence = &report["imported_callables"]["factory"]["callback_factory_evidence"][0];
    assert_eq!(evidence["origin"], "inferred_immutable_wrapper");
    assert_eq!(evidence["defining_module"], "static/peer");
    assert_eq!(evidence["export"], "factory");
    assert_eq!(evidence["local"], "factory");
    assert_eq!(evidence["source_location"]["source_path"], "static/peer.js");
    assert_eq!(evidence["source_location"]["start_line"], 1);
    assert_eq!(evidence["source_location"]["end_line"], 1);
    assert_eq!(evidence["source_location"]["start_column"], 8);
    let graph = fixture.dependency_graph();
    assert_eq!(graph.imported_callable_summaries.len(), 1);
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    let uses = &wrapped
        .synchronous_execution
        .as_ref()
        .unwrap()
        .callback_factory_uses;
    assert!(
        uses.iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::CallTiming)
    );
    assert!(
        uses.iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
    );
    assert!(uses.iter().all(|proof_use| proof_use.summary_ref
        == graph.imported_callable_summaries[0].id
        && proof_use.source_location.as_ref().unwrap().start_line == 2));
}

#[test]
fn inferred_two_parameter_module_factory_defers_initializer() {
    let peers = [(
        "static/peer",
        "export var factory = (initialize, cache) => () => {\n\
         if (!cache) { cache = { exports: {} }; initialize(cache.exports, cache); }\n\
         return cache.exports;\n\
         };\n",
    )];
    let source = "import { factory } from './peer.js';\n\
                  const wrapped = factory((exports) => { exports.value = later; });\n\
                  const later = 42;\n\
                  console.log(wrapped().value);\n\
                  export { wrapped, later };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn inferred_factory_accepts_assignment_valued_fresh_member_argument() {
    let provider =
        "export var factory = (callback, cache) => () => callback((cache = { value: 1 }).value);\n";
    let source = "import { factory } from './peer.js';\n\
                  const wrapped = factory((value) => later + value);\n\
                  const later = 42;\n\
                  console.log(wrapped());\n\
                  export { wrapped, later };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "43\n");
}

#[test]
fn inferred_module_factory_rejects_extra_callback_roots_and_mutable_exports() {
    let app = "import { factory } from './peer.js';\n\
               const wrapped = factory(() => later);\n\
               const later = 42;\n\
               console.log(wrapped());\n";
    for provider in [
        // The outer call itself can invoke the supplied callback.
        "export var factory = (callback, cache) => (callback(), () => cache);\n",
        // The returned function can invoke an unrepresented host callback.
        "export var factory = (callback, cache) => () => { globalThis.saved(); return callback(); };\n",
        // A separate captured binding can read importer state when invoked.
        "const other = () => globalThis.saved(); export var factory = (callback, cache) => () => { other(); return callback(); };\n",
        // An inherited property is not part of the fresh returned graph.
        "export var factory = (callback, cache) => () => { cache = {}; return cache.constructor; };\n",
        "export var factory = (callback, cache) => () => { const other = () => globalThis.saved(); return callback(); };\n",
        "export var factory = (callback, cache) => () => { cache = { exports: {} }; return cache['exports']; };\n",
        "export var factory = (callback, cache) => () => callback((cache = { get value() { return globalThis.saved(); } }).value);\n",
        "export var factory = (callback, cache) => () => callback((cache = { value: 1 })['value']);\n",
        "export var factory = (callback, cache) => () => this;\n",
        "export var factory = (callback = () => 0, cache) => () => callback();\n",
        "export var factory = ({ callback }, cache) => () => callback();\n",
        "export var factory = (callback, cache) => () => callback(); factory = callback => callback();\n",
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                app,
                vec![
                    logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", provider)]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn inferred_module_factory_does_not_admit_a_second_supplied_capability() {
    let provider =
        "export var factory = (callback, cache) => () => { callback(); return cache(); };\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => 1, () => later);\n\
             const later = 42;\n\
             console.log(wrapped());\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn inferred_var_factory_keeps_a_caught_preinitialization_throw() {
    let source = "import './peer.js';\n";
    let peer = [
        (
            "static/peer",
            "import './trigger.js';\nexport var factory = (callback, cache) => () => callback();\n",
        ),
        (
            "static/trigger",
            "import { factory } from './peer.js';\n\
             let observed = 'none';\n\
             try { factory(() => 42); } catch (error) { observed = error.name; }\n\
             console.log(observed);\n",
        ),
    ];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peer));
    assert_preserved_entry_output(&fixture, "TypeError\n");
}

const INLINE_ACTION_PEER: &str = r#"export function action(callback) {
function wrapped() { return callback(); }
wrapped.kind = 'action';
return wrapped;
}
"#;

#[test]
fn audited_inline_factory_defers_its_single_callback() {
    let peers = [("static/peer", INLINE_ACTION_PEER)];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { action as create } from './peer.js';\n\
             const wrapped = create(() => later);\n\
             const later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn inline_only_contract_keeps_other_call_shapes_conservative() {
    let peers = [("static/peer", INLINE_ACTION_PEER)];
    for (setup, call, members) in [
        (
            "const callback = () => later;\n",
            "action(callback)",
            vec![Member::new("callback"), Member::new("wrapped")],
        ),
        ("", "action(...[() => later])", vec![Member::new("wrapped")]),
        ("", "new action(() => later)", vec![Member::new("wrapped")]),
    ] {
        let source = format!(
            "import {{ action }} from './peer.js';\n{setup}\
             const wrapped = {call};\nconst later = 42;\nconsole.log(wrapped());\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &members),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_inline_callback_factories(&["action"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn inline_only_contract_does_not_compose_a_retained_object() {
    let peers = [(
        "static/peer",
        "export function make(callback) { return { invoke: () => callback() }; }\n\
         export function action(value) {\n\
           if (typeof value !== 'function') return value.invoke();\n\
           function wrapped() { return value(); }\n\
           wrapped.kind = 'action';\n\
           return wrapped;\n}\n",
    )];
    let source = "import { make, action } from './peer.js';\n\
                  let later = 0;\nconst result = action(make(() => later));\n\
                  later = 42;\nconsole.log(result);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/result", &[Member::new("result")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["make"])
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/result", "values/later"],
        &[],
    );
}

#[test]
fn inline_only_contract_does_not_cross_an_unresolved_reexport() {
    let peers = [
        ("static/peer", INLINE_ACTION_PEER),
        (
            "static/bridge",
            "export { action as create } from './peer.js';\n",
        ),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { create } from './bridge.js';\n\
             const wrapped = create(() => later);\n\
             const later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn inline_only_result_invoked_before_later_write_remains_atomic() {
    let peers = [("static/peer", INLINE_ACTION_PEER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { action } from './peer.js';\n\
             let later = 0;\nconst wrapped = action(() => later);\n\
             const observed = wrapped();\nlater = 42;\nconsole.log(observed, later);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "observed"]),
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn immediate_callback_invocation_preserves_the_tdz() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn successive_factories_do_not_publish_callbacks_to_the_next_factory() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const first = factory(() => later);\n\
         const second = factory(() => later);\n\
         const later = 42;\nconsole.log(first(), second());\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["first", "second"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn invoking_returned_wrapper_before_initialization_is_rejected() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             const outcome = wrapped();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "outcome"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn escaped_wrapper_can_be_invoked_by_a_later_opaque_call() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             globalThis.saved = wrapped;\nconst outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "outcome"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn reassigned_provider_does_not_inherit_the_original_factory_summary() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\nfactory = callback => callback();\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_shadowing_parameter_does_not_inherit_the_import_contract() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             function invoke(factory) { return factory(() => later); }\n\
             const outcome = invoke(callback => callback());\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("outcome")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

const OBJECT_SOURCE: &str = "import { factory } from './peer.js';\n\
    const wrapped = factory(() => later);\n\
    const later = 42;\nconsole.log(wrapped.invoke());\n";

const OBJECT_FACTORY: &str = "let creations = 0;\n\
    export function factory(callback) { creations++; return { invoke: () => callback() }; }\n";

#[test]
fn audited_factory_keeps_callbacks_in_its_returned_object() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["imported_callables"]["factory"]["callback_factory_evidence"],
        serde_json::json!([{ "origin": "author_assertion", "defining_module": "static/peer", "export": "factory" }]),
    );
    let graph = fixture.dependency_graph();
    assert_eq!(graph.imported_callable_summaries.len(), 1);
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    assert!(
        wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
    );
}

#[test]
fn available_imported_factory_without_eligible_call_has_no_proof_use() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    for (source, priority) in [
        (
            "import { factory } from './peer.js';\nconsole.log(42);\n",
            &[][..],
        ),
        (
            "import { factory } from './peer.js';\nfactory(123);\nconsole.log(42);\n",
            &[][..],
        ),
        (
            "import { factory } from './peer.js';\nfunction use(factory) { return factory(() => 1); }\nconsole.log(use((callback) => callback()) + 41);\n",
            &["residual"][..],
        ),
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![])
                .with_import_order(priority)
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let graph = fixture.dependency_graph();
        assert!(graph.imported_callable_summaries.is_empty());
        assert!(graph.nodes.iter().all(|node| {
            node.synchronous_execution
                .as_ref()
                .is_none_or(|execution| execution.callback_factory_uses.is_empty())
        }));
    }
}

#[test]
fn nested_repeated_factory_use_keeps_lexical_site_and_top_level_owner() {
    let source = "import { factory } from './peer.js';\n\
                  function wrap() { return factory(() => later); }\n\
                  const pair = [wrap(), wrap()];\n\
                  const later = 42;\n\
                  console.log(pair[0](), pair[1]());\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&[(
                "static/peer",
                "export function factory(callback) { return () => callback(); }\n",
            )]),
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
    let graph = fixture.dependency_graph();
    let pair = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "pair")
        })
        .unwrap();
    let uses = &pair
        .synchronous_execution
        .as_ref()
        .unwrap()
        .callback_factory_uses;
    assert_eq!(
        uses.iter()
            .filter(|proof_use| proof_use.kind == CallbackFactoryUseKind::CallTiming)
            .count(),
        1
    );
    assert_eq!(
        uses.iter()
            .filter(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
            .count(),
        1
    );
    assert!(
        uses.iter().all(
            |proof_use| proof_use.lexical_statement_ordinal != pair.statement_ordinal
                && proof_use.source_location.as_ref().unwrap().start_line == 2
        )
    );
    let later = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "later")
        })
        .unwrap();
    assert!(
        later
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .is_empty()
    );
    let mut old_graph = serde_json::to_value(&graph).unwrap();
    old_graph
        .as_object_mut()
        .unwrap()
        .remove("imported_callable_summaries");
    for node in old_graph["nodes"].as_array_mut().unwrap() {
        if let Some(execution) = node["synchronous_execution"].as_object_mut() {
            execution.remove("callback_factory_uses");
        }
    }
    let historical: graph::DependencyGraphReport = serde_json::from_value(old_graph).unwrap();
    assert!(historical.imported_callable_summaries.is_empty());
}

#[test]
fn private_factory_result_records_reachability_exemption() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "const wrapped = factory(() => later);\n\
         import { factory } from './peer.js';\n\
         const later = 42;\n\
         console.log(wrapped());\n",
            vec![],
        )
        .with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export function factory(callback) { return () => callback(); }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    assert!(
        wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ClosedResultRoot)
    );
}

#[test]
fn private_factory_result_requires_an_exact_initializer_and_retains_summary_evidence() {
    let mut actual = Vec::new();
    let mut expected = Vec::new();
    for (initializer, private, marker) in [
        ("factory(() => later)", true, 0),
        ("(factory(() => later))", true, 0),
        ("((factory)((() => later)))", false, 0),
        ("(0, factory(() => later))", true, 0),
        ("(0, factory)(() => later)", false, 0),
        ("(globalThis.marker = 1, factory(() => later))", false, 1),
        ("(globalThis.marker = 1, factory)(() => later)", false, 1),
        ("factory((globalThis.marker = 1, () => later))", false, 1),
        (
            "({get current() { globalThis.marker = 1; return factory; }}).current(() => later)",
            false,
            1,
        ),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             globalThis.marker = 0;\nconst wrapped = {initializer};\n\
             const later = 42;\nconsole.log(globalThis.marker, wrapped());\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&[(
                    "static/peer",
                    "export function factory(callback) { return () => callback(); }\n",
                )]),
        );
        assert_preserved_entry_output(&fixture, &format!("{marker} 42\n"));
        let graph = fixture.dependency_graph();
        let wrapped = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "wrapped")
            })
            .unwrap();
        let uses: Vec<_> = wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .iter()
            .filter(|proof| proof.kind == CallbackFactoryUseKind::ClosedResultRoot)
            .collect();
        actual.push((initializer, uses.len()));
        expected.push((initializer, usize::from(private)));
        if let [proof] = uses.as_slice() {
            assert_eq!(proof.summary_ref, graph.imported_callable_summaries[0].id);
            assert!(
                !graph.imported_callable_summaries[0]
                    .callback_factory_evidence
                    .is_empty()
            );
        }
    }
    assert_eq!(actual, expected);
}

#[test]
fn callback_factory_privacy_does_not_cover_throwing_operands() {
    for initializer in [
        "((() => { throw new Error('operand'); })(), factory)(() => 42)",
        "factory((() => { throw new Error('operand'); })())",
        "({get current() { throw new Error('operand'); }}).current(() => 42)",
    ] {
        let source =
            format!("import {{ factory }} from './peer.js';\nconst wrapped = {initializer};\n");
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&[(
                    "static/peer",
                    "export function factory(callback) { return () => callback(); }\n",
                )]),
        );
        assert_preserved_entry_error(&fixture, "Error: operand");
        assert!(fixture.dependency_graph().nodes.iter().all(|node| {
            node.synchronous_execution.as_ref().is_none_or(|execution| {
                execution
                    .callback_factory_uses
                    .iter()
                    .all(|proof| proof.kind != CallbackFactoryUseKind::ClosedResultRoot)
            })
        }));
    }
}

#[test]
fn private_factory_result_retains_the_audited_summary() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![],
        ).with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export function factory(callback) { return { invoke: () => callback() }; }\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_pure_exports(&["factory"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    let uses: Vec<_> = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|execution| &execution.callback_factory_uses)
        .filter(|proof| proof.kind == CallbackFactoryUseKind::ClosedResultRoot)
        .collect();
    assert_eq!(uses.len(), 1);
    assert_eq!(
        graph
            .imported_callable_summaries
            .iter()
            .find(|summary| summary.id == uses[0].summary_ref)
            .unwrap()
            .callback_factory_evidence,
        vec![
            analysis_vocabulary::inputs::CallbackFactoryEvidence::AuthorAssertion {
                defining_module: "static/peer".into(),
                export: "factory".into(),
            }
        ]
    );
}

#[test]
fn acyclic_exported_factory_result_keeps_its_initialization_exemption() {
    for declaration in [
        "export const wrapped = factory(() => later);",
        "const wrapped = factory(() => later);\nexport { wrapped };",
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n{declaration}\nconst later = 42;\nconsole.log(wrapped());\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&[(
                    "static/peer",
                    "export function factory(callback) { return () => callback(); }\n",
                )]),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        assert_eq!(
            fixture
                .dependency_graph()
                .nodes
                .iter()
                .filter_map(|node| node.synchronous_execution.as_ref())
                .flat_map(|execution| &execution.callback_factory_uses)
                .filter(|proof| proof.kind == CallbackFactoryUseKind::ClosedResultRoot)
                .count(),
            1,
            "{declaration}"
        );
    }
}

#[test]
fn joined_imported_factories_preserve_behavior_without_false_direct_proof_use() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { factory as first } from './first.js';\n\
         import { factory as second } from './second.js';\n\
         let selected = first;\n\
         selected = second;\n\
         const wrapped = selected(() => later);\n\
         const later = 42;\n\
         console.log(wrapped());\n",
            vec![],
        )
        .with_import_order(&["residual"])
        .with_extra_chunks(&[
            (
                "static/first",
                "export function factory(callback) { return () => callback(); }\n",
            ),
            (
                "static/second",
                "export function factory(callback) { return () => callback(); }\n",
            ),
        ]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    assert!(graph.imported_callable_summaries.is_empty());
}

#[test]
fn invoked_factory_result_does_not_run_its_callback_after_await_synchronously() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             import { consume } from './host.js';\n\
             const wrapped = factory(async () => { await 0; return later; });\n\
             const consumed = consume(wrapped);\n\
             const later = 42;\nconsole.log(later);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "consumed"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[
            ("static/peer", OBJECT_FACTORY),
            (
                "static/host",
                "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
            ),
        ])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn invoked_factory_result_keeps_synchronous_callback_and_returned_callback_reads() {
    for (callback, consumer) in [
        (
            "async () => { globalThis.seen = later; await 0; }",
            "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
        ),
        (
            "() => () => { globalThis.seen = later; }",
            "export function consume(value) { return Reflect.apply(Reflect.apply(value.invoke, value, []), null, []); }\n",
        ),
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    "import {{ factory }} from './peer.js';\n\
                     import {{ consume }} from './host.js';\n\
                     let later = 0;\nconst wrapped = factory({callback});\n\
                     const consumed = consume(wrapped);\nlater = 42;\n\
                     console.log(globalThis.seen, later);\n"
                ),
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &binding_members(["wrapped", "consumed"]),
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", OBJECT_FACTORY), ("static/host", consumer)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factories(&["factory"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn unknown_complex_factory_remains_conservative() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(OBJECT_SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callback_factory_contract_does_not_apply_to_an_existing_callback() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const callback = () => later;\nconst wrapped = factory(callback);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "callback"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn purity_alone_does_not_defer_callbacks() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules())
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callbacks_written_to_the_returned_value_remain_observable() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => 0);\nwrapped.read = () => later;\n\
             const outcome = wrapped.read();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "outcome"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn import_and_local_aliases_preserve_factory_identity() {
    let peers = [(
        "static/peer",
        "function build(callback) { return () => callback(); }\nexport { build as factory };\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory as create } from './peer.js';\nconst alias = create;\n\
         const wrapped = alias(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn result_mutation_cannot_hide_an_early_getter_read() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    for (property, read) in [
        ("__proto__", "wrapped.value"),
        ("options", "wrapped.options.value"),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             const wrapped = factory(() => 0);\n\
             const configured = (wrapped.{property} = {{ get value() {{ return later; }} }});\n\
             const outcome = {read};\nconst later = 42;\nconsole.log(outcome);\n",
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &binding_members(["wrapped", "configured", "outcome"]),
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn primitive_metadata_writes_keep_the_callback_deferred() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const named = (wrapped.label = 'factory');\n\
         const later = 42;\nconsole.log(wrapped(), wrapped.label);\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["wrapped", "named"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 factory\n");
}
