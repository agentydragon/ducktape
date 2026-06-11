//! Pins docs/design.md "Lemma 4 (Lazy-read correctness)": lazy
//! cross-module reads (reads inside function bodies) fire only after
//! their target binding is initialized, under ANY evaluation order
//! the linker picks. A mutual lazy cycle is therefore accepted, and
//! post-init calls in both directions see initialized bindings —
//! whichever module the linker's DFS happens to enter first.

use debundle_e2e_support::*;

#[test]
fn mutual_lazy_cycle_post_init_calls_in_both_directions_see_initialized_bindings() {
    // mod_a owns A and readB; readB() lazily reads B (in mod_b).
    // mod_b owns B and readA; readA() lazily reads A (in mod_a).
    // No cross-module read fires at-init: when the linker evaluates
    // either module, no top-level statement reaches into the other
    // one. The function bodies only run *after* both modules finish
    // evaluating — no TDZ.
    //
    // The SCC in the imports graph `I` is `{mod_a, mod_b}`, but it
    // carries no at-init (`R`) and no side-effect (`S`) cross-module
    // edges — only `L` edges. The realizability gate must accept
    // this spec; rejecting would over-restrict the realizable subset
    // of `I ∪ S` cycles. Lemma 4's claim is the runtime half: both
    // lazy directions (mod_a's body read of B and mod_b's body read
    // of A) resolve to initialized bindings when residual's
    // post-init calls fire.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const A = "a-value";
const B = "b-value";
function readA() { return A; }
function readB() { return B; }
console.log(readA(), readB());
export { A, B, readA, readB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
            logical_module("mod_b", &[Member::new("B"), Member::new("readA")]),
        ],
    ));
    // ESM evaluates both modules to completion before the residual
    // entry's `console.log(readA(), readB())` fires; both function
    // bodies see fully-assigned bindings.
    assert_entry_output(&fixture, "a-value b-value\n");
}
