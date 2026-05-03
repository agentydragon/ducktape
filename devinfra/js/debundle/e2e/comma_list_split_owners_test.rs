//! Per-declarator owner attribution for comma-list var-decls.
//!
//! Pre-fix, `build_module_dep_graph` computed `stmt_owner` once
//! per `StatementFacts` by picking the first declared name's
//! owner. The emitter's `split_var_decl` splits comma-lists per
//! declarator at lower-time, so a chunk like
//! `const A = 1, B = X;` with `A → mod_x` and `B → mod_y` would
//! attribute `B`'s reads to `mod_x`'s home in the validator,
//! even though `B` actually emits into `mod_y`. The two
//! observable failures: invented cycles (validator rejects a
//! realisable spec) and missed cycles (validator accepts an
//! unrealisable spec). The pre-analysis split in
//! `analyze_chunk_facts` aligns the validator's view with the
//! emitter's per-declarator placement.

use debundle_e2e_support::*;

#[test]
fn comma_list_split_does_not_invent_cross_module_cycle() {
    // `a_in_x` is in mod_x; `B = X` is in mod_y reading mod_y's
    // own X (no edge). `Y = a_in_x` is in mod_y reading mod_x's
    // a_in_x (R-edge mod_y → mod_x). Pre-fix, `stmt_owner`
    // picked A's owner (mod_x) for the whole comma-list and
    // attributed B's read of X to mod_x → spurious R-edge
    // mod_x → mod_y, plus the real mod_y → mod_x edge → cycle.
    // Validator rejected. Post-fix, B's row owns just B and
    // reads X within its own module (no cross-module edge), so
    // only mod_y → mod_x remains. Spec realises and runs.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a_in_x = "x";
const X = 42;
const A = 1, B = X;
const Y = a_in_x;
console.log(A, B, X, Y);
export { A, B, X, Y, a_in_x };
"#,
        vec![
            logical_module("mod_x", &[Member::new("A"), Member::new("a_in_x")]),
            logical_module(
                "mod_y",
                &[Member::new("B"), Member::new("X"), Member::new("Y")],
            ),
        ],
    ));
    // Original chunk evaluates left-to-right: a_in_x="x", X=42,
    // A=1, B=42, Y="x" → log "1 42 42 x". Post-fix emit
    // preserves it (mod_x evaluates first; mod_y reads a_in_x
    // already initialised).
    assert_entry_output(&fixture, "1 42 42 x\n");
}

#[test]
fn comma_list_split_surfaces_missed_cross_module_cycle() {
    // Mirror of the previous fixture: now `B`'s read crosses
    // modules instead of staying within mod_y. `a_in_x` is in
    // mod_x; `B = a_in_x` is in mod_y reading mod_x → R-edge
    // mod_y → mod_x. `fromB = B` is in mod_x reading mod_y →
    // R-edge mod_x → mod_y. Cycle.
    //
    // Pre-fix: `stmt_owner` picks A's owner (mod_x) for the
    // whole `const A = 1, B = a_in_x;` line. The read of
    // `a_in_x` is attributed to mod_x → owner(a_in_x) = mod_x.
    // Same module, no edge. The mod_y → mod_x edge is missed.
    // Only `fromB` reads B → mod_x → mod_y. No cycle. Validator
    // accepts a spec that TDZs at runtime.
    //
    // Post-fix: B's row owns just B and is in mod_y, so reads
    // of `a_in_x` correctly attribute to mod_y. Both edges
    // present, validator rejects.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"const a_in_x = "x";
const A = 1, B = a_in_x;
const fromB = B;
console.log(A, B, fromB);
export { A, B, fromB, a_in_x };
"#,
            vec![
                logical_module(
                    "mod_x",
                    &[
                        Member::new("A"),
                        Member::new("a_in_x"),
                        Member::new("fromB"),
                    ],
                ),
                logical_module("mod_y", &[Member::new("B")]),
            ],
        ),
        // The cycle report must name both modules so the spec
        // author knows which split to fix.
        &["cycle", "mod_x", "mod_y"],
    );
}
