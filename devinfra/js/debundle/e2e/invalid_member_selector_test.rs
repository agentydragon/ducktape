//! A `members[].selector` that fails validation claims no binding name: a
//! command that reads module YAML without resolving selectors refuses it
//! instead of taking a present `binding.name` at face value.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::write_text_file;

#[test]
fn unverified_merge_refuses_a_module_whose_selector_fails_validation() {
    let fixture = GraphFixture::acyclic_pair();
    // `binding` beside a second selector key is invalid, yet still spells a name.
    // Every graph-gated command rejects the edited file by digest, so the merge
    // runs unverified: name collection is the only check left between it and a
    // write.
    write_text_file(
        &fixture.modules.join("a.yaml"),
        "members: [{name: Alpha, selector: {binding: {name: alpha}, cross_ref: {references: Beta}}}]\n",
    );
    fixture.assert_rejected_unchanged_without_graph(
        &["modules", "merge", "--target", "b", "a", "--no-verify"],
        &["exactly one of"],
    );
}
