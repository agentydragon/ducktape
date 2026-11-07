//! Unassign real bindings, then re-run the edited spec and emitted JS.

use debundle_e2e_graph_support::GraphFixture;
use std::fs;

#[test]
fn graph_only_unassign_previews_but_cannot_write_without_full_assessment() {
    let fixture = GraphFixture::atomic_pair();
    let path = fixture.modules.join("home/atom.yaml");
    let before = fs::read(&path).unwrap();
    let preview = fixture.json(&["bindings", "unassign", "alpha", "--dry-run"]);
    assert_eq!(preview["gate"], "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&["bindings", "unassign", "alpha"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    assert_eq!(fs::read(path).unwrap(), before);
}

#[test]
fn graph_only_whole_atom_unassign_still_needs_full_assessment() {
    let fixture = GraphFixture::atomic_pair();
    let preview = fixture.json(&["bindings", "unassign", "alpha", "beta", "--dry-run"]);
    assert_eq!(preview["unassigned"], 2);
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&["bindings", "unassign", "alpha", "beta"]);
    assert!(!output.status.success());
    assert!(fixture.modules.join("home/atom.yaml").exists());
}

#[test]
fn full_assessment_allows_unassigning_the_whole_atomic_group() {
    let fixture = GraphFixture::new_full_tree(
        "let alpha = 0;\nfunction beta() { alpha = 1; }\nconsole.log(alpha, typeof beta);\n",
        &[(
            "home/atom.yaml",
            "members: [{selector: {binding: {name: alpha}}}, {selector: {binding: {name: beta}}}]\n",
        )],
    );
    let analysis = fixture.stage_analysis();
    fixture.assert_success_with_assessment(&["bindings", "unassign", "alpha", "beta"], &analysis);
    assert!(
        !fixture.modules.join("home/atom.yaml").exists(),
        "drained source must be deleted"
    );
    fixture.assert_runs("0 function\n");
}
