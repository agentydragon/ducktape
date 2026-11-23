//! Behavioral controls for numeric member projection: preserve an already
//! accepted literal-key cut and refuse unsafe numeric, named-key and coercion
//! cuts. Source-flow precision and work bounds are checked in the facts tests.

use debundle_e2e_support::saved_analysis::{
    StagedInvocation, assert_same_invocation_trees, graph_node_for_binding, path, staged,
};
use debundle_e2e_support::*;
use serde_json::Value;

#[derive(Clone, Copy)]
enum Read {
    NumericLiteral,
    CountedLoop,
    ReusedLoopIndex,
    CapturedLoopIndex,
    Subtraction,
    LengthMinusOne,
    ConcatenatedName,
    NanSpelling,
    EffectfulCoercion,
}

impl Read {
    fn body(self) -> &'static str {
        match self {
            Self::NumericLiteral => "return items[0];",
            Self::CountedLoop => {
                "for (var slot = 0; slot < items.length; slot++) { return items[slot]; }"
            }
            Self::ReusedLoopIndex => {
                "for (var slot = 0; slot < items.length; slot++) { void items[slot]; } slot = 'named'; return items[slot];"
            }
            Self::CapturedLoopIndex => {
                "const mutate = () => { slot = 'named'; }; for (var slot = 0; slot < items.length; slot++) { mutate(); return items[slot]; }"
            }
            Self::Subtraction => "return items[index - 0];",
            Self::LengthMinusOne => "return items[items.length - 1];",
            Self::ConcatenatedName => "return items[propertyName + \"\"];",
            Self::NanSpelling => "index = \"not-a-number\"; return items[index - 0];",
            Self::EffectfulCoercion => "return items[key - 0];",
        }
    }

    fn source(self, actual_hook: &str) -> String {
        let named_properties = match self {
            Self::NanSpelling => ", NaN: unrelatedCallback",
            Self::EffectfulCoercion => "",
            _ => ", named: unrelatedCallback",
        };
        let coercion_key = if matches!(self, Self::EffectfulCoercion) {
            "const key = { valueOf() { return later ? 0 : 0; } };"
        } else {
            ""
        };
        format!(
            r#"
function pick(items, index) {{ {} }}
const earlier = "selected";
function selectedCallback() {{ return earlier; }}
function unrelatedCallback() {{ return later; }}
const propertyName = "named";
{}
const items = {{ 0: {}, length: 1{} }};
const later = "later";
const observed = pick(items, 0)();
const back = observed;
console.log(observed, back);
"#,
            self.body(),
            coercion_key,
            actual_hook,
            named_properties,
        )
    }
}

fn fixture(source: &str) -> FixtureOpts<'_> {
    let mut use_members = vec![
        Member::new("pick"),
        Member::new("earlier"),
        Member::new("selectedCallback"),
        Member::new("unrelatedCallback"),
        Member::new("propertyName"),
        Member::new("items"),
        Member::new("observed"),
    ];
    if source.contains("const key = {") {
        use_members.push(Member::new("key"));
    }
    FixtureOpts::new(
        source,
        vec![
            logical_module("use", &use_members),
            logical_module("state", &[Member::new("later"), Member::new("back")]),
        ],
    )
    .with_import_order(&["state", "use"])
    .with_unassigned_mode(unassigned_mode_inline())
}

fn assert_rejection_cycle(invocation: &StagedInvocation, label: &str) {
    let report = invocation
        .output_root()
        .join("reports/tree/static/app/cycles.json");
    let cycles: Vec<Value> = read_json(&report);
    assert!(
        cycles.iter().any(|cycle| {
            let modules = cycle["modules"].as_array();
            let has_module = |name: &str| {
                modules.is_some_and(|modules| {
                    modules.iter().any(|module| module.as_str() == Some(name))
                })
            };
            let witness = &cycle["pass_one_witness"];
            cycle["rejection"] == "mutual_constraining_cycle"
                && has_module("use")
                && has_module("state")
                && witness["complete"] == true
                && witness["steps"].as_array().is_some_and(|edges| {
                    edges
                        .iter()
                        .any(|edge| edge["kind"] == "eager_use" && edge["binding"] == "later")
                })
        }),
        "{label}: rejection did not carry the expected `use`/`state` eager-use TDZ cycle: {cycles:#?}"
    );
}

fn assert_fresh_and_saved_cli(
    label: &str,
    read: Read,
    actual_hook: &str,
    accepted: bool,
    expected_source: &str,
    expected_emitted: Option<&str>,
) {
    let source = read.source(actual_hook);
    let (stage, _) = staged(fixture(&source));
    assert_node_output(
        &stage.root().join("snapshot/static/app.js"),
        expected_source,
        "",
    );

    let fresh = stage.run(&["--emit-on-rejection"]);
    let saved = stage.run(&["--analysis", path(&stage.artifact), "--emit-on-rejection"]);
    for (kind, invocation) in [("fresh", &fresh), ("saved", &saved)] {
        let emitted_entry = invocation.output_root().join("app/static/app/entry.js");
        if let Some(expected) = expected_emitted {
            assert_node_output(&emitted_entry, expected, "");
        } else {
            assert_eq!(invocation.result.status.code(), Some(3));
            let report = invocation
                .output_root()
                .join("reports/tree/static/app/cycles.json");
            assert_rejection_cycle(invocation, label);
            assert_gate_rejected_banner(&emitted_entry, &report);
            assert_node_error(
                &emitted_entry,
                "Cannot access 'later' before initialization",
            );
        }
        if !accepted && expected_emitted.is_some() {
            // A precise source-origin relation does not provide the separate
            // synchronous member-read proof. Preserve this reduced blocker
            // until PF0's execution-fallback work replaces the receipt.
            assert_rejection_cycle(invocation, label);
            let graph: Value = read_gzip_json(
                &invocation
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let observed = graph_node_for_binding(&graph, "observed");
            assert!(
                observed["synchronous_execution"]["precision_losses"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|loss| {
                        loss["reason"]["code"] == "unsupported_operation"
                            && loss["reason"]["operation"] == "member_read"
                            && loss["reach"]["kind"] == "global"
                    })
            );
        }
        assert_eq!(
            invocation.result.status.success(),
            accepted,
            "{label} {kind} CLI acceptance: stdout={:?}, stderr={:?}",
            invocation.result.stdout,
            invocation.result.stderr
        );
    }
    assert_same_invocation_trees(&fresh, &saved);
}

#[test]
fn numeric_literal_key_preserves_an_accepted_cut() {
    assert_fresh_and_saved_cli(
        "numeric literal key",
        Read::NumericLiteral,
        "selectedCallback",
        true,
        "selected selected\n",
        Some("selected selected\n"),
    );
}

#[test]
fn actual_numeric_elements_keep_the_later_callback_dependency() {
    for (label, read) in [
        ("subtraction-derived element", Read::Subtraction),
        ("length-minus-one element", Read::LengthMinusOne),
    ] {
        assert_fresh_and_saved_cli(
            label,
            read,
            "unrelatedCallback",
            false,
            "later later\n",
            None,
        );
    }
    assert_fresh_and_saved_cli(
        "effectful numeric coercion",
        Read::EffectfulCoercion,
        "selectedCallback",
        false,
        "selected selected\n",
        None,
    );
}

#[test]
fn dynamic_and_non_finite_keys_keep_named_property_dependencies() {
    for (label, read) in [
        ("concatenated named key", Read::ConcatenatedName),
        ("NaN property spelling after coercion", Read::NanSpelling),
    ] {
        assert_fresh_and_saved_cli(
            label,
            read,
            "selectedCallback",
            false,
            "later later\n",
            None,
        );
    }
}

#[test]
fn computed_key_cut_retains_the_independent_execution_fallback() {
    for (label, read) in [
        ("computed subtraction safe cut", Read::Subtraction),
        ("computed length safe cut", Read::LengthMinusOne),
    ] {
        assert_fresh_and_saved_cli(
            label,
            read,
            "selectedCallback",
            false,
            "selected selected\n",
            Some("selected selected\n"),
        );
    }
}

#[test]
fn loop_projection_preserves_numeric_callback_and_index_invalidation_dependencies() {
    assert_fresh_and_saved_cli(
        "counted-loop numeric callback",
        Read::CountedLoop,
        "unrelatedCallback",
        false,
        "later later\n",
        None,
    );
    for (label, read) in [
        ("index reused after loop", Read::ReusedLoopIndex),
        (
            "index changed by a captured writer",
            Read::CapturedLoopIndex,
        ),
    ] {
        assert_fresh_and_saved_cli(
            label,
            read,
            "selectedCallback",
            false,
            "later later\n",
            None,
        );
    }
}
