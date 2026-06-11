//! Regression test: at-init call promotion (docs/design.md "At-init call
//! promotion") must not propagate rebinds or calls that lexically
//! appear inside an async function body **after** the first `await`.
//! Those statements run in a microtask after the at-init caller has
//! finished, not synchronously at module-initialization time.
//! Promoting them manufactures cross-module `eager_rebind` edges that
//! don't reflect runtime ordering, leading to
//! `atomic-factor-unit conflict` materializer rejections of specs
//! that ESM can actually realize.
//!
//! ## Fixture
//!
//! ```js
//! let state = "initial";
//! function setState(v) { state = v; }
//! async function asyncSetup() {
//!     await Promise.resolve();
//!     setState("updated");
//! }
//! asyncSetup();
//! console.log(state);
//! ```
//!
//! Spec: `state` + `setState` → `mod_state`, `asyncSetup` →
//! `mod_setup`. Top-level `asyncSetup()` + `console.log(state)`
//! stay in residual.
//!
//! `setState` is co-located with `state` in `mod_state` because ESM
//! imports are read-only — a direct `state = "updated"` inside
//! `mod_setup` would crash at microtask time regardless of whether
//! the materializer accepts the spec. The production case the test
//! is motivated by (post-await calls like `i2t()`) follows the same
//! pattern: the async body invokes a function whose body internally
//! rebinds local state.
//!
//! ## Why the spec is realizable
//!
//! When the chunk loads under ESM:
//! 1. mod_state initializes `state = "initial"` and defines
//!    `setState`.
//! 2. mod_setup defines `asyncSetup`.
//! 3. Residual runs `asyncSetup()` — this synchronously enters the
//!    async function's body, immediately reaches `await
//!    Promise.resolve()`, and suspends. The body resumes from a
//!    microtask after the current synchronous frame finishes.
//! 4. Residual runs `console.log(state)` → prints `"initial"`.
//!    The await hasn't resumed yet; `setState("updated")` has not
//!    run.
//! 5. After the synchronous frame: the microtask queue runs the
//!    continuation. `setState("updated")` fires inside `mod_state`
//!    and updates `state` locally. No cross-module write.
//!
//! No cross-module rebind happens during initialization. The ESM
//! linker has no ordering problem to solve.
//!
//! ## Implementation
//!
//! `facts.rs::BindingWriteCollector` / `LazyReadCollector` /
//! `CallCollector` each track a `lazy_depth: u32` counter that
//! increments on entry to a function/arrow/method/getter/setter body
//! and decrements on exit, plus a per-body `past_await: bool` flag
//! that flips `true` when an `AwaitExpr` is encountered. The flag is
//! saved+reset at each `descend_lazy` boundary so a nested function
//! body starts pre-await regardless of the enclosing body's state.
//! `visit_await_expr` visits the awaited operand's children first
//! (those subexpressions run before the engine suspends) and then
//! flips the flag.
//!
//! The three `first_order_*` populate paths gate on
//! `lazy_depth == 1 && !past_await`, so post-await rebinds /
//! reads / calls land in the coarse `lazy_*` set (still considered
//! lazy from the chunk's top-level POV) but are excluded from the
//! first-order subset that `graph.rs::promote_at_init_calls` and
//! `graph.rs::push_binding_edge` (for the direct
//! `EdgeReason::lazy_rebind`) consume.
//!
//! ## Production observation
//!
//! a real production chunk `static/index-EXAMPLE` post-#1634
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
    // The fixture uses a setter (`setState`) co-located with `state`
    // in `mod_state` so the post-await rebind fires inside the
    // binding's own module — ESM imports are read-only, so a direct
    // `state = "updated"` in `mod_setup` would crash at microtask
    // time regardless of whether the materializer accepts the spec.
    // This mirrors the production shape called out in the test
    // doc-comment (post-await calls like `i2t()` whose body
    // internally rebinds state in their own module), not a synthetic
    // direct rebind across modules. The post-await classifier under
    // test fires the same way for both rebinds and calls (both go
    // through `first_order_*` gates).
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setState(v) { state = v; }
async function asyncSetup() {
    await Promise.resolve();
    setState("updated");
}
asyncSetup();
console.log(state);
export { state, setState, asyncSetup };
"#,
        vec![
            logical_module(
                "mod_state",
                &[Member::new("state"), Member::new("setState")],
            ),
            logical_module("mod_setup", &[Member::new("asyncSetup")]),
        ],
    ));
    assert_entry_output(&fixture, "initial\n");
}
