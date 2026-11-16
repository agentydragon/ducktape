//! Unassign real bindings, then re-run the edited spec and emitted JS.

use debundle_e2e_graph_support::GraphFixture;
use std::{collections::BTreeSet, fs};
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

#[test]
fn non_inline_unclaim_requires_canonical_materialization_even_without_residual_modules() {
    let fixture = GraphFixture::per_atomic_group(
        "const alpha = 1;\nconsole.log(alpha);\n",
        &[(
            "a.yaml",
            "members: [{selector: {binding: {name: alpha}}}]\nanonymous_statements: [{match: 'console.log(alpha);'}]\n",
        )],
    );
    fixture.assert_rejected_unchanged(
        &["modules", "delete", "a.yaml", "--force"],
        &[
            "newly unclaimed",
            "non-inline unassigned_mode",
            "canonical materialization",
        ],
    );
    fixture.assert_runs("1\n");
}

#[test]
fn merge_preserves_generated_residual_dependencies() {
    let fixture = GraphFixture::per_atomic_group(
        "const beta = 1;\nconst alpha = beta + 1;\nconsole.log(alpha);\n",
        &[
            (
                "a.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("empty.yaml", "members: []\n"),
        ],
    );
    let mut final_modules: BTreeSet<String> = fixture
        .dependency_graph()
        .import_order
        .iter()
        .map(ToString::to_string)
        .filter(|module| !["a", "empty"].contains(&module.as_str()))
        .collect();
    final_modules.insert("combined".into());
    let priority =
        fixture.write_import_order("merge-order.json", &final_modules, &["combined".into()]);
    fixture.assert_success(&[
        "modules",
        "merge",
        "--target",
        "combined",
        "a.yaml",
        "empty.yaml",
        "--import-order",
        priority.to_str().unwrap(),
    ]);
    fixture.assert_runs("2\n");
}

#[test]
fn merge_repairs_rejected_baseline_through_shared_assignment_validator() {
    let fixture = GraphFixture::rejected(
        "let alpha = 0;\nfunction beta() { alpha = 1; }\nconsole.log(alpha, typeof beta);\n",
        &[
            (
                "a.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
        ],
    );
    let priority = fixture.root().join("merge-order.json");
    fs::write(&priority, "[\"repaired\"]\n").unwrap();
    fixture.assert_success(&[
        "modules",
        "merge",
        "--target",
        "repaired",
        "a.yaml",
        "b.yaml",
        "--import-order",
        priority.to_str().unwrap(),
    ]);
    fixture.assert_runs("0 function\n");
}

#[test]
fn delete_repairs_unresolved_direct_claim_in_rejected_baseline() {
    let fixture = GraphFixture::rejected(
        "const alpha = 1;\nconsole.log(alpha);\n",
        &[(
            "invalid.yaml",
            "members: [{selector: {binding: {name: missing}}}]\n",
        )],
    );
    fixture.assert_success(&["modules", "delete", "invalid.yaml", "--force"]);
    fixture.assert_runs("1\n");
}
