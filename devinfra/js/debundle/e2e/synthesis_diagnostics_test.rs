//! Synthesis skips carry semantic evidence in JSON and readable text at the CLI.
use debundle_e2e_support::{CaseWorkspace, parse_stdout_json, run_synthesize_selectors};

fn synthesize(source: &str, binding: &str, format: &str) -> (CaseWorkspace, std::process::Output) {
    let workspace = CaseWorkspace::default();
    let source_path = workspace.write_text("app.js", source);
    workspace.write_text(
        "modules/functions.yaml",
        &format!(
            "members:\n  - name: Helper\n    selector:\n      binding:\n        name: {binding}\n",
        ),
    );
    let output = run_synthesize_selectors(
        &workspace.path().join("modules"),
        &[
            "--source-file",
            source_path.to_str().unwrap(),
            "--format",
            format,
        ],
    );
    (workspace, output)
}

#[test]
fn missing_binding_skip_retains_identity_and_count() {
    let (_workspace, output) = synthesize("const present = 7;\n", "missing", "json");
    let report = parse_stdout_json(&output);
    let reason = &report["candidates"][0]["reason"];
    assert_eq!(reason["kind"], "binding_declaration_count", "{report:#}");
    assert_eq!(reason["binding"], "missing", "{report:#}");
    assert_eq!(reason["count"], 0, "{report:#}");
}

#[test]
fn duplicate_declarations_retain_the_shared_resolver_outcome() {
    let (_workspace, output) = synthesize(
        "function first() { return 7; }\nfunction second() { return 7; }\n",
        "first",
        "json",
    );
    let report = parse_stdout_json(&output);
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
    let (_workspace, output) = synthesize("const present = 7;\n", "missing", "text");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.contains("binding `missing` is not declared in the source chunk"),
        "{text}"
    );
}
