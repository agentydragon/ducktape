//! Cross-chunk origin flow is consumed by the real gate and emitted program.
//! Call edges supplement the Node/refusal oracle so these fixtures cannot pass
//! merely because an opaque-call fallback happens to reject the same split.

use analysis::{DependencyGraphReport, reports::schema::EdgeRoleReport};
use analysis_model::DepKind;
use debundle_e2e_graph_support::source_unit_for_binding;
use debundle_e2e_support::saved_analysis::{
    assert_same_invocation_trees, assert_staged_entry_output, path, staged as stage,
};
use debundle_e2e_support::*;

fn assert_flow_case(source: &str, peers: &[(&str, &str)], calls: &[(&str, &str)]) {
    let opts = || {
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(peers)
            .with_unassigned_mode(unassigned_mode_inline())
    };
    let (staged, _fixture) = stage(opts());
    assert_node_output(&staged.root().join("snapshot/static/app.js"), "0 7\n", "");
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        assert!(
            invocation.result.status.success(),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "0 7\n",
            "",
        );
        let graph: DependencyGraphReport = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        for (caller, callee) in calls {
            let caller = source_unit_for_binding(&graph, caller);
            let callee = source_unit_for_binding(&graph, callee);
            let target = source_unit_for_binding(&graph, "later");
            assert!(graph.edges.iter().any(|edge| edge.source == caller && edge.target == target && edge.edge_kind == DepKind::EagerUse && matches!(&edge.role, Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) if callee_source_unit == callee)), "missing callback-specific promoted read {caller} -> {target} via {callee}: {:#?}", graph.edges);
        }
    }
    assert_same_invocation_trees(&direct, &saved);

    // Moving the cell's initializer and write into an eagerly imported module
    // changes the observed callback result from 0 to 7. Both analysis paths
    // must refuse it; emit-on-rejection demonstrates the concrete divergence.
    let (staged, _fixture) = stage(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "state",
                &[Member::new("later")],
                &["later = 7;"],
            )],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let direct = staged.run(&["--emit-on-rejection"]);
    let saved = staged.run(&["--emit-on-rejection", "--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        assert_eq!(
            invocation.result.status.code(),
            Some(3),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "7 7\n",
            "",
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}

#[test]
fn audited_factory_carrier_with_multiple_source_origins_keeps_a_valid_cut() {
    let source = "import { factory, api } from './peer.js';\n\
                 let inner = factory(() => later);\n\
                 const wrapped = api.make(inner, () => other);\n\
                 const later = 42;\nconst other = 7;\n\
                 console.log(wrapped.invoke(), wrapped.compare());\n";
    let provider = "const wrappers = { wrap(value) { return (...args) => value(...args); } };\n\
                    export function factory(callback) {\n\
                      if (typeof callback !== 'function') return callback;\n\
                      return wrappers.wrap(callback);\n\
                    }\n\
                    export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n";
    assert_staged_entry_output(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &binding_members(["inner", "wrapped"])),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
        "42 7\n",
    );
}

#[test]
fn ordinary_multiple_callback_origins_remain_conservative() {
    let source = "import { api } from './peer.js';\n\
                 const chooseLater = true;\n\
                 const callback = chooseLater ? () => later : () => other;\n\
                 const wrapped = api.make(callback, () => other);\n\
                 const later = 42;\nconst other = 7;\n\
                 console.log(wrapped.invoke(), wrapped.compare());\n";
    let provider = "export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["chooseLater", "callback", "wrapped"]),
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn rebound_factory_carrier_remains_conservative() {
    let source = "import { factory, api } from './peer.js';\n\
                 let inner = factory(() => later);\n\
                 inner = factory(() => later);\n\
                 const wrapped = api.make(inner, () => other);\n\
                 const later = 42;\nconst other = 7;\n\
                 console.log(wrapped.invoke(), wrapped.compare());\n";
    let provider = "const wrappers = { wrap(value) { return (...args) => value(...args); } };\n\
                    export function factory(callback) {\n\
                      if (typeof callback !== 'function') return callback;\n\
                      return wrappers.wrap(callback);\n\
                    }\n\
                    export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n";
    let rejection = run_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["inner", "wrapped"]),
                    &["inner = factory(() => later);"],
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let conflicts = rejection.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["wrapped", "later"].iter().all(|binding| {
                claims.iter().any(|claim| {
                    claim["binding_names"].as_array().is_some_and(|names| {
                        names.iter().any(|name| name.as_str() == Some(binding))
                    })
                })
            }) && ["callbacks/wrapped", "values/later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(module))
            })
        }),
        "rebind refusal did not produce a relevant wrapped/later atomic conflict: {conflicts:#?}"
    );
}

#[test]
fn parameter_return_and_reexport_chain_recover_the_local_callback() {
    assert_flow_case(
        "import { relay } from './bridge.js';\nlet later = 0;\nfunction callback() { return later; }\nconst forwarded = relay(callback);\nconst observed = forwarded();\nlater = 7;\nconsole.log(observed, later);\n",
        &[
            (
                "static/bridge",
                "import { relay } from './provider.js';\nexport { relay };\nexport const marker = 1;\n",
            ),
            (
                "static/provider",
                "import { marker } from './bridge.js';\nexport function relay(value) { return value; }\n",
            ),
        ],
        &[("observed", "callback")],
    );
}

#[test]
fn a_foreign_record_retains_local_object_and_callback_origins() {
    assert_flow_case(
        "import { wrap } from './peer.js';\nlet later = 0;\nfunction callback() { return later; }\nconst input = { run: callback };\nconst forwarded = wrap(input);\nconst observed = forwarded.box.run();\nlater = 7;\nconsole.log(observed, later);\n",
        &[(
            "static/peer",
            "export function wrap(value) { return { box: value }; }\n",
        )],
        &[("observed", "callback")],
    );
}

#[test]
fn equal_offsets_and_names_in_two_providers_are_distinct() {
    let source = "import { relay as first } from './first.js';\nimport { relay as second } from './second.js';\nconst alpha = 1;\nconst beta = 2;\nfunction callback() { return alpha; }\nfunction other() { return beta; }\nconst left = first(callback);\nconst right = second(other);\nconst observed = left();\nconst unrelated = right();\nconsole.log(observed, unrelated);\n";
    // Identical source gives identical local function offsets and names.
    let provider = "export function relay(value) { return value; }\n";
    let peers = [("static/first", provider), ("static/second", provider)];
    let (staged, _fixture) = stage(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        assert!(
            invocation.result.status.success(),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "1 2\n",
            "",
        );
        let graph: DependencyGraphReport = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        for (caller, expected, excluded, target) in [
            ("observed", "callback", "other", "alpha"),
            ("unrelated", "other", "callback", "beta"),
        ] {
            let caller = source_unit_for_binding(&graph, caller);
            let target = source_unit_for_binding(&graph, target);
            for (callee, present) in [(expected, true), (excluded, false)] {
                let callee = source_unit_for_binding(&graph, callee);
                assert_eq!(graph.edges.iter().any(|edge| edge.source == caller && edge.target == target && edge.edge_kind == DepKind::EagerUse && matches!(&edge.role, Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) if callee_source_unit == callee)), present, "qualified callback {caller} -> {target} via {callee}: {:#?}", graph.edges);
            }
        }
    }
    assert_same_invocation_trees(&direct, &saved);
}

#[test]
fn late_targets_and_arguments_through_a_cyclic_object_preserve_callback_reads() {
    let body = r#"const box = {};
function dispatch(run) { return run(box.peer.run); }
function callback() { return later; }
let later = 0;
box.peer = box;
box.run = callback;
const forwarded = dispatch(relay);
const observed = forwarded();
later = 7;
console.log(observed, later);
"#;
    assert_flow_case(
        &format!("function relay(value) {{ return value; }}\n{body}"),
        &[],
        &[("observed", "callback")],
    );
    assert_flow_case(
        &format!("import {{ relay }} from './peer.js';\n{body}"),
        &[(
            "static/peer",
            "export function relay(value) { return value; }\n",
        )],
        &[("observed", "callback")],
    );
}

#[test]
fn self_feeding_formals_keep_the_source_event_argument_order() {
    assert_flow_case(
        r#"let later = 0;
function callback() { return later; }
function relay(value) { return value; }
function feed(seed, carry) {
  (relay || feed)(carry, seed);
  return seed;
}
const forwarded = feed(callback, callback);
const observed = forwarded();
later = 7;
console.log(observed, later);
"#,
        &[],
        &[("observed", "callback")],
    );
}

#[test]
fn late_target_backfill_keeps_nested_missing_property_unknown_local_to_early_edges() {
    assert_flow_case(
        r#"let later = 0;
function callback() { return later; }
function first(early) { return early; }
function second(late) { return late; }
const box = {};
const selected = first || delayed;
function dispatch() { return selected(box.peer.run); }
box.peer = { run: callback };
var delayed = next;
var next = second;
const forwarded = dispatch();
const observed = forwarded();
later = 7;
console.log(observed, later);
"#,
        &[],
        &[("observed", "callback")],
    );
}

#[test]
fn metadata_only_inline_calls_preserve_output_without_inventing_value_readiness() {
    let source = r#"(function (value) { value(); })(function () { console.log('ready'); });
"#;
    assert_staged_entry_output(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
        "ready\n",
    );
}

#[test]
fn property_writes_with_late_parameters_preserve_callback_order() {
    assert_flow_case(
        "let later = 0;\nfunction callback() { return later; }\nconst record = {};\nfunction install(receiver, value) { receiver.run = value; return receiver; }\nconst forwarded = install(record, callback);\nconst observed = forwarded.run();\nlater = 7;\nconsole.log(observed, later);\n",
        &[],
        &[("observed", "callback")],
    );
}

#[test]
fn imported_property_write_and_self_receiver_preserve_callback_order() {
    assert_flow_case(
        "import { record } from './peer.js';\nlet later = 0;\nfunction callback() { return later; }\nrecord.next = record;\nfunction install(value) { record.next.run = value; }\ninstall(callback);\nconst observed = record.next.run();\nlater = 7;\nconsole.log(observed, later);\n",
        &[("static/peer", "export const record = {};\n")],
        &[("observed", "callback")],
    );
}

// Also used by the fanout benchmark. Keep the actual guarded shape: removing
// hasOwnProperty.call would omit an observable call from a future recognizer.
const DEFAULT_EXPORT_WRAPPER: &str = include_str!("fixtures/default_export_wrapper.js");

#[test]
fn default_export_wrapper_keeps_direct_and_namespace_callbacks_observable() {
    for input in ["callback", "{ __esModule: true, default: callback }"] {
        assert_flow_case(
            &format!(
                "{DEFAULT_EXPORT_WRAPPER}\n\
                 let later = 0;\n\
                 function callback() {{ return later; }}\n\
                 function unrelated() {{ return 11; }}\n\
                 const forwarded = unwrapDefault({input});\n\
                 const other = unwrapDefault(unrelated);\n\
                 const observed = forwarded();\n\
                 later = 7;\n\
                 console.log(observed, later);\n"
            ),
            &[],
            &[("observed", "callback")],
        );
    }
}

#[test]
fn default_export_wrapper_preserves_an_accessor_returned_callback() {
    assert_flow_case(
        &format!(
            "{DEFAULT_EXPORT_WRAPPER}\n\
             let later = 0;\n\
             function callback() {{ return later; }}\n\
             const namespace = {{ __esModule: true, get default() {{ return callback; }} }};\n\
             const forwarded = unwrapDefault(namespace);\n\
             const observed = forwarded();\n\
             later = 7;\n\
             console.log(observed, later);\n"
        ),
        &[],
        &[],
    );
}

#[test]
fn default_export_wrapper_preserves_function_default_property_writes() {
    assert_flow_case(
        &format!(
            "{DEFAULT_EXPORT_WRAPPER}\n\
             let later = 0;\n\
             function callback() {{ return later; }}\n\
             function input() {{ return 11; }}\n\
             input.__esModule = true;\n\
             const alias = input;\n\
             alias.default = callback;\n\
             const forwarded = unwrapDefault(input);\n\
             const observed = forwarded();\n\
             later = 7;\n\
             console.log(observed, later);\n"
        ),
        &[],
        &[],
    );
}

#[test]
fn default_export_wrapper_preserves_a_shadowed_guard_call() {
    assert_flow_case(
        &format!(
            "{DEFAULT_EXPORT_WRAPPER}\n\
             let later = 0;\n\
             let observed = -1;\n\
             function guard() {{ observed = later; return false; }}\n\
             const Object = {{ prototype: {{ hasOwnProperty: {{ call: guard }} }} }};\n\
             const forwarded = unwrapDefault({{ __esModule: true, default: 11 }});\n\
             later = 7;\n\
             console.log(observed, later);\n"
        ),
        &[],
        &[],
    );
}

#[test]
fn default_export_wrapper_reassignment_keeps_the_actual_callee() {
    assert_flow_case(
        &format!(
            "{DEFAULT_EXPORT_WRAPPER}\n\
             let later = 0;\n\
             function callback() {{ return later; }}\n\
             function unrelated() {{ return 11; }}\n\
             function replacement() {{ return callback; }}\n\
             unwrapDefault = replacement;\n\
             const forwarded = unwrapDefault(unrelated);\n\
             const observed = forwarded();\n\
             later = 7;\n\
             console.log(observed, later);\n"
        ),
        &[],
        &[("observed", "callback")],
    );
}
