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
//! (`mediator_reaches_asymmetric_cycle_test`) covers the shape
//! where residual's own statements never reference the SCC and a
//! non-residual mediator reaches into it — also accepted, because
//! the entry's universal per-plan imports DFS into the SCC at the
//! dependent before the mediator's dependency-first imports can.

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

// Node-anchored regression test (originally RED) for the gaffer
// over-rejection: an asymmetric I-cycle whose only residual-side
// reference points at the constraining edge's TARGET (the
// dependency), never the source (the dependent).
//
// Shape (gaffer's `domains/system/ids` ↔ `domains/system/schemas`
// minimal repro; unit-level twin:
// `realizability::tests::pass_two_simulator_models_entry_universal_imports_for_runtime_dfs`):
//
// - `mod_schemas` owns `schemas_target` (eager-read target) and
//   `lazy_back` (lazy back-edge into `mod_ids`).
// - `mod_ids` owns `ids_val`, whose initializer eager-reads
//   `schemas_target`.
// - residual's only reference into the SCC is `console.log(schemas_target)`
//   — the dependency side. (No `export` statements: entry-side
//   re-exports add residual read edges of their own, which would
//   incidentally hand the old gate a direct edge to the dependent
//   and mask the over-rejection this test pins.)
//
// The old gate modeled residual's DFS fan-out as only the modules
// residual's statements reference, entered the SCC at `mod_schemas`,
// followed the emitted lazy-read import to `mod_ids`, and flagged a
// TDZ. The emitted entry, however, imports EVERY plan in Lemma 2's
// source-import order — `mod_ids` (the dependent) first — so the
// runtime DFS unwinds through `mod_schemas` and evaluates it before
// `mod_ids`. If the order were wrong, `ids_val`'s initializer would
// throw a TDZ ReferenceError during loading and the asserted stdout
// would never be produced.
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
