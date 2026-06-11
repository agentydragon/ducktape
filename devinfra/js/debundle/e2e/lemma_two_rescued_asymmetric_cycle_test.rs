//! Node-anchored test: a 2-module asymmetric I-cycle where every
//! cross-SCC entrant is the residual (entry) must be accepted by
//! the gate AND run successfully under Node — Lemma 2's
//! `source_import_position` reversal puts the SCC's dependent
//! first in entry's imports, ESM DFS unwinds via the dependency,
//! and the eager read fires after its target's body evaluates.
//!
//! ## Shape
//!
//! Two peeled modules, residual is the only outside importer:
//!
//! - `mod_dep` owns `dep_value` (the eager-read target) and
//!   `lazy_reader` (whose function body lazily references
//!   `cross_value` in `mod_dependent` — the LazyUse back-edge).
//! - `mod_dependent` owns `cross_value`, whose initializer
//!   eager-reads `dep_value` from `mod_dep` at top level — the
//!   EagerUse forward-edge.
//! - residual reads `dep_value`, `cross_value`, and calls
//!   `lazy_reader()` at-init via the `console.log`.
//!
//! Owner-graph cross-module edges:
//!
//! - `mod_dependent → mod_dep` `EagerUse(dep_value)` (constraining)
//! - `mod_dep → mod_dependent` `LazyUse(cross_value)` (non-constraining)
//! - `residual → mod_dep` `EagerUse` (constraining; from console.log)
//! - `residual → mod_dependent` `EagerUse` (constraining; from console.log)
//!
//! I-graph SCC: `{mod_dep, mod_dependent}`. Residual is **not** in
//! the SCC (no path back from the SCC to residual). All
//! cross-SCC incoming edges are from residual.
//!
//! ## Why Lemma 2 rescues this
//!
//! `linker_order` over the constraining-edge subgraph: only
//! constraining cross-module edge inside the SCC is
//! `mod_dependent → mod_dep`, so `mod_dep` evaluates first by
//! the toposort. `source_import_position` sorts entry's imports
//! by (SCC rank ASC, intra-SCC linker_position DESC), reversing
//! within the SCC. Entry's import list becomes
//! `[mod_dependent, mod_dep]`.
//!
//! ESM Phase-2 DFS from entry:
//!   1. Visit `mod_dependent` (first import). Its imports include
//!      `mod_dep` (eager). DFS into `mod_dep`.
//!   2. `mod_dep`'s imports include `mod_dependent` (lazy back).
//!      `mod_dependent` is on the DFS stack → cycle no-op.
//!   3. `mod_dep` body evaluates: `const dep_value = "alpha"`.
//!   4. Back to `mod_dependent`, body evaluates:
//!      `const cross_value = dep_value + "-beta"` — `dep_value`
//!      is now initialized. No TDZ.
//!   5. Back to entry. `mod_dep` is the next import but already
//!      evaluated; skip. Entry body runs. `console.log(...)`
//!      sees all bindings initialized.
//!
//! ## Expected outcomes
//!
//! - **Today (RED)**: the over-tightened gate rejects this
//!   shape unconditionally — the second Tarjan pass over the
//!   I-graph reports the `{mod_dep, mod_dependent}` SCC and
//!   bails because it contains a constraining edge.
//! - **After the fix**: the gate recognizes that every external
//!   entrant into the SCC is residual, so Lemma 2's reversal
//!   applies; gate accepts; Node runs the emitted entry and
//!   prints `alpha alpha-beta alpha-beta`.

use debundle_e2e_support::*;

#[test]
fn two_module_asymmetric_cycle_with_only_residual_entrant_runs_under_node() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() { return cross_value; }
console.log(dep_value, cross_value, lazy_reader());
export { dep_value, cross_value, lazy_reader };
"#,
        vec![
            logical_module(
                "mod_dep",
                &[Member::new("dep_value"), Member::new("lazy_reader")],
            ),
            logical_module("mod_dependent", &[Member::new("cross_value")]),
        ],
    ));
    assert_entry_output(&fixture, "alpha alpha-beta alpha-beta\n");
}
