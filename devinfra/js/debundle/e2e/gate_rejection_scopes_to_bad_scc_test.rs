//! A gate rejection must blame ONLY the SCCs the realizability
//! verdict diagnosed as unrealizable. A rescued asymmetric I-cycle
//! (Lemma 2's source-import reversal makes it evaluate without TDZ;
//! the gate's Pass-2 simulator accepts it) that happens to coexist
//! with a genuinely unrealizable SCC in the same chunk must NOT be
//! reported as a blocker alongside the real one.
//!
//! Shape: one chunk combining two disjoint asymmetric I-cycles that
//! differ only in where the eager read's target lives:
//!
//! - the rescued pair (`mod_ok_dep` / `mod_ok_dependent`): eager
//!   forward edge, lazy back-edge, both targets non-residual — the
//!   shape `lemma_two_rescued_asymmetric_cycle_test` pins as
//!   accepted, and
//! - the broken pair (`mod_bad_dependent` + residual): `bad_cross`
//!   eager-reads `bad_seed`, which stays in the entry file (inline
//!   mode) — the ESM DFS root, whose body always evaluates last —
//!   while residual's `bad_lazy_back` closes the I-cycle with a lazy
//!   read. The shape `runtime_tdz_on_imported_class_test` and
//!   `pass_two_tdz_cut_diagnosis_test` pin as a Pass-2
//!   (ESM-evaluation-simulator) rejection.
//!
//! Both real blockers must stay Pass-2 (no mutual constraining
//! cycle): a Pass-1 SCC anywhere empties the chunk-wide constraining
//! linker order (`chunk_linker_order_from_pairs` returns `[]` on a
//! cyclic input), which degrades the simulator's import ordering for
//! every other SCC and makes the gate itself (correctly, mirroring
//! emit) reject otherwise-rescued shapes.
//!
//! The historical bug: once ANY SCC was unrealizable, the validator
//! re-walked every quotient SCC (`verdict.scc_partition()`) and
//! reported each one carrying any constraining edge between members
//! — over-reporting the rescued pair as a second blocker.

use debundle_e2e_support::*;
use serde_json::Value;

// `console.log` (impure, residual) must precede `bad_cross` in source
// order: `bad_cross`'s initializer is conservatively impure, and an
// impure residual statement AFTER it would add a Sequenced
// residual→mod_bad_dependent edge, upgrading the broken pair to a
// Pass-1 mutual constraining cycle (which empties the chunk-wide
// linker order and degrades the rescued pair too).
fn combined_fixture<'a>() -> FixtureOpts<'a> {
    FixtureOpts::new(
        r#"const ok_dep = "alpha";
const ok_cross = ok_dep + "-beta";
function ok_lazy_reader() { return ok_cross; }
console.log(ok_dep, ok_cross, ok_lazy_reader());
const bad_seed = "gamma";
const bad_cross = bad_seed + "-delta";
function bad_lazy_back() { return bad_cross; }
export { ok_dep, ok_cross, ok_lazy_reader, bad_seed, bad_lazy_back };
"#,
        vec![
            logical_module(
                "mod_ok_dep",
                &[Member::new("ok_dep"), Member::new("ok_lazy_reader")],
            ),
            logical_module("mod_ok_dependent", &[Member::new("ok_cross")]),
            logical_module("mod_bad_dependent", &[Member::new("bad_cross")]),
        ],
    )
    // Inline mode keeps `bad_seed` / `bad_lazy_back` in the entry
    // file (the partition's residual) so the broken pair's eager
    // read targets the DFS root.
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn rescued_asymmetric_scc_is_not_reported_alongside_real_blocker() {
    let rejected = run_rejection_fixture(combined_fixture());
    let stderr_lower = rejected.stderr.to_lowercase();
    for required in ["unrealizable", "mod_bad_dependent", "bad_seed"] {
        assert!(
            stderr_lower.contains(required),
            "stderr must blame the residual-targeting SCC ({required:?} missing):\n{}",
            rejected.stderr,
        );
    }
    assert!(
        !stderr_lower.contains("mod_ok"),
        "rescued asymmetric SCC (mod_ok_dep/mod_ok_dependent) must not be \
         reported as a blocker:\n{}",
        rejected.stderr,
    );

    let cycles_path = rejected
        .report_root
        .join("static")
        .join("app")
        .join("cycles.json");
    let cycles: Vec<Value> = read_json(&cycles_path);
    assert_eq!(
        cycles.len(),
        1,
        "only the genuinely unrealizable SCC may appear in cycles.json: {cycles:?}",
    );
    let modules: Vec<&str> = cycles[0]["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert!(
        modules.iter().any(|m| m.contains("mod_bad_dependent")),
        "blocking entry must list the residual-targeting I-cycle members: {modules:?}",
    );
    assert!(
        modules.iter().all(|m| !m.contains("mod_ok")),
        "rescued SCC members leaked into the blocking entry: {modules:?}",
    );
}
