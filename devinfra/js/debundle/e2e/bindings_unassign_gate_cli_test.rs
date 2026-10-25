//! Unassign real bindings, then re-run the edited spec and emitted JS.

use debundle_e2e_support::GraphFixture;

#[test]
fn bindings_unassign_rejects_atom_split_in_both_modes_without_writes() {
    GraphFixture::atomic_pair().assert_rejected_unchanged(
        &["bindings", "unassign", "alpha"],
        &["splits one or more atomic groups"],
    );
}

#[test]
fn bindings_unassign_accepts_when_whole_atom_unassigned_together() {
    let fixture = GraphFixture::atomic_pair();
    fixture.assert_success(&["bindings", "unassign", "alpha", "beta"]);
    assert!(
        !fixture.modules.join("home/atom.yaml").exists(),
        "drained source must be deleted"
    );
    fixture.assert_runs("0 function\n");
}
