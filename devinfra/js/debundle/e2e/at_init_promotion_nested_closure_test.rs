//! RED test: at-init call promotion is over-conservative for
//! rebinds that lexically appear inside a nested closure but never
//! fire at module-initialization time.
//!
//! Background: DESIGN.md "At-init call promotion" makes the
//! caller-statement of an at-init call inherit its callee's
//! transitive `lazy_reads` and `lazy_rebinds`. The classifier behind
//! `lazy_rebinds` (`BindingWriteCollector` + `LazyBoundary` in
//! `facts.rs:691-790`) sets `in_lazy = true` at the **first**
//! function boundary it crosses and never resets it. Rebinds inside
//! a function body that is itself nested inside an outer function
//! body (e.g. an arrow function returned/stashed by the outer
//! function) are recorded as `lazy_rebinds` of the OUTER function.
//!
//! That's correct for clause 3 ("any rebind inside any function
//! body is lazy from the chunk's top-level perspective"), but it's
//! too coarse for at-init promotion. Promotion's intended semantics
//! is "the at-init caller statement runs the part of the callee's
//! body that executes synchronously when the call returns". A
//! rebind inside a nested arrow function returned by the callee
//! does NOT execute synchronously — it only fires when something
//! later invokes the returned closure. Treating it as an at-init
//! rebind manufactures a cross-module `eager_rebind` edge that
//! doesn't exist at runtime, which then surfaces as an
//! `atomic-factor-unit conflict` during materialize.
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
//! ## What ducktape does today
//!
//! `facts.rs::analyze_chunk` builds `setupHandler.lazy_rebinds =
//! {"state"}` because the arrow body's `state = "updated"` write is
//! lexically inside setupHandler's body (and `BindingWriteCollector`
//! flips `in_lazy` at the outer function boundary; it never
//! distinguishes between setupHandler's first-order body and a
//! nested arrow inside it).
//!
//! At-init call promotion in `graph.rs::promote_at_init_calls` then
//! resolves residual's `setupHandler()` at-init call, walks
//! `setupHandler.reachable_lazy_rebinds = {"state"}`, and emits an
//! `eager_rebind` owner edge from the residual call-statement to
//! `state`'s owner. Because `state` lives in `mod_state` and the
//! call-statement lives in residual, the edge is cross-module.
//!
//! The factorize-assembly machinery (called from
//! `materialize_logical_modules`) sees this rebind as a clause-2
//! violation (cross-destination rebinding writes are never
//! importable) and groups residual + mod_state + mod_handler into
//! one inseparable atomic unit, rejecting the spec with an
//! `atomic-factor-unit conflict` error.
//!
//! This test asserts the correct behavior (entry prints
//! "initial"). It currently fails at `run_fixture` because the
//! materializer rejects the spec before any JS is emitted.
//!
//! ## Suggested fix family
//!
//! `BindingWriteCollector` and `LazyReadCollector` need a finer
//! state than the current binary `in_lazy`. A clean
//! implementation: distinguish "in callee's executable body" from
//! "in a nested function inside callee's executable body". Two
//! options:
//!
//! 1. Add a third state (`InFirstOrderBody` vs `InNestedClosure`)
//!    to `LazyBoundary`. Collect rebinds in the first-order set
//!    only; promotion uses the first-order set.
//! 2. Split into two separate collectors with different boundary
//!    rules — one for clause 3 (any function body counts as lazy),
//!    one for promotion (only the immediate body counts; nested
//!    closures don't).
//!
//! Production observation: gaffer-private's Tana chunk
//! `static/index-DI2GynTv` produces 22 cross-module
//! `eager_rebind` edges all sharing `statement_ordinal: 9705`
//! (the top-level `try { ... Age(...) ... }` bootstrap), creating a
//! 690-owner SCC spanning 11 modules. Every promoted rebind in
//! that SCC traces back to deferred-callback writes inside event
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
