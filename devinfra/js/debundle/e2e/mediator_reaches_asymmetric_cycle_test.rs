//! Node-anchored test: a 3-module shape where a non-residual
//! mediator reaches into a non-residual asymmetric I-cycle is
//! ACCEPTED and runs cleanly under Node.
//!
//! ## Shape
//!
//! Three peeled modules; residual's own statements reach the cycle
//! only through `mod_mediator`:
//!
//! - `mod_dep` owns `dep_value` (the eager-read target) and
//!   `lazy_reader` whose body lazily references `cross_value`
//!   (LazyUse back-edge).
//! - `mod_dependent` owns `cross_value`, whose initializer
//!   eager-reads `dep_value` — the EagerUse forward-edge.
//! - `mod_mediator` owns a function that reads `dep_value` lazily
//!   plus an at-init `mediator_init` constant.
//! - Residual reads `mediator_init` at-init but does NOT directly
//!   reference `dep_value` or `cross_value`.
//!
//! ## Why this is realizable
//!
//! The emitted entry imports EVERY logical module — `mod_dep` and
//! `mod_dependent` included — regardless of whether residual's own
//! statements reference their bindings, and orders those imports by
//! `source_import_position` (Lemma 2). The SCC's dependent
//! (`mod_dependent`) therefore appears in entry's import list BEFORE
//! any mediator can reach the SCC through its dependency-first
//! imports: ESM DFS enters the SCC at `mod_dependent`, recurses into
//! `mod_dep` via the eager edge, the lazy back-edge hits an on-stack
//! node (cycle no-op), and post-order evaluates `mod_dep` before
//! `mod_dependent`. By the time `mod_mediator`'s body runs, the SCC
//! has finished evaluating.
//!
//! ## History
//!
//! An earlier gate modeled residual's DFS fan-out as only the modules
//! residual's own statements reference, concluded the DFS could only
//! enter this SCC through the mediator's dependency-first imports
//! (which WOULD TDZ), and rejected the spec — an over-rejection,
//! since the emitted entry has always imported every binding-owning
//! plan. The gate's evaluation simulator now models the entry's
//! universal imports (`realizability::EsmIGraph`), shares the
//! emitter's exact ordering (`esm_import_order::EsmImportOrder`),
//! and accepts; this test pins acceptance AND the runtime behavior
//! under Node.

use debundle_e2e_support::*;

#[test]
fn three_module_mediator_into_asymmetric_cycle_runs_under_node() {
    // Residual exports ONLY `mediator_init`, so residual's own
    // statements give it no direct I-edge into the
    // {mod_dep, mod_dependent} SCC — the shape that used to be
    // over-rejected. The entry's universal per-plan imports are what
    // make Lemma 2's reversal reach the SCC first.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() { return cross_value; }
function mediator_helper() { return dep_value + "-via-mediator-" + lazy_reader(); }
const mediator_init = mediator_helper();
console.log(mediator_init);
export { mediator_init };
"#,
        vec![
            logical_module(
                "mod_dep",
                &[Member::new("dep_value"), Member::new("lazy_reader")],
            ),
            logical_module("mod_dependent", &[Member::new("cross_value")]),
            logical_module(
                "mod_mediator",
                &[Member::new("mediator_helper"), Member::new("mediator_init")],
            ),
        ],
    ));
    assert_entry_output(&fixture, "alpha-via-mediator-alpha-beta\n");
}
