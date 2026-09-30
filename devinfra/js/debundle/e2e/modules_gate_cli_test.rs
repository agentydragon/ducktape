//! End-to-end coverage of the realizability gate hookup in
//! `debundle modules merge` and `debundle modules delete --force`
//! (task #84). Shells out to the built `debundle` binary against
//! synthetic owner_graph.json fixtures.
//!
//! Fixtures are hand-rolled JSON so the cycle topology is precise:
//! the constraining edges in the owner graph, combined with the
//! post-edit partition the gate builds from the modified spec
//! YAMLs, force `validate_factorization` to surface an
//! unrealizable SCC (or none). The reject path's diagnostic is
//! the same `render_cycle_summary` text the pipeline prints when
//! the materializer's gate rejects.

use debundle_e2e_support::{
    graph_with_acyclic_cross_module_read, graph_with_merge_cycle_potential, owner_edge,
    owner_graph, owner_node, run_debundle, write_text_file,
};

/// Synthetic owner graph where alpha (owner:0) and beta (owner:1)
/// mutually eager-read each other. Pre-edit with alpha in module_a
/// and beta in module_b: a ↔ b cycle (unrealizable). Whatever the
/// caller does next — co-locating them into one module fixes it
/// (clean merge); deleting either while keeping the other still
/// leaves the surviving binding's owner pointing at residual,
/// which still cycles with the other module.
fn graph_with_mutual_cross_module_reads() -> String {
    owner_graph(
        "test/chunk",
        vec![
            owner_node("owner:0", 0, "alpha", "a"),
            owner_node("owner:1", 1, "beta", "b"),
        ],
        vec![
            owner_edge("owner_edge:0", "eager_use", "owner:0", "owner:1", "beta", 0),
            owner_edge(
                "owner_edge:1",
                "eager_use",
                "owner:1",
                "owner:0",
                "alpha",
                1,
            ),
        ],
    )
    .to_string()
}

#[test]
fn modules_merge_rejects_when_merge_creates_cycle() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write_text_file(&graph, &graph_with_merge_cycle_potential());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );
    write_text_file(
        &modules.join("c.yaml"),
        "members:\n  - selector: { binding: { name: gamma } }\n",
    );

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph.to_str().unwrap(),
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("unrealizable"),
        "expected unrealizability diagnostic, got stderr:\n{stderr}",
    );
    // The YAML must NOT have been written.
    assert!(modules.join("a.yaml").exists(), "a.yaml must survive");
    assert!(modules.join("b.yaml").exists(), "b.yaml must survive");
}

#[test]
fn modules_merge_accepts_clean_merge() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write_text_file(&graph, &graph_with_acyclic_cross_module_read());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph.to_str().unwrap(),
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    // After-merge: a.yaml exists, b.yaml has been removed.
    assert!(modules.join("a.yaml").exists());
    assert!(!modules.join("b.yaml").exists());
}

#[test]
fn modules_merge_gate_accepts_missing_target() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write_text_file(&graph, &graph_with_acyclic_cross_module_read());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph.to_str().unwrap(),
        "--target",
        "merged/new_target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(modules.join("merged/new_target.yaml").exists());
    assert!(!modules.join("a.yaml").exists());
    assert!(!modules.join("b.yaml").exists());
}

#[test]
fn modules_delete_force_rejects_when_post_state_unrealizable() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    // Pre-delete state already mutually-references — deleting either
    // module leaves the surviving one in a cycle with residual
    // (the orphaned binding's effective destination).
    write_text_file(&graph, &graph_with_mutual_cross_module_reads());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph.to_str().unwrap(),
        "b.yaml",
        "--force",
    ]);
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    // The mutual-eager-reads fixture forms one atomic unit; deleting
    // either module strands one member at residual while the other
    // stays on its module, which the atom-split check rejects before
    // the cycle check would. Either diagnostic is acceptable
    // evidence the gate fired.
    assert!(
        stderr.contains("unrealizable") || stderr.contains("splits one or more atomic units"),
        "expected unrealizability or atom-split diagnostic, got stderr:\n{stderr}",
    );
    assert!(modules.join("b.yaml").exists(), "b.yaml must survive");
}

#[test]
fn modules_delete_force_accepts_clean_deletion() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write_text_file(&graph, &graph_with_acyclic_cross_module_read());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    // Delete `b.yaml`: beta becomes unclaimed → residual. The
    // post-delete quotient is `a → residual`, still a DAG, so the
    // gate accepts.
    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph.to_str().unwrap(),
        "b.yaml",
        "--force",
    ]);
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(!modules.join("b.yaml").exists());
    assert!(modules.join("a.yaml").exists());
}
