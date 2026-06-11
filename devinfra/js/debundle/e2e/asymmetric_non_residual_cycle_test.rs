//! Sibling of `lemma_two_rescued_asymmetric_cycle_test`: the gate
//! must **accept** an asymmetric I-cycle between two non-residual
//! modules when residual is the only outside importer, because
//! Lemma 2's `source_import_position` reversal at residual makes
//! the linker DFS unwind through the dependency first.
//!
//! ## Shape
//!
//! Two peeled modules, neither of them residual:
//!
//! - `mod_a` owns a TDZ-locked declaration plus a function whose
//!   body lazily references a binding declared in `mod_b`.
//! - `mod_b` owns a binding whose initializer eager-reads the
//!   declaration in `mod_a` at top level.
//!
//! Owner-graph edges (`(from, to)` = `from` reads `to`):
//!
//! - `mod_b → mod_a` `EagerUse` (constraining)
//! - `mod_a → mod_b` `LazyUse`  (non-constraining)
//!
//! The constraining-edge subgraph is a single arrow, no cycle.
//! The I-graph (constraining ∪ lazy) has a 2-cycle
//! `{mod_a, mod_b}` that does NOT contain `residual`.
//!
//! Residual reads `entry_value`, `cross_value`, and calls
//! `lazy_reader()` at-init via the `console.log`, plus
//! re-exports each name. That makes residual the only outside
//! importer of the SCC.
//!
//! ## Why Lemma 2 rescues this
//!
//! Entry's `source_import_position` puts the SCC's dependent
//! (`mod_b`) first in source order, then `mod_a`. ESM DFS
//! enters `mod_b`, recurses into `mod_a` via the eager edge,
//! `mod_a`'s lazy edge to `mod_b` hits an on-stack node (cycle
//! no-op), `mod_a` body evaluates, then `mod_b` body — with
//! `entry_value` already initialized. No TDZ.
//!
//! The 3-module-mediator companion test
//! (`mediator_reaches_asymmetric_cycle_test`) covers the
//! adversarial shape where Lemma 2 fails — a non-residual
//! mediator's `linker_position`-sorted imports DFS into the
//! dependency first and the cycle TDZs.

use debundle_e2e_support::*;

#[test]
fn asymmetric_non_residual_i_cycle_with_only_residual_entrant_runs_under_node() {
    // mod_a = {entry_value, lazy_reader}
    //   - entry_value is a TDZ-locked const (target of the
    //     EagerUse back into mod_a).
    //   - lazy_reader's body lazily reads `cross_value` (in
    //     mod_b), creating the non-constraining `mod_a → mod_b`
    //     I-edge that closes the cycle.
    //
    // mod_b = {cross_value}
    //   - cross_value's initializer eager-reads `entry_value`
    //     from mod_a — the constraining `mod_b → mod_a` edge.
    //
    // Residual statements: an at-init `console.log` that
    // exercises both modules at runtime. The export list
    // re-exports every binding so the materializer wires up
    // entry imports for both modules — residual is the only
    // outside importer of the SCC, satisfying the Lemma 2
    // rescue condition.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const entry_value = "alpha";
const cross_value = entry_value + "-beta";
function lazy_reader() { return cross_value; }
console.log(entry_value, cross_value, lazy_reader());
export { entry_value, cross_value, lazy_reader };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::new("entry_value"), Member::new("lazy_reader")],
            ),
            logical_module("mod_b", &[Member::new("cross_value")]),
        ],
    ));
    assert_entry_output(&fixture, "alpha alpha-beta alpha-beta\n");
}
