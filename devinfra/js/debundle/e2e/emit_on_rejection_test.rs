//! `debundle run --emit-on-rejection`: a spec the realizability gate rejects
//! still writes its logical-module tree for reading, marked unrunnable
//! (SPEC.md § Accepted output). Without the flag the same rejection writes
//! diagnostics only.

use debundle_e2e_support::*;

// The residual ↔ aggregator at-init cycle of
// `namespace_aggregator_split_tdz_test`: `helper` stays in the entry, the
// sub-objects read it at init, and the entry reads the aggregator at init.
const AGGREGATOR_SOURCE: &str = r#"const helper = "H";
const sub_a = { foo: helper + "1" };
const sub_b = { bar: helper + "2" };
const ids = { ...sub_a, ...sub_b };
const consumed = ids.foo + "|" + ids.bar;
console.log(consumed);
export { ids, sub_a, sub_b, helper };
"#;

const MODULE_FILES: [&str; 3] = [
    "static/app/modules/ids/index.js",
    "static/app/modules/ids/sub_a.js",
    "static/app/modules/ids/sub_b.js",
];

fn rejected_split() -> FixtureOpts<'static> {
    FixtureOpts::new(
        AGGREGATOR_SOURCE,
        vec![
            logical_module("ids/index", &[Member::new("ids")]),
            logical_module("ids/sub_a", &[Member::new("sub_a")]),
            logical_module("ids/sub_b", &[Member::new("sub_b")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_browser_harness()
}

/// Co-locating `helper` with the aggregator leaves only the entry's eager
/// read of `ids`: acyclic, so the gate accepts it.
fn accepted_split() -> FixtureOpts<'static> {
    FixtureOpts::new(
        AGGREGATOR_SOURCE,
        vec![logical_module(
            "ids/index",
            &[
                Member::new("ids"),
                Member::new("sub_a"),
                Member::new("sub_b"),
                Member::new("helper"),
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_browser_harness()
}

#[test]
fn rejection_writes_marked_unrunnable_tree_without_harness() {
    let rejected = run_emit_on_rejection_fixture(rejected_split());
    let cycles = rejected.report_path("cycles.json");
    assert!(cycles.is_file(), "gate evidence is still written");

    let app_root = rejected.app_root();
    for module in MODULE_FILES {
        assert!(app_root.join(module).is_file(), "missing {module}");
    }
    assert_module_source(&app_root, MODULE_FILES[1], &["sub_a"], &[]);
    assert_gate_rejected_banner(&rejected.entry_path(), &cycles);

    let marker = std::fs::read_to_string(rejected.gate_rejected_marker())
        .expect("GATE_REJECTED marker at the output root");
    assert!(
        marker.contains(cycles.to_str().unwrap()),
        "marker must name the gate report:\n{marker}"
    );

    assert!(
        !rejected.harness_root.as_ref().unwrap().exists(),
        "a rejected program gets no browser harness"
    );
}

/// A harness-only spec gets the reading-only tree where its harness would go.
#[test]
fn rejection_without_js_tree_writes_tree_to_harness_dir() {
    let rejected = run_emit_on_rejection_fixture(
        rejected_split().with_raw_spec_key("write_js_tree", serde_json::Value::Null),
    );
    let harness_root = rejected.harness_root.as_ref().unwrap();
    assert!(harness_root.join("GATE_REJECTED").is_file());
    for module in MODULE_FILES {
        assert!(
            harness_root.join("app").join(module).is_file(),
            "missing {module}"
        );
    }
    assert!(!harness_root.join("app/bootstrap.js").exists());
    assert!(!rejected.app_root().exists());
}

/// A spec error beside the gate's rejection fails the run as a plain refusal,
/// so nothing mistakes it for a reading-only tree.
#[test]
fn rejection_with_unresolved_claim_writes_no_tree() {
    let opts = FixtureOpts::new(
        AGGREGATOR_SOURCE,
        vec![
            logical_module("ids/index", &[Member::new("ids")]),
            logical_module(
                "ids/sub_a",
                &[Member::new("sub_a"), Member::new("undeclared_binding")],
            ),
            logical_module("ids/sub_b", &[Member::new("sub_b")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_browser_harness();
    let rejected = run_emit_on_rejection_failure_fixture(opts);
    assert!(
        rejected.report_path("cycles.json").is_file(),
        "the gate ran"
    );
    assert!(rejected.stderr.contains("undeclared_binding"));
    assert!(!rejected.gate_rejected_marker().exists());
    assert!(!rejected.app_root().exists());
}

#[test]
fn rejection_without_flag_writes_no_tree() {
    let rejected = run_rejection_fixture(rejected_split());
    assert!(rejected.report_path("cycles.json").is_file());
    assert!(!rejected.app_root().exists());
    assert!(!rejected.gate_rejected_marker().exists());
    assert!(!rejected.harness_root.as_ref().unwrap().exists());
}

/// Anchor for the harness assertions above: the same fixture's browser
/// harness is written once the gate accepts.
#[test]
fn accepted_split_writes_browser_harness() {
    let fixture = run_fixture(accepted_split());
    let harness_root = fixture.harness_root.as_ref().unwrap();
    assert!(harness_root.join("app/bootstrap.js").is_file());
    assert!(harness_root.join("app/index.html").is_file());
}
