//! Cross-chunk origin flow is consumed by the real gate and emitted program.
//! Call edges supplement the Node/refusal oracle so these fixtures cannot pass
//! merely because an opaque-call fallback happens to reject the same split.

use analysis::{DependencyGraphReport, reports::schema::EdgeRoleReport};
use analysis_model::DepKind;
use debundle_e2e_graph_support::source_unit_for_binding;
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged as stage};
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
    let (staged, _fixture) =
        stage(FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()));
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
            "ready\n",
            "",
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}
