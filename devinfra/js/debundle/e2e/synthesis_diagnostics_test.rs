//! Synthesis skips carry semantic evidence in JSON and readable text at the CLI.
use debundle_e2e_support::{parse_stdout_json, run_synthesize_selectors, write_text_file};

fn synthesize(source: &str, binding: &str) -> serde_json::Value {
    let dir = tempfile::tempdir().unwrap();
    let source_path = dir.path().join("app.js");
    write_text_file(&source_path, source);
    let modules = dir.path().join("modules");
    write_text_file(
        &modules.join("functions.yaml"),
        &format!(
            "members:\n  - name: Helper\n    selector:\n      binding:\n        name: {binding}\n",
        ),
    );
    let output = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source_path.to_str().unwrap(),
            "--format",
            "json",
        ],
    );
    parse_stdout_json(&output)
}

#[test]
fn missing_binding_skip_retains_identity_and_count() {
    let report = synthesize("const present = 7;\n", "missing");
    let reason = &report["candidates"][0]["reason"];
    assert_eq!(reason["kind"], "binding_declaration_count", "{report:#}");
    assert_eq!(reason["binding"], "missing", "{report:#}");
    assert_eq!(reason["count"], 0, "{report:#}");
}

#[test]
fn duplicate_declarations_retain_the_shared_resolver_outcome() {
    let report = synthesize(
        "function first() { return 7; }\nfunction second() { return 7; }\n",
        "first",
    );
    let reason = &report["candidates"][0]["reason"];
    assert_eq!(reason["kind"], "exact_declaration_rejected", "{report:#}");
    assert_eq!(reason["failure"]["kind"], "target_not_proven", "{report:#}");
    assert_eq!(reason["failure"]["binding"], "first", "{report:#}");
    assert_eq!(
        reason["failure"]["outcome"]["outcome"]["kind"], "ambiguous",
        "{report:#}"
    );
    assert_eq!(
        reason["failure"]["outcome"]["outcome"]["candidates"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "{report:#}"
    );
}

#[test]
fn missing_binding_text_uses_the_explicit_human_explanation() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    write_text_file(&source, "const present = 7;\n");
    let modules = dir.path().join("modules");
    write_text_file(
        &modules.join("functions.yaml"),
        "members:\n  - name: Helper\n    selector:\n      binding:\n        name: missing\n",
    );
    let output = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--format",
            "text",
        ],
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.contains("binding `missing` is not declared in the source chunk"),
        "{text}"
    );
}
