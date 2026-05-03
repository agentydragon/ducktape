//! Realizability of `define_logical_module` specs against
//! `materialize_logical_modules`. Each test feeds a fixture spec
//! into the materializer and asserts either:
//!
//! - **Acceptance + behaviour preservation** — the validator
//!   accepts the spec, the emit runs under Node, and the entry's
//!   stdout matches the input chunk's evaluation.
//! - **Rejection with cycle evidence** — the validator surfaces
//!   the unrealizable cycle at materialize time, naming the
//!   modules involved.
//!
//! The cases here exercise the full `I ∪ S` realizability
//! contract from <DESIGN.md>:
//!
//! - `R` cycles where both back-edges are at-init reads.
//! - `I` cycles where one back-edge is a lazy read (the linker
//!   import-graph bug Phase 5 closed).
//! - Cross-module init order under an acyclic `I` (ESM linker
//!   topo-sorts the modules and reads land initialised).
//! - `S` (side-effect ordering) precision under the
//!   expression-level purity classifier — pure const interleaves
//!   stay realisable; `S`-only cycles get caught.
//! - Per-declarator owner attribution across a comma-list
//!   `var/let/const` (the analyzer pre-splits comma-lists so the
//!   validator's view matches the emitter's per-declarator
//!   placement).

use debundle_e2e_support::*;
use serde_json::json;

// --- R cycles (both back-edges at-init) ----------------------------------

#[test]
fn cyclic_spec_is_rejected_with_clear_error() {
    // Source: A and C are independent decls; B reads A at-init,
    // D reads C at-init. The spec puts A,D in mod_x and B,C in
    // mod_y, creating cross-module reads in both directions.
    //
    //   mod_x = { A, D }   D = wrap(C) reads C ∈ mod_y
    //   mod_y = { B, C }   B = wrap(A) reads A ∈ mod_x
    //
    // Cycle: mod_x ↔ mod_y.
    let opts = FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            json!({
                "id": "logical__mod_x",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_x" },
                "members": [
                    { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                    { "id": "m_d", "name": "D", "selector": { "binding": { "name": "D" } } },
                ],
            }),
            json!({
                "id": "logical__mod_y",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_y" },
                "members": [
                    { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                    { "id": "m_c", "name": "C", "selector": { "binding": { "name": "C" } } },
                ],
            }),
        ],
    );
    expect_logical_modules_e2e_rejection(opts, &["cycle", "mod_x", "mod_y"]);
}

// --- I cycles via lazy back-edges ----------------------------------------

#[test]
fn rejects_cycle_through_lazy_back_edge() {
    // mod_b reads A from mod_a at-init (B's initializer);
    // mod_a's `readB` body reads B from mod_b lazily.
    //
    // The at-init read graph `R` is acyclic (one edge mod_b →
    // mod_a); the linker's import graph `I` is cyclic (the
    // function body's lazy read still emits an `import`
    // directive, closing the loop). Phase 5 made the validator
    // gate on `I ∪ S`, so this spec is now caught.
    //
    // Minimal synthetic shape of a real-world regression where
    // the old validator (built from `R` only) passed the spec
    // and the emitted bundle TDZ'd at runtime.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"const A = "a-value";
function readB() { return B; }
const B = A + "-postfix";
console.log(readB());
export { A, B, readB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        // Require every substring to appear: a generic "cycle"
        // mention that fails to name the implicated modules
        // wouldn't satisfy the test's contract.
        &["cycle", "mod_a", "mod_b"],
    );
}

// --- Acyclic specs and cross-module init order ----------------------------

#[test]
fn init_call_order_respects_cross_module_dependency() {
    // - mod_a owns `x1` (source item 0) and `x2` (source item 2;
    //   init reads `y.id`).
    // - mod_b owns `y` (source item 1).
    //
    // The pure-object initializers don't trigger `S` edges — the
    // only constraints are the `R`/`I` edge from `mod_a → mod_b`
    // (x2 reads y at-init) and the entry's reads of x1/x2/y. The
    // ESM linker evaluates `mod_b` before `mod_a` because of the
    // cross-module import.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
export { x1, y, x2 };
"#,
        vec![
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [
                    { "id": "m_x1", "name": "x1", "selector": { "binding": { "name": "x1" } } },
                    { "id": "m_x2", "name": "x2", "selector": { "binding": { "name": "x2" } } },
                ],
            }),
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [{
                    "id": "m_y",
                    "name": "y",
                    "selector": { "binding": { "name": "y" } },
                }],
            }),
        ],
    ));
    // Behaviour preservation under correct init-call ordering.
    // Without the init-order fix this would crash at module load
    // with `TypeError: Cannot read properties of undefined`.
    assert_entry_output(&fixture, "x1 k v\n");
}

// --- Side-effect ordering (`S`) ------------------------------------------

#[test]
fn pure_const_decls_across_modules_dont_create_s_cycles() {
    // mod_a's const = 1, mod_b's const = 1 — both pure literal
    // initializers. Source order interleaves them: mod_a, mod_b,
    // mod_a, mod_b. With a coarse `has_side_effect` (every
    // var-decl marked side-effecting) this would create S edges
    // in both directions and reject; the precise expression-
    // level purity classifier sees these as Pure and `S` is
    // empty for this fixture.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a1 = 1;
const b1 = 2;
const a2 = "x";
const b2 = "y";
const a3 = { k: a1 };
const b3 = [b1, b2];
console.log(a1, a2, a3.k, b1, b2, b3[0]);
export { a1, a2, a3, b1, b2, b3 };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::new("a1"), Member::new("a2"), Member::new("a3")],
            ),
            logical_module(
                "mod_b",
                &[Member::new("b1"), Member::new("b2"), Member::new("b3")],
            ),
        ],
    ));
    assert_entry_output(&fixture, "1 x 1 2 y 2\n");
}

#[test]
fn s_only_cycle_is_rejected() {
    // mod_a's body has a globalThis write at ord 0; mod_b's body
    // has another at ord 1; mod_a's body has another at ord 2.
    // Source order requires mod_a's first write, then mod_b's,
    // then mod_a's third — interleaving across modules. No
    // cross-module reads (no `R`/`I` edges), but `S` adds edges
    // in both directions, closing a cycle. The validator catches
    // it.
    expect_logical_modules_e2e_rejection_containing_all(
        FixtureOpts::new(
            r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
                logical_module("mod_b", &[Member::new("b1")]),
            ],
        ),
        // Cycle report must name both modules and surface the
        // side-effect evidence (not a binding name) so the spec
        // author understands the rejection comes from `S`, not
        // `R`/`I`.
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

// --- Per-declarator attribution across comma-list var-decls --------------

#[test]
fn comma_list_split_does_not_invent_cross_module_cycle() {
    // `a_in_x` is in mod_x; `B = X` is in mod_y reading mod_y's
    // own X (no edge). `Y = a_in_x` is in mod_y reading mod_x's
    // a_in_x (R-edge mod_y → mod_x). Without per-declarator
    // attribution, `stmt_owner` would pick A's owner (mod_x) for
    // the whole comma-list and attribute B's read of X to mod_x
    // → spurious R-edge mod_x → mod_y, plus the real
    // mod_y → mod_x edge → cycle. With pre-analysis split, B's
    // row owns just B and reads X within its own module (no
    // cross-module edge), so only mod_y → mod_x remains.
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
    // A=1, B=42, Y="x" → log "1 42 42 x". Emit preserves it
    // (mod_x evaluates first; mod_y reads a_in_x already
    // initialised).
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
    // Without per-declarator attribution, the read of `a_in_x`
    // from `const A = 1, B = a_in_x;` is attributed to mod_x
    // (A's owner). owner(a_in_x) = mod_x → same module, no edge.
    // The mod_y → mod_x edge is missed. Validator accepts a
    // spec that TDZs at runtime.
    //
    // With pre-analysis split, B's row owns just B and is in
    // mod_y, so the read of `a_in_x` correctly attributes to
    // mod_y. Both edges present, validator rejects.
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
        &["cycle", "mod_x", "mod_y"],
    );
}
