//! Regression test: at-init call promotion (and the direct
//! `lazy_rebind` owner edge) must not propagate rebinds that
//! lexically appear inside a nested closure but never fire at
//! module-initialization time.
//!
//! Background: docs/design.md "At-init call promotion" makes the
//! caller-statement of an at-init call inherit its callee's
//! transitive `lazy_reads` and `lazy_rebinds`. The classifier in
//! `facts.rs` now tracks a `lazy_depth` counter on each
//! `LazyBoundary` visitor, with `first_order_lazy_*` collected
//! when the visitor sits directly inside a function body (depth 1)
//! and the coarse `lazy_*` covering any depth ≥1. Promotion and
//! the direct `lazy_rebind` edge use the first-order subset so a
//! rebind inside a nested arrow doesn't manufacture a cross-module
//! constraint that nothing actually fires.
//!
//! ## Fixture
//!
//! ```js
//! let state = "initial";
//! function setupHandler() {
//!     globalThis.__updateState = () => { state = "updated"; };
//! }
//! setupHandler();
//! console.log(state);
//! ```
//!
//! Spec: `state` → `mod_state`, `setupHandler` → `mod_handler`.
//! Top-level `setupHandler()` + `console.log(state)` stay in
//! residual.
//!
//! ## Why the spec is realizable
//!
//! When the chunk loads:
//! 1. mod_state initializes `state = "initial"`.
//! 2. mod_handler initializes `setupHandler` (function declaration).
//! 3. Residual runs `setupHandler()` — which only assigns the
//!    arrow function `() => { state = "updated"; }` to
//!    `globalThis.__updateState`. The arrow body does NOT execute
//!    here; nothing rebinds `state` at this point.
//! 4. Residual runs `console.log(state)` → prints "initial".
//!
//! There is no actual cross-module rebind during initialization.
//! The materializer can emit this spec and Node will run it
//! correctly. The ESM linker has no ordering problem to solve.
//!
//! ## Implementation
//!
//! `BindingWriteCollector`, `LazyReadCollector`, and `CallCollector`
//! each track a `lazy_depth: u32` counter. `descend_lazy` increments
//! on entry to a function/arrow/method body and decrements on exit.
//! Each collector exposes a `first_order_*` subset whose entries
//! were recorded while `lazy_depth == 1`. Two graph sites consume
//! the subset:
//!
//! - `graph.rs::push_binding_edge` for `EdgeReason::lazy_rebind` —
//!   so a rebind inside a nested closure no longer creates the
//!   bidirectional G_atomic constraint at `atomic_units.rs:82-85`.
//! - `graph.rs::promote_at_init_calls` for the call graph and its
//!   per-owner rebind/read seeds — so an at-init call only inherits
//!   the synchronous part of the callee's body.
//!
//! Production observation: a real production chunk
//! `static/index-EXAMPLE` produced 22 cross-module
//! `eager_rebind` edges all sharing `statement_ordinal: 9705`
//! (the top-level `try { ... Age(...) ... }` bootstrap), creating a
//! 690-owner SCC spanning 11 modules. Every promoted rebind in
//! that SCC traced back to deferred-callback writes inside event
//! handlers nested in the bootstrap's call graph — none of them
//! fire at module init.

use debundle_e2e_support::*;

#[test]
fn at_init_call_does_not_promote_rebinds_from_nested_closures() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setupHandler() {
    globalThis.__updateState = () => { state = "updated"; };
}
setupHandler();
console.log(state);
export { state, setupHandler };
"#,
        vec![
            logical_module("mod_state", &[Member::new("state")]),
            logical_module("mod_handler", &[Member::new("setupHandler")]),
        ],
    ));
    assert_entry_output(&fixture, "initial\n");
}
