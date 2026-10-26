//! Module edits against dependency graphs generated from executable JS/specs.

use debundle_e2e_support::GraphFixture;
use std::fs;

#[test]
fn modules_merge_rejects_when_merge_creates_cycle() {
    let fixture = GraphFixture::dependency_chain();
    fixture.assert_rejected_unchanged(
        &["modules", "merge", "--target", "a.yaml", "b.yaml"],
        &["unrealizable"],
    );
    fixture.assert_runs("3\n");
}

#[test]
fn modules_merge_accepts_existing_and_missing_targets() {
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
        assert_eq!(fixture.json(&preview)["action"], "dry-run");
        for (path, bytes) in before {
            assert_eq!(fs::read(fixture.modules.join(path)).unwrap(), bytes);
        }
        assert_eq!(
            fixture.modules.join(format!("{target}.yaml")).exists(),
            target == "a"
        );
        fixture.assert_success(&args);
        assert!(fixture.modules.join(format!("{target}.yaml")).exists());
        for source in sources {
            assert!(!fixture.modules.join(source).exists());
        }
        fixture.assert_runs("2\n");
    }
}

#[test]
fn modules_delete_force_rejects_stranding_an_eager_provider_in_entry() {
    let fixture = GraphFixture::acyclic_pair();
    // Unlike a fabricated cyclic input, this pre-edit spec is executable.
    // Deleting beta's module leaves emitted alpha reading beta from entry,
    // which evaluates last and therefore cannot provide eager dependencies.
    fixture.assert_rejected_unchanged(
        &["modules", "delete", "b.yaml", "--force"],
        &["unrealizable"],
    );
    fixture.assert_runs("2\n");
}

#[test]
fn modules_delete_force_accepts_clean_deletion() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_success(&["modules", "delete", "a.yaml", "--force"]);
    assert!(!fixture.modules.join("a.yaml").exists());
    assert!(fixture.modules.join("b.yaml").exists());
    fixture.assert_runs("2\n");
}

#[test]
fn edit_gate_resolves_decorator_statement_claims_from_real_source() {
    // A leading export is not an unit: source statement ordinals must not be
    // confused with positions in the source_unit table when resolving the decorator.
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
    fixture.assert_success(&[
        "modules",
        "merge",
        "--target",
        "state",
        "empty",
        "--dry-run",
    ]);
    fixture.assert_success(&["modules", "merge", "--target", "state", "empty"]);
    fixture.assert_runs("2\n");
}
