//! End-to-end exercise of `debundle modules list`'s filters by
//! shelling out to the built binary against a tiny modules fixture.

use debundle_e2e_support::{run_debundle, write_text_file};
use std::path::Path;

fn setup_modules_fixture(root: &Path) {
    write_text_file(
        &root.join("runtime/plugins.yaml"),
        "comment: plugin glue layer\nmembers:\n  - selector: { binding: { name: XOe } }\n",
    );
    write_text_file(
        &root.join("ui/sidebar.yaml"),
        "members:\n  - selector: { binding: { name: YOe } }\n  - selector: { binding: { name: ZOe } }\n",
    );
    write_text_file(&root.join("residual/unhandled.yaml"), "members: []\n");
    write_text_file(&root.join("ui/empty.yaml"), "members: []\n");
}

fn list(modules: &Path, extra: &[&str]) -> serde_json::Value {
    let mut args = vec![
        "modules",
        "list",
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
    ];
    args.extend_from_slice(extra);
    let out = run_debundle(&args);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

#[test]
fn modules_list_filters_preserve_sorted_paths_and_counts() {
    let dir = tempfile::tempdir().unwrap();
    setup_modules_fixture(dir.path());
    for (args, expected) in [
        (
            vec![],
            vec![
                ("residual/unhandled", 0),
                ("runtime/plugins", 1),
                ("ui/empty", 0),
                ("ui/sidebar", 2),
            ],
        ),
        (vec!["--residual"], vec![("residual/unhandled", 0)]),
        (
            vec!["--empty"],
            vec![("residual/unhandled", 0), ("ui/empty", 0)],
        ),
    ] {
        let report = list(dir.path(), &args);
        let rows: Vec<_> = report["modules"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| {
                (
                    m["path"].as_str().unwrap(),
                    m["member_count"].as_u64().unwrap(),
                )
            })
            .collect();
        assert_eq!(rows, expected, "{args:?}");
    }
}

#[test]
fn modules_list_empty_filter_excludes_canonical_source_claims_and_annotations() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write_text_file(&modules.join("ui/empty.yaml"), "members: []\n");
    write_text_file(
        &modules.join("ui/source_claim.yaml"),
        r#"source_matches:
  - match: "const claimed = 1;"
    bindings: [claimed]
"#,
    );
    write_text_file(
        &modules.join("ui/annotation_only.yaml"),
        r#"annotations:
  stale:
    note: preserved diagnostic debt
"#,
    );

    let parsed = list(&modules, &["--empty"]);
    let paths: Vec<&str> = parsed["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, vec!["ui/empty"]);
}
