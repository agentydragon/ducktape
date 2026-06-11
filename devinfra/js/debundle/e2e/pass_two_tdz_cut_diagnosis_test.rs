//! Pass-2 (ESM-evaluation-simulator) rejections must be diagnosable:
//! the bail summary names the TDZ binding pair and the lazy
//! back-edge that closes the I-cycle, and `cycles.json` carries a
//! non-empty `cut` naming the violated at-init read.
//!
//! The fixture is a residual-in-SCC shape (the canonical remaining
//! Pass-2 rejection now that the simulator models the entry's
//! universal per-plan imports): the constraining subgraph of the
//! `{residual, mod_dependent}` SCC is acyclic (one forward
//! `EagerUse` into residual, one lazy back-edge out of residual), so
//! a feedback-arc-set over constraining edges alone finds nothing to
//! cut. The cut must instead come from the simulator's violated
//! post-order check: `cross_value` (mod_dependent) at-init reads
//! `seed_value`, which stays in the entry file — the ESM DFS root,
//! whose body always evaluates last.

use debundle_e2e_support::*;
use serde_json::Value;

// No impure residual statement may follow `cross_value` in source
// order: `cross_value`'s initializer is conservatively impure (the
// `+` can fire `valueOf`), and a later impure residual statement
// would add a Sequenced residual→mod_dependent edge, upgrading the
// rejection to a Pass-1 mutual constraining cycle — a different
// (FAS-cut) diagnostic path than the Pass-2 simulator cut this test
// pins.
fn residual_cycle_fixture<'a>() -> FixtureOpts<'a> {
    FixtureOpts::new(
        r#"const seed_value = "alpha";
const cross_value = seed_value + "-beta";
function lazy_back() { return cross_value; }
export { seed_value, lazy_back };
"#,
        vec![logical_module(
            "mod_dependent",
            &[Member::new("cross_value")],
        )],
    )
    // Inline mode keeps `seed_value` / `lazy_back` in the entry file
    // (the partition's residual), which is what makes the eager read
    // target the DFS root rather than an ordinary sibling module.
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn pass_two_rejection_summary_names_tdz_binding_pair_and_lazy_closure() {
    // The summary must blame the violated at-init read as a binding
    // pair (reader binding, target binding, both module names) and
    // name the lazy read that closes the I-cycle — not print
    // "0 R/S edge(s)" with no rows, which is what the FAS-only cut
    // computation produced for asymmetric (Pass-2) rejections.
    expect_rejection_containing_all(
        residual_cycle_fixture(),
        &[
            "unrealizable",
            "cross_value",
            "seed_value",
            "at-init",
            "mod_dependent",
            "closes through lazy read",
            "lazy_back",
        ],
    );
}

#[test]
fn pass_two_rejection_cycles_json_has_nonempty_cut() {
    let rejected = run_rejection_fixture(residual_cycle_fixture());
    let cycles_path = rejected
        .report_root
        .join("static")
        .join("app")
        .join("cycles.json");
    let cycles: Vec<Value> = read_json(&cycles_path);
    assert_eq!(cycles.len(), 1, "expected one blocking SCC: {cycles:?}");
    let entry = &cycles[0];
    let modules: Vec<&str> = entry["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert!(
        modules.iter().any(|m| m.contains("mod_dependent")),
        "blocking SCC must list the I-cycle members: {modules:?}",
    );
    let cut = entry["cut"].as_array().unwrap();
    assert!(
        !cut.is_empty(),
        "Pass-2 rejection must carry a non-empty cut (the violated at-init reads): {entry}",
    );
    // The cut names the violated constraining edge with both bindings.
    assert!(
        cut.iter().any(|edge| {
            edge["binding"].as_str() == Some("seed_value")
                && edge["from_binding"].as_str() == Some("cross_value")
                && edge["kind"].as_str() == Some("eager_use")
        }),
        "cut must name the violated at-init read cross_value -> seed_value: {cut:?}",
    );
}
