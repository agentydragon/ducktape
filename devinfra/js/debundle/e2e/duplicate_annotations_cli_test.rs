use debundle_e2e_support::{
    TreeFixture, assert_module_source, assert_node_output, run_tree_fixture,
};

const SOURCE: &str = "const factory = () => 1; const other = 2; console.log(factory() + other);";

#[test]
fn duplicate_binding_annotation_keys_are_rejected_by_run_cli() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/factory.yaml",
                r#"members:
  - selector: { binding: { name: factory } }
annotations:
  factory:
    retained_record_factory: true
  factory:
    note: overwritten annotation
"#,
            )],
        },
        &[],
    );

    assert_eq!(
        run.result.status.code(),
        Some(1),
        "duplicate annotation should fail the CLI: {}",
        run.result.stderr
    );
    assert!(
        run.result.stderr.contains("duplicate"),
        "{}",
        run.result.stderr
    );
    assert!(
        run.result.stderr.contains("factory"),
        "{}",
        run.result.stderr
    );
    assert!(
        !run.out_root.join("app/main/entry.js").exists(),
        "a rejected spec must not emit the entry"
    );
}

#[test]
fn distinct_binding_annotations_are_emitted_separately() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/factory.yaml",
                r#"members:
  - selector: { binding: { name: factory } }
  - selector: { binding: { name: other } }
annotations:
  factory: { comment: factory annotation }
  other: { comment: other annotation, note: retained note }
"#,
            )],
        },
        &[],
    );

    assert!(run.result.status.success(), "{}", run.result.stderr);
    assert_module_source(
        &run.out_root,
        "app/main/factory.js",
        &["// factory annotation", "// other annotation"],
        &["retained note"],
    );
    assert_node_output(&run.out_root.join("app/main/entry.js"), "3\n", "");
}
