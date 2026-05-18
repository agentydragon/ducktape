//! RED test: at-init call promotion (DESIGN.md "At-init call
//! promotion") must not propagate rebinds that lexically appear
//! inside an async function body **after** the first `await`. Those
//! rebinds run in a microtask after the at-init caller has finished,
//! not synchronously at module-initialization time. Promoting them
//! manufactures cross-module `eager_rebind` edges that don't reflect
//! runtime ordering, leading to `atomic-factor-unit conflict`
//! materializer rejections of specs that ESM can actually realize.
//!
//! ## Fixture
//!
//! ```js
//! let state = "initial";
//! async function asyncSetup() {
//!     await Promise.resolve();
//!     state = "updated";
//! }
//! asyncSetup();
//! console.log(state);
//! ```
//!
//! Spec: `state` → `mod_state`, `asyncSetup` → `mod_setup`. Top-level
//! `asyncSetup()` + `console.log(state)` stay in residual.
//!
//! ## Why the spec is realizable
//!
//! When the chunk loads under ESM:
//! 1. mod_state initializes `state = "initial"`.
//! 2. mod_setup defines `asyncSetup`.
//! 3. Residual runs `asyncSetup()` — this synchronously enters the
//!    async function's body, immediately reaches `await
//!    Promise.resolve()`, and suspends. The body resumes from a
//!    microtask after the current synchronous frame finishes.
//! 4. Residual runs `console.log(state)` → prints `"initial"`.
//!    The await hasn't resumed yet; `state = "updated"` has not run.
//! 5. After the synchronous frame: the microtask queue runs the
//!    continuation. `state` becomes `"updated"`. The module is
//!    already done with its synchronous initialization phase.
//!
//! No cross-module rebind happens during initialization. The ESM
//! linker has no ordering problem to solve.
//!
//! ## What ducktape does today
//!
//! `facts.rs::BindingWriteCollector` (and `LazyReadCollector`,
//! `CallCollector`) track a `lazy_depth: u32` counter that increments
//! on entry to a function/arrow/method/getter/setter body and
//! decrements on exit. PR #1646 introduced `first_order_lazy_rebinds`
//! — entries recorded at `lazy_depth == 1` — and routed at-init call
//! promotion through this subset so a rebind inside a nested closure
//! doesn't manufacture a cross-module constraint that never fires.
//!
//! However, `descend_lazy` does **not** bump `lazy_depth` past an
//! `await` expression. Code in an async function body that sits
//! lexically inside the immediate body — but **after** the first
//! `await` — is therefore still classified as `lazy_depth == 1` and
//! propagated through `first_order_lazy_rebinds`. That's incorrect:
//! the JS engine suspends at the `await`, returns control to the
//! caller, and only resumes the post-`await` body in a later
//! microtask. By the time `state = "updated"` runs, the at-init
//! caller has already moved on. The rebind is **not** an at-init
//! event.
//!
//! `graph.rs::promote_at_init_calls` walks the call graph from the
//! at-init caller (residual's `asyncSetup()`), unions in
//! `asyncSetup.first_order_lazy_rebinds = {"state"}`, and emits an
//! `eager_rebind` edge from the residual call-statement to `state`'s
//! owner. Because `state` lives in `mod_state` and the call-statement
//! lives in residual, the edge is cross-module.
//!
//! The factorize-assembly machinery sees the cross-destination
//! `eager_rebind` as a clause-2 violation (cross-destination
//! rebinding writes are never importable across ESM module
//! boundaries) and merges residual + mod_state + mod_setup into one
//! inseparable atomic factor unit. Materializer rejects.
//!
//! This test asserts the correct runtime behavior (entry prints
//! `"initial"`). It currently fails at `run_fixture` because the
//! materializer rejects the spec before any JS is emitted.
//!
//! ## Suggested fix family
//!
//! Add an "await past" boundary to `LazyBoundary` collectors. The
//! cleanest formulation: while visiting an async function/arrow
//! body, treat the first `AwaitExpr` (and every statement that
//! follows it within the same async body) as `lazy_depth + 1`.
//! Implementation hook: in `visit_async_function`/`visit_arrow_expr`
//! (when `is_async`), walk the body's statements; for each
//! statement past the first containing `AwaitExpr`, descend with an
//! incremented `lazy_depth`. Or simpler: introduce a separate
//! `past_first_await: bool` flag on the visitor that flips true
//! when a top-level `AwaitExpr` is encountered, and is checked
//! alongside `lazy_depth == 1` when populating `first_order_*`.
//!
//! Both sites that consume `first_order_*` already use the subset
//! correctly (`graph.rs::push_binding_edge` for direct
//! `EdgeReason::lazy_rebind`, `graph.rs::promote_at_init_calls` for
//! the call graph and per-owner rebind/read seeds), so the fix is
//! localized to the collector.
//!
//! ## Production observation
//!
//! gaffer-private's Tana chunk `static/index-DI2GynTv` post-#1634
//! materializer rejects with a 690-owner atomic-factor-unit conflict
//! spanning residual + 9 named modules. Tracing the 11 cross-module
//! `eager_rebind` edges from the bootstrap statement (owner:9705,
//! `try { ... gR(...); ... }` calling `gR`, which calls async
//! `GGt(n)`) shows that ~5 of them are pre-await assignments (real
//! at-init rebinds, ducktape correct) and ~5 are post-await
//! assignments inside `GGt`'s async body — specifically `i2t()`
//! (after `await FY(e, ...)` at line 203619), `bD(!0)`, and the
//! `Uge`/`OGt` chain that assigns offline-mode-state bindings. Those
//! 5 over-conservative promoted edges anchor 5 of the 9 named
//! modules into the atomic factor unit; if they were correctly
//! recognized as past-await, the conflict would shrink to ~4
//! modules.

use debundle_e2e_support::*;

#[test]
fn at_init_call_does_not_promote_rebinds_after_await_in_async_body() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
async function asyncSetup() {
    await Promise.resolve();
    state = "updated";
}
asyncSetup();
console.log(state);
export { state, asyncSetup };
"#,
        vec![
            logical_module("mod_state", &[Member::new("state")]),
            logical_module("mod_setup", &[Member::new("asyncSetup")]),
        ],
    ));
    assert_entry_output(&fixture, "initial\n");
}
