//! RED test: a spec ducktape ACCEPTS should run under Node.
//!
//! This is a generalization of #1681: that PR pinned the
//! residual-class TDZ shape. This pins a different shape that
//! the post-#1683 gate still accepts but TDZs at runtime —
//! confirmed against a real Tana bundle reproduction.
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
    // Matches the Tana chain: init_state.bootstrap → gR
    // (startWebClientBootstrap) → startBootProgressTracking →
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
    // This matches the Tana repro shape: init_state's bootstrap
    // anonymous statement calls `gR()` (startWebClientBootstrap,
    // a function decl in residual), whose body eventually
    // reads `T` (in `infra/logging/tana_logger`). At-init
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
