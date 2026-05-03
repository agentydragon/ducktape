//! Side-effect ordering edges (`S`) in `I ∪ S`.
//!
//! - **Positive**: many cross-module pure const declarations
//!   interleaved in source order. With the precise purity
//!   classifier in place, none of these contribute to `S`, so
//!   `I ∪ S` stays acyclic and the materializer accepts the spec.
//!   Without the classifier, every var-decl was marked
//!   side-effecting and every cross-module pair would create an
//!   `S` edge — densely cyclic.
//!
//! - **Negative**: two side-effecting statements whose modules
//!   close an `S`-only cycle (no `R`/`I` edge required). The
//!   validator rejects with `<side-effect>` evidence in both
//!   directions.

use debundle_e2e_support::*;

#[test]
fn pure_const_decls_across_modules_dont_create_s_cycles() {
    // mod_a's const = 1, mod_b's const = 1 — both pure literal
    // initializers. Source order interleaves them: mod_a, mod_b,
    // mod_a, mod_b. Pre-classifier this would create S edges in
    // both directions and reject; post-classifier none of them
    // are side-effecting and the spec is realisable.
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
    // Behaviour preservation: entry runs and prints expected
    // values. Demonstrates that the materializer accepted the
    // spec (no validator rejection) and the emitted bundle is
    // observationally equivalent.
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
