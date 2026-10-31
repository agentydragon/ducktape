//! Translation validation (migration step 1) against the realizability gate
//! on the real pipeline. Every e2e test also runs with `DEBUNDLE_TV_ORACLE`,
//! which fails the run when the two verdicts disagree; these fixtures pin the
//! evidence each side reports.

use debundle_e2e_support::*;
use serde_json::Value;

fn edges(report: &Value, key: &str) -> usize {
    report[key]
        .as_array()
        .unwrap_or_else(|| panic!("{key} must be an array: {report:#}"))
        .len()
}

#[test]
fn backward_reads_in_source_order_swap_nothing() {
    let fixture = run_fixture(FixtureOpts::new(
        "const a = 1;\nconst b = a + 1;\nconsole.log(b);\nexport { a, b };\n",
        vec![logical_module("mod_a", &[Member::new("a")])],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
    let report = fixture.translation_validation();
    assert_eq!(report["gate_accepts"], true, "{report:#}");
    assert_eq!(report["accepts"], true, "{report:#}");
    assert_eq!(report["swapped_pairs"], 0, "{report:#}");
}

#[test]
fn constraining_module_cycle_is_a_swapped_conflict() {
    let report = run_rejection_fixture(FixtureOpts::new(
        "const a = 1;\nconst b = a + 1;\nconst c = b + 1;\nconsole.log(c);\nexport { a, b, c };\n",
        vec![
            logical_module("mod_x", &[Member::new("a"), Member::new("c")]),
            logical_module("mod_y", &[Member::new("b")]),
        ],
    ))
    .translation_validation();
    assert_eq!(report["gate_accepts"], false, "{report:#}");
    assert_eq!(report["accepts"], false, "{report:#}");
    let conflicts = report["conflicts"].as_array().expect("conflicts");
    assert!(
        conflicts
            .iter()
            .any(|conflict| conflict["kind"] == "eager_use"
                && conflict["runs_first"]["unit"].as_u64()
                    > conflict["runs_later"]["unit"].as_u64()),
        "{report:#}"
    );
}

/// A read of a later `var` sees `undefined` in the source. The read is a
/// forward edge and the declaration's timeline edge points back at it, so
/// both verdicts keep the pair in one module.
#[test]
fn forward_var_read_split_is_rejected() {
    for source in [
        "const seen = String(later);\nvar later = \"set\";\nconsole.log(seen);\nexport { seen, later };\n",
        "const seen = readLater();\nfunction readLater() { return String(later); }\nvar later = \"set\";\nconsole.log(seen);\nexport { seen, readLater, later };\n",
    ] {
        let report = run_rejection_fixture(FixtureOpts::new(
            source,
            vec![
                logical_module("reader", &[Member::new("seen")]),
                logical_module("values", &[Member::new("later")]),
            ],
        ))
        .translation_validation();
        assert_eq!(report["gate_accepts"], false, "{report:#}");
        assert_eq!(report["accepts"], false, "{report:#}");
        assert_eq!(edges(&report, "placement_splits"), 0, "{report:#}");
    }
}

/// The at-init call reads `value` before its declaration in the source too.
/// The emit keeps that order, so no pair swaps; the forward edge counts as
/// violated for the gate and for step 1, though swapped-pair semantics would
/// accept this order.
#[test]
fn forward_at_init_read_kept_in_source_order_is_reported() {
    let report = run_rejection_fixture(FixtureOpts::new(
        "const seed = 7;\nconst result = readValue(seed);\nfunction readValue(x) { return value + x; }\nconst value = 42;\nconsole.log(result);\nexport { seed, result, readValue, value };\n",
        vec![
            logical_module("readers", &[Member::new("readValue")]),
            logical_module("values", &[Member::new("value")]),
        ],
    ))
    .translation_validation();
    assert_eq!(report["gate_accepts"], false, "{report:#}");
    assert_eq!(report["accepts"], false, "{report:#}");
    assert_eq!(edges(&report, "conflicts"), 0, "{report:#}");
    assert_eq!(edges(&report, "forward_edges_kept"), 1, "{report:#}");
}
