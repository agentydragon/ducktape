//! Pins docs/design.md "Lemma 1 (Linker order is post-order over
//! `I`)": ECMA-262's `InnerModuleEvaluation` evaluates the emitted
//! modules in a post-order DFS linearization of the imports graph
//! `I`, rooted at the entry.
//!
//! The fixture is a diamond: residual eager-reads `left_val` and
//! `right_val`, whose initializers each eager-read `base_val`. All
//! initializers are pure, so `I` carries only the diamond's `R`
//! edges — no `S` edges constrain the order. Per-module marker
//! prints (appended to the emitted files after the debundler runs,
//! so the analyzed graph is untouched) record each module body's
//! completion on stdout; the observed order must be a post-order
//! DFS over `I` rooted at the entry: the shared dependency before
//! either dependent, every dependent before residual, residual
//! last.
//!
//! Which of `mod_left` / `mod_right` evaluates first is the import
//! tie-break's choice — that steering is Lemma 2's contract (pinned
//! by `lemma_two_rescued_asymmetric_cycle_test` and siblings), so
//! this test only asserts the post-order relations Lemma 1 itself
//! proves.

use debundle_e2e_support::*;

#[test]
fn diamond_evaluation_order_is_post_order_dfs_over_imports() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const base_val = "b";
const left_val = base_val + "-l";
const right_val = base_val + "-r";
console.log(left_val, right_val);
"#,
        vec![
            logical_module("mod_base", &[Member::new("base_val")]),
            logical_module("mod_left", &[Member::new("left_val")]),
            logical_module("mod_right", &[Member::new("right_val")]),
        ],
    ));
    // Behaviour preservation first — the marker instrumentation
    // below mutates the emitted files.
    assert_entry_output(&fixture, "b-l b-r\n");

    let order =
        node_module_evaluation_order(&fixture, &["mod_base", "mod_left", "mod_right", "entry"]);
    let position = |label: &str| {
        order
            .iter()
            .position(|entry| entry == label)
            .unwrap_or_else(|| panic!("module {label} never evaluated; order: {order:?}"))
    };
    assert_eq!(
        order.len(),
        4,
        "every module evaluates exactly once: {order:?}"
    );
    // Post-order over I: every module's I-dependencies complete
    // before its own body does.
    assert!(
        position("mod_base") < position("mod_left"),
        "mod_left eager-reads base_val, so mod_base must evaluate first: {order:?}",
    );
    assert!(
        position("mod_base") < position("mod_right"),
        "mod_right eager-reads base_val, so mod_base must evaluate first: {order:?}",
    );
    // Rooted at the entry: the DFS root's body (which hosts
    // residual's statements) runs only after every requested module
    // unwinds.
    assert_eq!(
        order.last().map(String::as_str),
        Some("entry"),
        "the DFS root evaluates last in post-order: {order:?}",
    );
}
