//! Node-anchored test: a 3-module shape where a non-residual
//! mediator reaches into a non-residual asymmetric I-cycle must
//! be REJECTED. Without rejection, the emitted JS TDZs at
//! runtime — the mediator's imports are sorted by
//! `linker_position` (dependency-first), DFS enters the cycle
//! via the dependency, the lazy back-edge fires the cycle, and
//! the dependent's body evaluates while the dependency is
//! mid-evaluation.
//!
//! ## Shape
//!
//! Three peeled modules; residual reaches the cycle only
//! through `mod_mediator`:
//!
//! - `mod_dep` owns `dep_value` (the eager-read target) and
//!   `lazy_reader` whose body lazily references `cross_value`
//!   (LazyUse back-edge).
//! - `mod_dependent` owns `cross_value`, whose initializer
//!   eager-reads `dep_value` — the EagerUse forward-edge.
//! - `mod_mediator` owns a function that reads `dep_value` lazily
//!   plus an at-init `mediator_init` constant. The materializer
//!   imports `dep_value` into `mod_mediator`'s emitted source, so
//!   `mod_mediator → mod_dep` is an I-edge.
//! - Residual reads `mediator_init` at-init (forcing residual to
//!   import `mod_mediator`) but does NOT directly reference
//!   `dep_value` or `cross_value` — residual reaches the SCC
//!   only via `mod_mediator`.
//!
//! Owner-graph cross-module edges:
//!
//! - `mod_dependent → mod_dep` `EagerUse(dep_value)` (constraining; eager forward)
//! - `mod_dep → mod_dependent` `LazyUse(cross_value)` (non-constraining; lazy back)
//! - `mod_mediator → mod_dep` `LazyUse(dep_value)` (non-constraining)
//! - `residual → mod_mediator` `EagerUse(mediator_init)` (constraining)
//!
//! I-graph SCC: `{mod_dep, mod_dependent}`. Residual is **not**
//! in the SCC. The only external entrant into the SCC is from
//! `mod_mediator`, which is **not** the residual — so Lemma 2's
//! reversal at entry does not apply to the cycle entry point.
//!
//! ## Why Lemma 2 fails here
//!
//! Residual's source_import_position-sorted imports: only
//! `mod_mediator` (residual doesn't import SCC members directly).
//! DFS into `mod_mediator`. `mod_mediator`'s imports are sorted
//! by `linker_position` (dependency-first). `linker_order` from
//! the constraining-edge subgraph puts `mod_dep` ahead of
//! `mod_dependent` (because the only constraining edge inside
//! the SCC is `mod_dependent → mod_dep`, so `mod_dep` is the
//! dependency).
//!
//! `mod_mediator` only imports `mod_dep` directly. DFS into
//! `mod_dep`. `mod_dep`'s imports include `mod_dependent` (lazy
//! back-edge). DFS into `mod_dependent`. `mod_dependent`'s
//! imports include `mod_dep` (eager) — on stack, cycle no-op.
//! `mod_dependent` body evaluates: `const cross_value =
//! dep_value + "-beta"` — but `dep_value` is in `mod_dep`, whose
//! body has not finished evaluating. **TDZ**:
//! `ReferenceError: Cannot access 'dep_value' before initialization`.
//!
//! ## Expected outcomes
//!
//! - **With the buggy (pre-145984d83) gate**: gate accepts; Node
//!   throws ReferenceError when running the emitted entry.
//! - **With the over-tight gate (commit 145984d83)**: gate
//!   rejects (correct).
//! - **With the precise gate (this PR)**: gate rejects (correct)
//!   — the rejection rule recognizes that the SCC has an
//!   external entrant from a non-residual mediator.

use debundle_e2e_support::*;

#[test]
fn three_module_mediator_into_asymmetric_cycle_is_rejected() {
    // Residual exports ONLY `mediator_init` so its only I-edge into
    // the {mod_dep, mod_dependent} SCC routes through `mod_mediator`.
    // The re-export of dep_value/cross_value/lazy_reader would
    // otherwise create direct residual→SCC I-edges that Lemma 2
    // exploits (entry's source_import_position-ordered imports DFS
    // straight into the SCC with the dependent first, rescuing
    // evaluation). Mediator-only entry isolates the failure shape.
    let opts = FixtureOpts::new(
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
    );
    expect_rejection(opts, &["cycle", "mod_dep", "mod_dependent"]);
}
