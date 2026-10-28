//! Runtime failures from read-only top-level queries remain machine-readable.

use debundle_e2e_support::GraphFixture;
use serde_json::Value;
use std::fs;

fn assert_structured_failure(
    out: std::process::Output,
    format: &str,
    reason: &str,
    message_part: &str,
) {
    assert_eq!(
        out.status.code(),
        Some(1),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8(out.stdout).expect("failure output is UTF-8");
    let failure: Value = match format {
        "json" => serde_json::from_str(&stdout).expect("one JSON failure object"),
        "ndjson" => {
            let rows: Vec<_> = stdout.lines().collect();
            assert_eq!(rows.len(), 1, "expected one NDJSON row: {stdout:?}");
            serde_json::from_str(rows[0]).expect("one NDJSON failure object")
        }
        _ => panic!("unsupported test output format {format:?}"),
    };
    assert_eq!(failure["reason"], reason);
    let message = failure["message"].as_str().expect("failure message");
    assert!(
        String::from_utf8_lossy(&out.stderr).contains(message),
        "stderr did not retain the full JSON message: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        message.contains(message_part),
        "failure did not include {message_part:?}: {failure:#}"
    );
}

#[test]
fn top_level_query_runtime_failures_are_structured_in_json_formats() {
    let fixture = GraphFixture::new("const alpha = 1;\n", &[]);
    let graph_bytes = fs::read(&fixture.graph).expect("read valid dependency graph");

    fs::write(&fixture.graph, "{").expect("make malformed graph");
    let malformed_queries: &[&[&str]] = &[
        &["describe", "unit:0"],
        &["inspect-source", "--id", "unit:0"],
        &["graph-summary"],
        &["atoms"],
        &["coverage"],
        &["scc"],
        &["cluster", "alpha"],
    ];
    for format in ["json", "ndjson"] {
        for query in malformed_queries {
            let mut args = query.to_vec();
            args.extend(["--format", format]);
            assert_structured_failure(
                fixture.command(&args),
                format,
                "invalid_json",
                &fixture.graph.display().to_string(),
            );
        }
    }
    fs::write(&fixture.graph, graph_bytes).expect("restore valid dependency graph");

    let missing_graph = fixture.root().join("missing-dependency-graph.json");
    let missing_graph = missing_graph.to_str().expect("UTF-8 fixture path");
    for format in ["json", "ndjson"] {
        assert_structured_failure(
            fixture.command(&["atoms", "--graph", missing_graph, "--format", format]),
            format,
            "io",
            missing_graph,
        );
        assert_structured_failure(
            fixture.command(&["inspect-source", "--id", "unit:99999", "--format", format]),
            format,
            "source_unit_not_found",
            "source-unit ID \"unit:99999\" not found in dependency graph",
        );

        let missing_source = fixture.root().join("missing-source.js");
        let missing_source = missing_source.to_str().expect("UTF-8 fixture path");
        assert_structured_failure(
            fixture.command(&[
                "inspect-source",
                "--source-file",
                missing_source,
                "--format",
                format,
            ]),
            format,
            "io",
            missing_source,
        );
    }

    let missing_source = fixture.root().join("missing-source.js");
    let missing_source = missing_source.to_str().expect("UTF-8 fixture path");
    let text = fixture.command(&[
        "inspect-source",
        "--source-file",
        missing_source,
        "--format",
        "text",
    ]);
    assert_eq!(text.status.code(), Some(1));
    assert!(text.stdout.is_empty());
    assert!(String::from_utf8_lossy(&text.stderr).contains(missing_source));

    fs::write(fixture.source_path(), "const alpha = 2;\n").expect("make graph source stale");
    let mismatch = fixture.command(&["inspect-source", "--id", "unit:0", "--format", "json"]);
    assert_eq!(mismatch.status.code(), Some(1));
    let mismatch: Value = serde_json::from_slice(&mismatch.stdout).expect("legacy mismatch JSON");
    assert_eq!(mismatch.as_object().expect("mismatch object").len(), 3);
    assert_eq!(mismatch["kind"], "graph_compatibility");
    assert_eq!(mismatch["action"], "rejected");
    assert_eq!(mismatch["mismatch"]["reason"], "file");
}
