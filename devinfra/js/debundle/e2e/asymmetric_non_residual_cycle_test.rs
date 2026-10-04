//! Sibling of `lemma_two_rescued_asymmetric_cycle_test` (residual references
//! both sides of the cycle): the gate must accept an asymmetric I-cycle
//! between two non-residual modules whose only residual entrant references the
//! dependency side, and the emitted entry must run under Node.
//!
//! The 3-module-mediator companion test
//! (`mediator_reaches_asymmetric_cycle_test`) covers the shape
//! where residual's own statements never reference the SCC and a
//! non-residual mediator reaches into it — also accepted, because
//! the entry's universal per-plan imports DFS into the SCC at the
//! dependent before the mediator's dependency-first imports can.

use debundle_e2e_support::*;

// An asymmetric I-cycle whose only residual-side reference points at the
// constraining edge's TARGET (the dependency), never the source (the
// dependent), is accepted and runs under Node.
//
// Shape:
//
// - `mod_schemas` owns `schemas_target` (eager-read target) and
//   `lazy_back` (lazy back-edge into `mod_ids`).
// - `mod_ids` owns `ids_val`, whose initializer eager-reads
//   `schemas_target`.
// - residual's only reference into the SCC is `console.log(schemas_target)`
//   — the dependency side. (No `export` statements: entry-side
//   re-exports add residual read edges of their own, which would
//   incidentally hand a gate a direct edge to the dependent and mask what
//   this test pins.)
//
// A gate that modeled residual's DFS fan-out as only the modules residual's
// statements reference would enter the SCC at `mod_schemas`, follow the
// emitted lazy-read import to `mod_ids`, and flag a TDZ. The emitted entry,
// however, imports EVERY plan in Lemma 2's source-import order — `mod_ids`
// (the dependent) first — so the runtime DFS unwinds through `mod_schemas`
// and evaluates it before `mod_ids`. If the order were wrong, `ids_val`'s
// initializer would throw a TDZ ReferenceError during loading and the
// asserted stdout would never be produced.
#[test]
fn dependency_only_residual_reference_into_asymmetric_cycle_runs_under_node() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const schemas_target = "v";
function lazy_back() { return ids_val; }
const ids_val = schemas_target;
console.log(schemas_target);
"#,
        vec![
            logical_module(
                "mod_schemas",
                &[Member::new("schemas_target"), Member::new("lazy_back")],
            ),
            logical_module("mod_ids", &[Member::new("ids_val")]),
        ],
    ));
    assert_entry_output(&fixture, "v\n");
}
