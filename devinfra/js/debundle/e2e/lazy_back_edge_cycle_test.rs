//! Cycle through a lazy back-edge: mod_a's body reads from mod_b
//! at-init, mod_b's body reads from mod_a only inside a function
//! body. The at-init read graph `R` is acyclic (one edge mod_b →
//! mod_a); the linker's import graph `I` is cyclic (the function
//! body's lazy read still emits an `import` directive, closing the
//! loop).
//!
//! This is the minimal synthetic shape of the regression that
//! surfaced in Tana's live-proxy run as `command_schema →
//! vendor/symbols → prompting_runtime → command_schema`. The old
//! validator (built from `R` only) passes the spec; the bundle
//! the materializer emits has a 2-module SCC in the linker's
//! import graph and a TDZ-prone evaluation order.
//!
//! The strict gating rule (DESIGN.md "The realizability theorem")
//! rejects every cycle in `I ∪ S`, including ones whose
//! at-init projection is acyclic. Phase 5 makes the validator
//! enforce this. This test is the contract for the fix: today it
//! fails (no rejection); after Phase 5 it passes.

use debundle_e2e_support::*;

#[ignore = "phase 5: un-ignore once build_module_dep_graph walks reads_lazy too"]
#[test]
fn rejects_cycle_through_lazy_back_edge() {
    expect_logical_modules_e2e_rejection(
        FixtureOpts::new(
            // mod_b reads A from mod_a at-init (B's initializer);
            // mod_a's `readB` body reads B from mod_b lazily.
            r#"const A = "a-value";
function readB() { return B; }
const B = A + "-postfix";
console.log(readB());
export { A, B, readB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["cycle", "mod_a", "mod_b"],
    );
}
