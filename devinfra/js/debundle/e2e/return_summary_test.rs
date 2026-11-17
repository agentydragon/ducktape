//! A call's return relationship must not import another call's callback.

use analysis::{DependencyGraphReport, reports::schema::EdgeRoleReport};
use analysis_model::DepKind;
use debundle_e2e_graph_support::source_unit_for_binding;
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
use debundle_e2e_support::*;

fn independent_callbacks(wrapper: &str, callee: &str, namespace: bool) {
    let input = |callback: &str| {
        if namespace {
            format!("{{ __esModule: true, default: {callback} }}")
        } else {
            callback.to_string()
        }
    };
    let source = format!(
        r#"
{wrapper}
function selectedCallback() {{ return 41; }}
function unrelatedCallback() {{ return later; }}
const selectedInput = {};
const unrelatedInput = {};
const selected = {callee}(selectedInput);
const unrelated = {callee}(unrelatedInput);
const observed = selected();
const later = 7;
console.log(observed, unrelated());
"#,
        input("selectedCallback"),
        input("unrelatedCallback"),
    );
    let (stage, _fixture) = staged(
        FixtureOpts::new(
            &source,
            if namespace {
                // The guarded call also needs effect/normal-return proofs.
                // A source return relation alone does not grant those.
                vec![]
            } else {
                vec![logical_module(
                    "selected",
                    &[
                        Member::new("relay"),
                        Member::new("selectedCallback"),
                        Member::new("selectedInput"),
                        Member::new("selected"),
                        Member::new("observed"),
                    ],
                )]
            },
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(&stage.root().join("snapshot/static/app.js"), "41 7\n", "");
    let fresh = stage.run(&[]);
    let saved = stage.run(&["--analysis", path(&stage.artifact)]);
    for invocation in [&fresh, &saved] {
        assert!(
            invocation.result.status.success(),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "41 7\n",
            "",
        );
        let graph: DependencyGraphReport = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let observed = source_unit_for_binding(&graph, "observed");
        let unrelated = source_unit_for_binding(&graph, "unrelatedCallback");
        let later = source_unit_for_binding(&graph, "later");
        assert!(!graph.edges.iter().any(|edge|
            edge.source == observed && edge.target == later
                && edge.edge_kind == DepKind::EagerUse
                && matches!(&edge.role, Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) if *callee_source_unit == unrelated)
        ), "an independent return polluted the selected call");
    }
    assert_same_invocation_trees(&fresh, &saved);
}

#[test]
fn identity_result_allows_an_independent_initializer_to_move() {
    independent_callbacks("function relay(value) { return value; }", "relay", false);
}

#[test]
fn nested_calls_with_the_same_start_keep_their_own_arguments() {
    independent_callbacks(
        "function relay(value) { return value; }",
        "relay(relay)",
        false,
    );
}

#[test]
fn arrow_identity_result_allows_an_independent_initializer_to_move() {
    independent_callbacks("const relay = value => value;", "relay", false);
}

#[test]
fn default_export_result_keeps_independent_callback_origins() {
    independent_callbacks(
        "function relay(value) { return value && value.__esModule && Object.prototype.hasOwnProperty.call(value, 'default') ? value.default : value; }",
        "relay",
        true,
    );
}
