//! Preparing source/spec inputs is independent of running the pipeline.

use debundle_e2e_support::*;
use std::path::PathBuf;

#[test]
fn prepared_tree_runs_the_current_inputs_without_an_eager_transform() {
    let workspace = write_tree_fixture(&TreeFixture {
        chunks: &[("main", "console.log(1);\n")],
        module_roots: &[],
        modules: &[],
    });
    assert!(!workspace.root().join("out").exists());
    write_text_file(
        &workspace.root().join("snapshot/main.js"),
        "console.log(2);\n",
    );
    let run = workspace.run(&[]);
    assert!(run.result.status.success(), "{}", run.result.stderr);
    assert_node_output(&run.out_root.join("app/main/entry.js"), "2\n", "");
}

#[test]
fn exact_tree_observation_preserves_binary_files_and_relative_paths() {
    let workspace = write_tree_fixture(&TreeFixture {
        chunks: &[("main", "console.log(1);\n")],
        module_roots: &[],
        modules: &[],
    });
    let original = workspace.root().join("original");
    let copy = workspace.root().join("copy");
    for root in [&original, &copy] {
        std::fs::create_dir_all(root.join("nested")).unwrap();
        std::fs::write(root.join("nested/bytes.bin"), [0, 255, 13, 10]).unwrap();
    }
    assert_eq!(
        read_tree(&original),
        std::collections::BTreeMap::from([(
            PathBuf::from("nested/bytes.bin"),
            vec![0, 255, 13, 10]
        ),])
    );
    assert_same_tree(&original, &copy);
}
