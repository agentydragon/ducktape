//! Imported, inferred and inline callback factories defer callback reads while their results stay observable.

use super::{API_MAKE_PEER, SOURCE, SplitCase, modules, peer_api_callback_factories};
use analysis_vocabulary::report_schema::{CallbackFactoryUseKind, CallbackFactoryUseReport};
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;
use graph::DependencyGraphReport;

const NO_CAUSES: &[&str] = &[];

const WRAPPER_PEER: &str = "export function factory(callback) { return () => callback(); }\n";

const INLINE_ACTION_PEER: &str = r#"export function action(callback) {
function wrapped() { return callback(); }
wrapped.kind = 'action';
return wrapped;
}
"#;

const OBJECT_SOURCE: &str = "import { factory } from './peer.js';\n\
    const wrapped = factory(() => later);\n\
    const later = 42;\nconsole.log(wrapped.invoke());\n";

const OBJECT_FACTORY: &str = "let creations = 0;\n\
    export function factory(callback) { creations++; return { invoke: () => callback() }; }\n";

fn callback_factory_uses<'a>(
    graph: &'a DependencyGraphReport,
    binding: &str,
) -> &'a [CallbackFactoryUseReport] {
    &node_declaring(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap()
        .callback_factory_uses
}

fn closed_result_roots(graph: &DependencyGraphReport) -> Vec<&CallbackFactoryUseReport> {
    graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|execution| &execution.callback_factory_uses)
        .filter(|proof| proof.kind == CallbackFactoryUseKind::ClosedResultRoot)
        .collect()
}

fn residual_graph_fixture(source: &str) -> Fixture {
    run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&[("static/peer", WRAPPER_PEER)]),
    )
}

#[test]
fn optional_namespace_member_aliases_still_disable_factory_deferral() {
    let peer = [("static/peer", API_MAKE_PEER)];
    let purity = [peer_api_callback_factories(&["make"]).build()];
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
        let case = SplitCase {
            name: &format!("{holder} via {lookup}"),
            source: &source,
            wrapped: &["holder", "alias", "eager", "wrapped"],
            wrapped_statements: &["alias.make = eager;"],
            later_write: true,
            peers: &peer,
            contracts: &purity,
            ..Default::default()
        };
        case.assert_colocated_runs("0 42\n");
        case.assert_split_rejected(&["eager_use", "sequenced"]);
    }
}

#[test]
fn returned_wrapper_does_not_invoke_its_callback_at_creation() {
    let peers = [("static/peer", WRAPPER_PEER)];
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
    let uses = callback_factory_uses(&graph, "wrapped");
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
fn deferring_factories_admit_the_split() {
    let wrapper = [("static/peer", WRAPPER_PEER)];
    let inline = [("static/peer", INLINE_ACTION_PEER)];
    let inline_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_inline_callback_factories(&["action"])
        .build()];
    let object_with_host = [
        ("static/peer", OBJECT_FACTORY),
        (
            "static/host",
            "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
        ),
    ];
    let object_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    let api_peer = [("static/peer", API_MAKE_PEER)];
    let api_contract = [peer_api_callback_factories(&["make"]).build()];
    let inferred_two_parameter = [(
        "static/peer",
        "export var factory = (initialize, cache) => () => {\n\
         if (!cache) { cache = { exports: {} }; initialize(cache.exports, cache); }\n\
         return cache.exports;\n\
         };\n",
    )];
    let assignment_valued_argument = [(
        "static/peer",
        "export var factory = (callback, cache) => () => callback((cache = { value: 1 }).value);\n",
    )];
    let aliased_export = [(
        "static/peer",
        "function build(callback) { return () => callback(); }\nexport { build as factory };\n",
    )];
    for (case, output) in [
        (
            SplitCase {
                name: "inferred two-parameter module factory defers its initializer",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory((exports) => { exports.value = later; });\n\
                         const later = 42;\n\
                         console.log(wrapped().value);\n\
                         export { wrapped, later };\n",
                wrapped: &["wrapped"],
                peers: &inferred_two_parameter,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "inferred factory accepts an assignment-valued fresh member argument",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory((value) => later + value);\n\
                         const later = 42;\n\
                         console.log(wrapped());\n\
                         export { wrapped, later };\n",
                wrapped: &["wrapped"],
                peers: &assignment_valued_argument,
                ..Default::default()
            },
            "43\n",
        ),
        (
            SplitCase {
                name: "audited inline factory defers its single callback",
                source: "import { action as create } from './peer.js';\n\
                         const wrapped = create(() => later);\n\
                         const later = 42;\nconsole.log(wrapped());\n",
                wrapped: &["wrapped"],
                peers: &inline,
                contracts: &inline_contract,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "successive factories do not publish callbacks to the next factory",
                source: "import { factory } from './peer.js';\n\
                         const first = factory(() => later);\n\
                         const second = factory(() => later);\n\
                         const later = 42;\nconsole.log(first(), second());\n",
                wrapped: &["first", "second"],
                peers: &wrapper,
                ..Default::default()
            },
            "42 42\n",
        ),
        (
            SplitCase {
                name: "import and local aliases preserve factory identity",
                source: "import { factory as create } from './peer.js';\nconst alias = create;\n\
                         const wrapped = alias(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
                wrapped: &["wrapped"],
                import_order: Some(&["callbacks/wrapped", "values/later", "residual"]),
                peers: &aliased_export,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "primitive metadata writes keep the callback deferred",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory(() => later);\n\
                         const named = (wrapped.label = 'factory');\n\
                         const later = 42;\nconsole.log(wrapped(), wrapped.label);\n",
                wrapped: &["wrapped", "named"],
                peers: &wrapper,
                ..Default::default()
            },
            "42 factory\n",
        ),
        (
            SplitCase {
                name: "invoked factory result does not run its callback after await synchronously",
                source: "import { factory } from './peer.js';\n\
                         import { consume } from './host.js';\n\
                         const wrapped = factory(async () => { await 0; return later; });\n\
                         const consumed = consume(wrapped);\n\
                         const later = 42;\nconsole.log(later);\n",
                wrapped: &["wrapped", "consumed"],
                peers: &object_with_host,
                contracts: &object_contract,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "namespace factory keeps unrelated optional member values independent",
                source: "import { api } from './peer.js';\n\
                         const input = { message: 'ready' };\n\
                         const message = input?.message;\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\n\
                         console.log(message, wrapped.invoke());\n",
                wrapped: &["input", "message", "wrapped"],
                peers: &api_peer,
                contracts: &api_contract,
                ..Default::default()
            },
            "ready 42\n",
        ),
    ] {
        case.assert_split_runs(output);
    }
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
        SplitCase {
            name: provider,
            source: app,
            wrapped: &["wrapped"],
            peers: &[("static/peer", provider)],
            ..Default::default()
        }
        .assert_split_rejected(&[]);
    }
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

#[test]
fn observable_or_unproven_callback_reads_keep_the_split_atomic() {
    let wrapper = [("static/peer", WRAPPER_PEER)];
    let inline = [("static/peer", INLINE_ACTION_PEER)];
    let inline_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_inline_callback_factories(&["action"])
        .build()];
    let object = [("static/peer", OBJECT_FACTORY)];
    let object_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    let immediate = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    let pure_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["factory"])
        .build()];
    let second_capability = [(
        "static/peer",
        "export var factory = (callback, cache) => () => { callback(); return cache(); };\n",
    )];
    let retained = [(
        "static/peer",
        "export function make(callback) { return { invoke: () => callback() }; }\n\
         export function action(value) {\n\
           if (typeof value !== 'function') return value.invoke();\n\
           function wrapped() { return value(); }\n\
           wrapped.kind = 'action';\n\
           return wrapped;\n}\n",
    )];
    let retained_contract = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["make"])
        .with_inline_callback_factories(&["action"])
        .build()];
    let reexported = [
        ("static/peer", INLINE_ACTION_PEER),
        (
            "static/bridge",
            "export { action as create } from './peer.js';\n",
        ),
    ];
    let reassigned = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\nfactory = callback => callback();\n",
    )];
    let inline_call_shapes = [
        (
            "const callback = () => later;\n",
            "action(callback)",
            &["callback", "wrapped"][..],
        ),
        ("", "action(...[() => later])", &["wrapped"][..]),
        ("", "new action(() => later)", &["wrapped"][..]),
    ]
    .map(|(setup, call, members)| {
        (
            call,
            format!(
                "import {{ action }} from './peer.js';\n{setup}\
                 const wrapped = {call};\nconst later = 42;\nconsole.log(wrapped());\n"
            ),
            members,
        )
    });
    let result_mutations = [
        ("__proto__", "wrapped.value"),
        ("options", "wrapped.options.value"),
    ]
    .map(|(property, read)| {
        (
            property,
            format!(
                "import {{ factory }} from './peer.js';\n\
                 const wrapped = factory(() => 0);\n\
                 const configured = (wrapped.{property} = {{ get value() {{ return later; }} }});\n\
                 const outcome = {read};\nconst later = 42;\nconsole.log(outcome);\n",
            ),
        )
    });
    let synchronous_reads = [
        (
            "async () => { globalThis.seen = later; await 0; }",
            "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
        ),
        (
            "() => () => { globalThis.seen = later; }",
            "export function consume(value) { return Reflect.apply(Reflect.apply(value.invoke, value, []), null, []); }\n",
        ),
    ]
    .map(|(callback, consumer)| {
        (
            callback,
            format!(
                "import {{ factory }} from './peer.js';\n\
                 import {{ consume }} from './host.js';\n\
                 let later = 0;\nconst wrapped = factory({callback});\n\
                 const consumed = consume(wrapped);\nlater = 42;\n\
                 console.log(globalThis.seen, later);\n"
            ),
            [("static/peer", OBJECT_FACTORY), ("static/host", consumer)],
        )
    });
    let mut cases = vec![
        (
            SplitCase {
                name: "immediate callback invocation preserves the TDZ",
                source: SOURCE,
                wrapped: &["wrapped"],
                peers: &immediate,
                ..Default::default()
            },
            &["eager_use", "sequenced"][..],
        ),
        (
            SplitCase {
                name: "purity alone does not defer callbacks",
                source: SOURCE,
                wrapped: &["wrapped"],
                peers: &immediate,
                contracts: &pure_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "inferred module factory does not admit a second supplied capability",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory(() => 1, () => later);\n\
                         const later = 42;\n\
                         console.log(wrapped());\n",
                wrapped: &["wrapped"],
                peers: &second_capability,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "inline-only contract does not compose a retained object",
                source: "import { make, action } from './peer.js';\n\
                         let later = 0;\nconst result = action(make(() => later));\n\
                         later = 42;\nconsole.log(result);\n",
                wrapped: &["result"],
                later_write: true,
                peers: &retained,
                contracts: &retained_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "inline-only contract does not cross an unresolved re-export",
                source: "import { create } from './bridge.js';\n\
                         const wrapped = create(() => later);\n\
                         const later = 42;\nconsole.log(wrapped());\n",
                wrapped: &["wrapped"],
                peers: &reexported,
                contracts: &inline_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "inline-only result invoked before the later write",
                source: "import { action } from './peer.js';\n\
                         let later = 0;\nconst wrapped = action(() => later);\n\
                         const observed = wrapped();\nlater = 42;\nconsole.log(observed, later);\n",
                wrapped: &["wrapped", "observed"],
                later_write: true,
                peers: &inline,
                contracts: &inline_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "invoking the returned wrapper before initialization",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory(() => later);\n\
                         const outcome = wrapped();\nconst later = 42;\nconsole.log(outcome);\n",
                wrapped: &["wrapped", "outcome"],
                peers: &wrapper,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "escaped wrapper can be invoked by a later opaque call",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory(() => later);\n\
                         globalThis.saved = wrapped;\nconst outcome = host();\n\
                         const later = 42;\nconsole.log(outcome);\n",
                wrapped: &["wrapped", "outcome"],
                peers: &wrapper,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "reassigned provider does not inherit the original factory summary",
                source: SOURCE,
                wrapped: &["wrapped"],
                peers: &reassigned,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "a shadowing parameter does not inherit the import contract",
                source: "import { factory } from './peer.js';\n\
                         function invoke(factory) { return factory(() => later); }\n\
                         const outcome = invoke(callback => callback());\n\
                         const later = 42;\nconsole.log(outcome);\n",
                wrapped: &["outcome"],
                import_order: Some(&["callbacks/wrapped", "values/later", "residual"]),
                peers: &wrapper,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "unknown complex factory remains conservative",
                source: OBJECT_SOURCE,
                wrapped: &["wrapped"],
                peers: &object,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "callback factory contract does not apply to an existing callback",
                source: "import { factory } from './peer.js';\n\
                         const callback = () => later;\nconst wrapped = factory(callback);\n\
                         const later = 42;\nconsole.log(wrapped.invoke());\n",
                wrapped: &["wrapped", "callback"],
                peers: &object,
                contracts: &object_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "callbacks written to the returned value remain observable",
                source: "import { factory } from './peer.js';\n\
                         const wrapped = factory(() => 0);\nwrapped.read = () => later;\n\
                         const outcome = wrapped.read();\nconst later = 42;\nconsole.log(outcome);\n",
                wrapped: &["wrapped", "outcome"],
                peers: &wrapper,
                ..Default::default()
            },
            NO_CAUSES,
        ),
    ];
    for (call, source, members) in &inline_call_shapes {
        cases.push((
            SplitCase {
                name: call,
                source,
                wrapped: members,
                peers: &inline,
                contracts: &inline_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ));
    }
    for (property, source) in &result_mutations {
        cases.push((
            SplitCase {
                name: property,
                source,
                wrapped: &["wrapped", "configured", "outcome"],
                peers: &wrapper,
                ..Default::default()
            },
            NO_CAUSES,
        ));
    }
    for (callback, source, peers) in &synchronous_reads {
        cases.push((
            SplitCase {
                name: callback,
                source,
                wrapped: &["wrapped", "consumed"],
                later_write: true,
                peers,
                contracts: &object_contract,
                ..Default::default()
            },
            NO_CAUSES,
        ));
    }
    for (case, causes) in cases {
        case.assert_split_rejected(causes);
    }
}

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
    assert!(
        callback_factory_uses(&graph, "wrapped")
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
        eprintln!("case: {source}");
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
    let fixture = residual_graph_fixture(
        "import { factory } from './peer.js';\n\
         function wrap() { return factory(() => later); }\n\
         const pair = [wrap(), wrap()];\n\
         const later = 42;\n\
         console.log(pair[0](), pair[1]());\n",
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
    let graph = fixture.dependency_graph();
    let pair = node_declaring(&graph, "pair");
    let uses = callback_factory_uses(&graph, "pair");
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
    assert!(callback_factory_uses(&graph, "later").is_empty());
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
    let historical: DependencyGraphReport = serde_json::from_value(old_graph).unwrap();
    assert!(historical.imported_callable_summaries.is_empty());
}

#[test]
fn private_factory_result_records_reachability_exemption() {
    // The import declaration follows its first use.
    let fixture = residual_graph_fixture(
        "const wrapped = factory(() => later);\n\
         import { factory } from './peer.js';\n\
         const later = 42;\n\
         console.log(wrapped());\n",
    );
    assert_preserved_entry_output(&fixture, "42\n");
    assert!(
        callback_factory_uses(&fixture.dependency_graph(), "wrapped")
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
        eprintln!("case: {initializer}");
        let fixture = residual_graph_fixture(&format!(
            "import {{ factory }} from './peer.js';\n\
             globalThis.marker = 0;\nconst wrapped = {initializer};\n\
             const later = 42;\nconsole.log(globalThis.marker, wrapped());\n"
        ));
        assert_preserved_entry_output(&fixture, &format!("{marker} 42\n"));
        let graph = fixture.dependency_graph();
        let uses: Vec<_> = callback_factory_uses(&graph, "wrapped")
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
        eprintln!("case: {initializer}");
        let fixture = residual_graph_fixture(&format!(
            "import {{ factory }} from './peer.js';\nconst wrapped = {initializer};\n"
        ));
        assert_preserved_entry_error(&fixture, "Error: operand");
        assert!(closed_result_roots(&fixture.dependency_graph()).is_empty());
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
    let uses = closed_result_roots(&graph);
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
        let fixture = residual_graph_fixture(&format!(
            "import {{ factory }} from './peer.js';\n{declaration}\nconst later = 42;\nconsole.log(wrapped());\n"
        ));
        assert_preserved_entry_output(&fixture, "42\n");
        assert_eq!(
            closed_result_roots(&fixture.dependency_graph()).len(),
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
            ("static/first", WRAPPER_PEER),
            ("static/second", WRAPPER_PEER),
        ]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    assert!(graph.imported_callable_summaries.is_empty());
}
