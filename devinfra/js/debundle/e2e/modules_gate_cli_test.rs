//! Module edits against dependency graphs generated from executable JS/specs.

use debundle_e2e_graph_support::GraphFixture;
use std::fs;

#[test]
fn graph_only_merge_reports_need_for_full_assessment() {
    let fixture = GraphFixture::dependency_chain();
    let args = ["modules", "merge", "--target", "a.yaml", "b.yaml"];
    let preview = fixture.json(&[&args[..], &["--dry-run"]].concat());
    assert_eq!(preview["gate"], "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&args);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    fixture.assert_runs("3\n");
}

#[test]
fn graph_only_merge_preserves_existing_and_missing_targets() {
    for (target, sources) in [
        ("a", vec!["b.yaml"]),
        ("merged/new_target", vec!["a.yaml", "b.yaml"]),
    ] {
        let fixture = GraphFixture::acyclic_pair();
        let mut args = vec!["modules", "merge", "--target", target];
        args.extend(sources.iter().copied());
        let before: Vec<_> = ["a.yaml", "b.yaml"]
            .into_iter()
            .map(|path| (path, fs::read(fixture.modules.join(path)).unwrap()))
            .collect();
        let mut preview = args.clone();
        preview.push("--dry-run");
        let report = fixture.json(&preview);
        assert_eq!(report["action"], "dry-run");
        assert_eq!(report["needs_full_assessment"], true);
        for (path, bytes) in before {
            assert_eq!(fs::read(fixture.modules.join(path)).unwrap(), bytes);
        }
        assert_eq!(
            fixture.modules.join(format!("{target}.yaml")).exists(),
            target == "a"
        );
        let output = fixture.command(&args);
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
        assert_eq!(
            fixture.modules.join(format!("{target}.yaml")).exists(),
            target == "a"
        );
        for source in sources {
            assert!(fixture.modules.join(source).exists());
        }
        fixture.assert_runs("2\n");
    }
}

#[test]
fn graph_only_force_delete_cannot_claim_final_acceptance() {
    let fixture = GraphFixture::acyclic_pair();
    // Unlike a fabricated cyclic input, this pre-edit spec is executable.
    // Deleting beta's module leaves emitted alpha reading beta from entry,
    // which evaluates last and therefore cannot provide eager dependencies.
    let args = ["modules", "delete", "b.yaml", "--force"];
    let preview = fixture.json(&[&args[..], &["--dry-run"]].concat());
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&args);
    assert!(!output.status.success());
    assert!(fixture.modules.join("b.yaml").exists());
    fixture.assert_runs("2\n");
}

#[test]
fn graph_only_clean_force_delete_still_needs_full_assessment() {
    let fixture = GraphFixture::acyclic_pair();
    let preview = fixture.json(&["modules", "delete", "a.yaml", "--force", "--dry-run"]);
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&["modules", "delete", "a.yaml", "--force"]);
    assert!(!output.status.success());
    assert!(fixture.modules.join("a.yaml").exists());
    assert!(fixture.modules.join("b.yaml").exists());
    fixture.assert_runs("2\n");
}

#[test]
fn claim_check_resolves_decorator_statement_claims_from_real_source() {
    // A leading export is not an unit: source statement ordinals must not be
    // confused with positions in the source_unit table when resolving the decorator.
    // Co-own the prefix so this claim-resolution fixture preserves source order.
    let fixture = GraphFixture::new(
        r#"export { Co };
const ignored = 0;
class Co {}
function Ro(tags, target, name, value) { target[name] = value; }
const Z = 1;
Ro([Z], Co.prototype, "visible", 2);
console.log(new Co().visible);
"#,
        &[
            (
                "state.yaml",
                r#"members:
  - selector: {binding: {name: ignored}}
  - selector: {binding: {name: Co}}
  - selector: {binding: {name: Ro}}
  - selector: {binding: {name: Z}}
anonymous_statements:
  - match: 'Ro([Z], Co.prototype, "visible", 2);'
    note: observable decorator side effect
"#,
            ),
            ("empty.yaml", "members: []"),
        ],
    );
    let preview = fixture.json(&[
        "modules",
        "merge",
        "--target",
        "state",
        "empty",
        "--dry-run",
    ]);
    assert_eq!(preview["gate"], "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&["modules", "merge", "--target", "state", "empty"]);
    assert!(!output.status.success());
    assert!(fixture.modules.join("empty.yaml").exists());
    fixture.assert_runs("2\n");
}
