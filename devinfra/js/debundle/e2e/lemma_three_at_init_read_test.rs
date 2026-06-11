//! Pins docs/design.md "Lemma 3 (At-init read correctness)": in an
//! accepted spec, every at-init read in module `M` of a binding
//! owned by module `M'` sees that binding initialized at the moment
//! of the read — the `R`-edge `M → M'` forces the linker to evaluate
//! `M'` fully before any line of `M`'s body runs.
//!
//! Only the accept side lives here. The rejection side (an `R` cycle
//! no evaluation order can satisfy) is pinned by
//! `realizability_test::cyclic_spec_is_rejected_with_clear_error`,
//! and the runtime TDZ shape the gate exists to prevent by
//! `runtime_tdz_on_imported_class_test`.

use debundle_e2e_support::*;

#[test]
fn accepted_cross_module_at_init_read_observes_initialized_value() {
    // mod_a owns x1 + x2; x2's initializer at-init reads `y.id` from
    // mod_b (computed key — the read fires while mod_a's body
    // evaluates). R-edge mod_a → mod_b; the ESM linker evaluates
    // mod_b first, so the read observes y fully initialized: the
    // emitted bundle prints the same values the input chunk does. A
    // TDZ (or an undefined-keyed x2) would change the output.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
export { x1, y, x2 };
"#,
        vec![
            logical_module("mod_a", &[Member::new("x1"), Member::new("x2")]),
            logical_module("mod_b", &[Member::new("y")]),
        ],
    ));
    assert_entry_output(&fixture, "x1 k v\n");
}
