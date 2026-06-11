//! Regression test (originally RED): a chunk the realizability
//! gate ACCEPTS must actually execute under Node when
//! materialized. The gate used to accept a partition whose
//! emitted ESM graph TDZ'd at runtime — its proof was unsound
//! for this shape.
//!
//! ## Shape
//!
//! Two cells. Entry declares
//!
//! ```js
//! class Backend { constructor() { this.tag = "B"; } }
//! ```
//!
//! and a co-located logger triple
//!
//! ```js
//! let currentLogger;
//! function setLogger(impl) { currentLogger = impl; }
//! setLogger(new Backend());           // at-init read of `Backend`
//! console.log(currentLogger.tag);
//! ```
//!
//! The spec peels the logger triple (`{currentLogger, setLogger}`
//! plus the anonymous `setLogger(new Backend());` statement)
//! into a logical module. `Backend` stays in residual / entry.
//!
//! Emitted module shape:
//!
//! - `mod_logger.js` imports `Backend` from entry and runs
//!   `setLogger(new Backend())` at top level (at-init read).
//! - `entry.js` imports `currentLogger` / `setLogger` (or just
//!   side-effects) from `mod_logger.js`, declares `class Backend`,
//!   runs `console.log(currentLogger.tag)`.
//!
//! Cycle: `entry ↔ mod_logger`. ESM evaluates by post-order DFS
//! from the entry. The standard outcome is:
//!
//! 1. DFS visits entry; entry imports mod_logger → visit mod_logger.
//! 2. mod_logger imports `Backend` from entry; entry is already
//!    on the DFS stack (cycle) → don't recurse.
//! 3. Done with mod_logger's deps → evaluate mod_logger's body.
//! 4. Body runs `setLogger(new Backend())`. `Backend` resolves
//!    to entry's `Backend` binding, but entry's body has NOT yet
//!    evaluated past the `class Backend` declaration.
//! 5. `class Backend` is a class declaration — Temporal Dead Zone
//!    until its line runs. The body crashes with
//!    `ReferenceError: Cannot access 'Backend' before initialization`.
//!
//! ## Why ducktape accepted
//!
//! The cycle `entry ↔ mod_logger` carries:
//!
//! - `mod_logger → entry`: at-init `EagerUse(Backend)` from the
//!   anonymous `setLogger(new Backend())` statement.
//! - `entry → mod_logger`: a `LazyUse` of `currentLogger`
//!   (the `console.log` reads it; or the residual statement is
//!   peeled and replaced by a re-export).
//!
//! The relaxed realizability primitive (docs/design.md "Realizability
//! primitive", clause 3) accepts cycles whose constraining-edge
//! subgraph (drops `LazyUse`) has no multi-module SCC. Here the
//! constraining edges from `mod_logger → entry` form a singleton
//! SCC, so the primitive's verdict is "realizable" — based on
//! Lemma 2's claim that the materializer's `source_import_position`
//! puts the cycle dependent (here `mod_logger`) FIRST in entry's
//! source so the linker's DFS lands entry first.
//!
//! That claim was the bug. The cycle dependent (`mod_logger`)
//! being put FIRST in entry's source means ESM's DFS visits it
//! FIRST, not entry first — so post-order evaluation runs
//! mod_logger's body before entry's body. Backend is TDZ at
//! mod_logger's evaluation. Lemma 2's direction is inverted for
//! the `(at-init forward, lazy back)` shape hit here.
//!
//! ## Pinned behavior
//!
//! The realizability gate recognizes the asymmetric-cycle shape
//! `(at-init forward, lazy back)` and rejects the spec. The
//! materializer can't emit working JS for this partition — ESM
//! hoists all imports above any statement, so re-sequencing
//! `source_import_position` is never going to put entry's
//! `class Backend` declaration before `mod_logger`'s
//! `new Backend()`. The only sound outcome is to reject loudly
//! so the spec author sees the conflict.

use debundle_e2e_support::*;

const FIXTURE_SOURCE: &str = r#"class Backend { constructor() { this.tag = "B"; } }
let currentLogger;
function setLogger(impl) {
    currentLogger = impl;
    globalThis.__tag = impl.tag;
}
setLogger(new Backend());
globalThis.__final = "done";
console.log(globalThis.__tag, globalThis.__final);
export { Backend, currentLogger, setLogger };
"#;

fn opts_for_fixture() -> FixtureOpts<'static> {
    let mut opts = FixtureOpts::new(
        FIXTURE_SOURCE,
        vec![logical_module_with_anon(
            "mod_logger",
            &[Member::new("currentLogger"), Member::new("setLogger")],
            &["setLogger(new Backend());"],
        )],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.dataflow_aware_s_chain = true;
    opts
}

#[test]
fn at_init_use_of_residual_class_is_rejected_to_avoid_tdz() {
    // Cycle: mod_logger → entry (EagerUse Backend, constraining)
    // + entry → mod_logger (LazyUse currentLogger / setLogger via
    // entry's re-export, captured because `visit_named_export`
    // records the orig idents as lazy reads). The tightened
    // realizability rule catches the asymmetric I-cycle: a
    // multi-module SCC in I that contains a constraining edge is
    // unrealizable. Pipeline rejects with a cycle report instead
    // of emitting JS that would TDZ at runtime.
    expect_rejection(
        opts_for_fixture(),
        &["unrealizable", "cycle", "tdz", "cannot access"],
    );
}
