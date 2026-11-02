//! Full graph compression preserves query, snapshot and emitted-program behavior.

use debundle_e2e_support::*;
use serde_json::Value;
use std::fs;

#[test]
fn full_gzip_graphs_support_queries_and_snapshots() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_runs("2\n");
    let graph: Value = read_gzip_json(&fixture.graph);
    let program_path = program_graph_path(&fixture.graph);
    for path in [&fixture.graph, &program_path] {
        let bytes = fs::read(path).unwrap();
        assert_eq!(&bytes[..8], &[0x1f, 0x8b, 8, 0, 0, 0, 0, 0]);
        assert!(
            !path.with_extension("").exists(),
            "no plaintext graph alias"
        );
    }
    assert_eq!(graph["program_graph"], "../program_graph.json.gz");
    for query in ["graph-summary", "atoms", "coverage", "modules", "integrity"] {
        let args = if query == "modules" {
            vec!["modules", "propose", "--format", "json"]
        } else if query == "integrity" {
            vec![
                "explain-integrity",
                "--slot",
                "Object.defineProperty",
                "--format",
                "json",
            ]
        } else {
            vec![query, "--format", "json"]
        };
        let mut args = args;
        args.extend(["--graph", fixture.graph.to_str().unwrap()]);
        let result = fixture.command(&args);
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let _: Value = serde_json::from_slice(&result.stdout).unwrap();
    }
    let snapshots = tempfile::tempdir().unwrap();
    let destination = snapshots.path().join("gzip-snapshot");
    let result = fixture.command(&[
        "snapshot",
        "create",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--destination",
        destination.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let snapshot: Value = serde_json::from_slice(&result.stdout).unwrap();
    let copied_graph = std::path::Path::new(snapshot["graph"].as_str().unwrap());
    assert_eq!(copied_graph.file_name(), fixture.graph.file_name());
    assert_eq!(
        fs::read(copied_graph).unwrap(),
        fs::read(&fixture.graph).unwrap()
    );
    let copied_program = program_graph_path(copied_graph);
    assert_eq!(
        fs::read(copied_program).unwrap(),
        fs::read(program_graph_path(&fixture.graph)).unwrap()
    );
    let verified = fixture.command(&["snapshot", "verify", destination.to_str().unwrap()]);
    assert!(
        verified.status.success(),
        "{}",
        String::from_utf8_lossy(&verified.stderr)
    );
}

#[test]
fn corrupt_gzip_query_is_an_error_and_rejection_reports_stay_compressed() {
    let fixture = GraphFixture::acyclic_pair();
    let mut bytes = fs::read(&fixture.graph).unwrap();
    bytes.pop();
    fs::write(&fixture.graph, bytes).unwrap();
    let result = fixture.command(&["graph-summary", "--format", "json"]);
    assert_eq!(result.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&result.stderr).contains("JSON report"));

    let rejected = GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconsole.log(last);\n",
        &[
            (
                "outer.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}]\n",
            ),
            (
                "inner.yaml",
                "members: [{selector: {binding: {name: middle}}}]\n",
            ),
        ],
    );
    assert_eq!(&fs::read(rejected.graph).unwrap()[..2], &[0x1f, 0x8b]);
}
