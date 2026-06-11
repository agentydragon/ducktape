//! RED test: a spec ducktape ACCEPTS should run under Node.
//!
//! This is a generalization of #1681: that PR pinned the
//! residual-class TDZ shape. This pins a different shape that
//! the post-#1683 gate still accepts but TDZs at runtime —
//! confirmed against a real an upstream bundle reproduction.
//!
//! ## Invariant being pinned
//!
//! The debundler's gate is supposed to be a *runtime
//! correctness oracle*: if the pipeline emits, the emitted JS
//! must successfully evaluate under ESM (no TDZ, no
//! ReferenceError) on Node and the major browsers.
//!
//! This invariant should be documented in
//! `devinfra/js/debundle/AGENTS.md` so future ducktape changes
//! can be evaluated against it: any spec that passes the gate
//! must execute the emitted entry without throwing.
//!
//! ## Fixture shape
//!
//! Two peeled modules in a chain:
//!
//! - `mod_logger`: declares `let T = …` (a TDZ-locked binding).
//! - `mod_init`: anonymous side-effect statement at top level
//!   calls a function whose body reads `T` lazily. With at-init
//!   call promotion, that lazy read fires *at module-init time*
//!   inside mod_init's body.
//!
//! Both modules are imported by entry. The constraining edge
//! `mod_init → mod_logger (eager_use, promoted)` is present in
//! the owner graph. Neither rule 1 nor rule 2 of the
//! realizability gate fires:
//!
//! - Rule 1 needs a constraining-edge SCC. There's no
//!   constraining edge from `mod_logger → mod_init`, so the
//!   constraining subgraph is acyclic.
//! - Rule 2 needs an I-cycle through residual with a
//!   constraining edge whose target IS residual. Neither
//!   endpoint is residual.
//!
//! So the gate ACCEPTS. But the materializer's
//! `source_import_position` may put `mod_init` before
//! `mod_logger` in entry's import list. Under ESM's post-order
//! DFS, `mod_init` evaluates first; its body reads `T` from
//! `mod_logger`; `mod_logger`'s body hasn't run; `T` is in
//! TDZ; `ReferenceError`.
//!
//! ## Expected outcomes
//!
//! - **Today (RED)**: gate accepts; Node throws
//!   `ReferenceError: Cannot access 'T' before initialization`
//!   when running the emitted entry. `assert_entry_output`
//!   panics on the non-zero exit.
//! - **After the fix**: either
//!     - the gate rejects this shape (recognizing
//!       cross-module-peeled at-init reads as needing
//!       order enforcement that source_import_position can't
//!       provide for this pattern), or
//!     - `source_import_position` correctly puts `mod_logger`
//!       before `mod_init` in entry's import list, so ESM
//!       DFS visits `mod_logger` first.

use debundle_e2e_support::*;

#[test]
fn cross_peeled_at_init_read_executes_under_node() {
    // mod_logger owns T. mod_init owns readT and `init` (where
    // `init = readT()` evaluates at-init, reading T cross-module).
    // Entry imports init from mod_init at-init → mod_init must
    // evaluate. mod_init's body runs readT() which reads T from
    // mod_logger.
    let fixture = run_fixture(FixtureOpts::new(
        r#"let T = "ready";
function readT() { return T; }
const init = readT();
console.log(init);
export { T, readT, init };
"#,
        vec![
            logical_module("mod_logger", &[Member::new("T")]),
            logical_module("mod_init", &[Member::new("readT"), Member::new("init")]),
        ],
    ));
    assert_entry_output(&fixture, "ready\n");
}

#[test]
fn at_init_when_reader_source_precedes_declarer_executes_under_node() {
    // Like the basic case, but reader's source ord (mod_init's
    // owners) come BEFORE declarer's (mod_logger's T) in source
    // order. If `source_import_position` blindly preserves
    // source-order it would put mod_init first → ESM DFS
    // evaluates mod_init's body before mod_logger's → TDZ.
    //
    // The eager_use edge mod_init → mod_logger MUST override
    // source order.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function readT() { return T; }
const init = readT();
let T = "ready";
console.log(init);
export { T, readT, init };
"#,
        vec![
            logical_module("mod_init", &[Member::new("readT"), Member::new("init")]),
            logical_module("mod_logger", &[Member::new("T")]),
        ],
    ));
    assert_entry_output(&fixture, "ready\n");
}

#[test]
fn at_init_through_deep_residual_chain_executes_under_node() {
    // Three-deep call chain: mod_init at-init → bootstrap (residual)
    // → startTracking (residual) → reads T (in mod_logger).
    //
    // Matches the the upstream chain: an entry-side bootstrap statement → gR
    // (an outer helper) → an inner helper →
    // reads T.
    let fixture = run_fixture(FixtureOpts::new(
        r#"let T = "ready";
function startTracking() { return T; }
function bootstrap() { return startTracking(); }
const init = bootstrap();
console.log(init);
export { T, bootstrap, startTracking, init };
"#,
        vec![
            logical_module("mod_logger", &[Member::new("T")]),
            logical_module("mod_init", &[Member::new("init")]),
        ],
    ));
    assert_entry_output(&fixture, "ready\n");
}

#[test]
fn at_init_through_residual_function_executes_under_node() {
    // Like above, but the at-init call chain crosses residual:
    // mod_init at-init calls a function decl that lives in
    // residual, whose body reads T (in mod_logger).
    //
    // This matches the the upstream repro shape: an entry-side module's bootstrap
    // anonymous statement calls `gR()` (an outer helper,
    // a function decl in residual), whose body eventually
    // reads `T` (in `logger_module`). At-init
    // promotion through the residual function decl is what we
    // need to surface the eager_use edge `mod_init → mod_logger`
    // for source_import_position to order them correctly.
    let fixture = run_fixture(FixtureOpts::new(
        r#"let T = "ready";
function bootstrap() { return T; }
const init = bootstrap();
console.log(init);
export { T, bootstrap, init };
"#,
        vec![
            logical_module("mod_logger", &[Member::new("T")]),
            // mod_init owns just the `init` const which calls
            // bootstrap (residual function decl).
            logical_module("mod_init", &[Member::new("init")]),
        ],
    ));
    assert_entry_output(&fixture, "ready\n");
}

#[test]
fn early_entry_importer_does_not_pull_scc_in_wrong_order() {
    // Mediator-heavy I-SCC `{mod_logger, mod_middle, mod_init}`
    // with one constraining edge `mod_init → mod_logger` (via
    // at-init promotion of `readT()` → `T`).
    //
    // Why Lemma 2 rescues this even though `mod_early` and
    // `mod_middle` both reach into the SCC: residual has
    // I-edges to every SCC member (the export list re-exports
    // T/init/middleHelper, and console.log(init) is at-init).
    // The materializer's `source_import_position` thus puts the
    // SCC's dependent (mod_init) first in entry's import list.
    // ESM DFS visits the SCC from residual FIRST, with
    // mod_init's eager import unwinding through mod_logger
    // before mod_middle's lazy back-edge reaches mod_init on
    // the link stack (cycle no-op). By the time mod_early or
    // any other mediator's body runs, the SCC has already
    // finished evaluating — their imports of SCC members are
    // no-ops.
    //
    // Empirically the emitted JS does run cleanly under Node;
    // the pre-145984d83 docstring's claim that mod_init's body
    // TDZs was a mis-attribution (mod_logger evaluates before
    // mod_init under Lemma 2's import order). The precise gate
    // accepts.
    let mut opts = FixtureOpts::new(
        r#"const T = "ready";
function readT() { return T; }
function disableDevMode() { return T; }
const init = readT();
function middleHelper() { return init + "-mid"; }
function loggerReader() { return middleHelper(); }
console.log(init);
export { T, readT, init, disableDevMode, middleHelper, loggerReader };
"#,
        vec![
            // mod_early imports `T` (drags mod_logger into DFS first).
            logical_module("mod_early", &[Member::new("disableDevMode")]),
            // mod_logger declares T. References middleHelper (lazy).
            logical_module(
                "mod_logger",
                &[Member::new("T"), Member::new("loggerReader")],
            ),
            // mod_middle owns middleHelper. References init (lazy).
            logical_module("mod_middle", &[Member::new("middleHelper")]),
            // mod_init owns init. At-init reads T via residual readT.
            logical_module("mod_init", &[Member::new("init")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "ready\n");
}

#[test]
fn peeled_method_reassigning_top_level_let_executes_under_node() {
    // ★ RED test: minimal reproduction of the
    // `Assignment to constant variable` runtime crash hit
    // when peeling the upstream `an upstream class` class.
    //
    // the upstream shape (paraphrased):
    //
    //   let isSearchBeingRefreshedForNode = (n) => false;
    //   class an upstream class {
    //     static setIsSearchBeingRefreshedForNodeHandler(e) {
    //       isSearchBeingRefreshedForNode = e;           // ← write
    //     }
    //   }
    //
    // Before peeling `an upstream class`, the write lives in
    // the same module as the `let` declaration: legal. After
    // peeling `an upstream class` into its own module, the
    // emitted file looks like:
    //
    //   import { isSearchBeingRefreshedForNode } from "../entry.js";
    //   class an upstream class {
    //     static setIsSearchBeingRefreshedForNodeHandler(e) {
    //       isSearchBeingRefreshedForNode = e;
    //     }
    //   }
    //
    // Imported bindings are immutable per ESM — the method
    // throws `TypeError: Assignment to constant variable` the
    // first time it's called.
    //
    // Ducktape's realizability gate today accepts this peel
    // because the write is inside a method body (lazy
    // position). The fix must either reject any peel whose
    // emitted module assigns to a binding declared in another
    // module, or rewrite the write through a setter exported
    // from the declaring module.
    let mut opts = FixtureOpts::new(
        r#"let counter = 0;
class Counter {
  bump(b) { counter = b; }
  read() { return counter; }
}
const c = new Counter();
c.bump(1);
console.log(c.read());
export { counter, Counter, c };
"#,
        vec![
            // Peel the class + its instance into a separate
            // module. `counter` (the let) stays in residual.
            logical_module(
                "mod_counter_class",
                &[Member::new("Counter"), Member::new("c")],
            ),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "1\n");
}
