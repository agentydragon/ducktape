use std::fs;
use std::path::Path;

use debundle_e2e_support::{run_debundler_tree_with_env, write_text_file};

#[test]
fn parallel_chunk_solves_write_distinct_diagnostics() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let snapshot = root.join("snapshot");
    let extracted = root.join("extracted");
    let modules = root.join("modules");
    let out = root.join("out");
    let request_dir = out.join("debug/selector_cpsat_requests");
    let summary_dir = out.join("debug/selector_cpsat_summaries");

    write_text_file(
        &snapshot.join("cli.js"),
        "function cliBroad() { return 'common-cli'; }\nfunction cliSpecific() { return 'specific-cli'; }\nexport { cliBroad, cliSpecific };\n",
    );
    write_text_file(
        &snapshot.join("print.js"),
        "function printBroad() { return 'common-print'; }\nfunction printSpecific() { return 'specific-print'; }\nexport { printBroad, printSpecific };\n",
    );
    write_text_file(&extracted.join("js-files.txt"), "cli.js\nprint.js\n");
    write_selector_modules(&modules.join("chunks/cli/routes.yaml"), "cli");
    write_selector_modules(&modules.join("chunks/print/routes.yaml"), "print");
    let config = root.join("spec_config.yaml");
    write_text_file(
        &config,
        r#"main_chunk_id: cli
module_roots:
  cli: chunks/cli
  print: chunks/print
inputs:
  root: snapshot
  js_list_path: extracted/js-files.txt
write_js_tree: true
unassigned_mode:
  cli: { kind: inline_in_entry }
  print: { kind: inline_in_entry }
"#,
    );
    let vendor_marks = root.join("vendor_marks.yaml");
    write_text_file(&vendor_marks, "vendor_marks: []\n");

    let result = run_debundler_tree_with_env(
        &config,
        &modules,
        &vendor_marks,
        root,
        &out,
        &[
            (
                "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO_DIR",
                request_dir.to_str().unwrap(),
            ),
            (
                "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR",
                summary_dir.to_str().unwrap(),
            ),
        ],
    );
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    assert_eq!(files_with_prefix(&request_dir, "selector-cpsat-").len(), 2);
    assert_eq!(files_with_prefix(&summary_dir, "selector-cpsat-").len(), 2);
    assert_eq!(
        files_with_prefix(&summary_dir, "selector-pre-solver-").len(),
        4,
    );
}

fn write_selector_modules(path: &Path, suffix: &str) {
    write_text_file(
        path,
        &format!(
            r#"source_matches:
  - match: |
      function selected() {{
        return ANYTHING;
      }}
    bindings:
      - local: selected
        name: Broad{suffix}
  - match: |
      function selected() {{
        return "specific-{suffix}";
      }}
    bindings:
      - local: selected
        name: Specific{suffix}
"#,
        ),
    );
}

fn files_with_prefix(dir: &Path, prefix: &str) -> Vec<String> {
    let mut files = fs::read_dir(dir)
        .unwrap_or_else(|error| panic!("read {}: {error}", dir.display()))
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with(prefix))
        .collect::<Vec<_>>();
    files.sort();
    files
}
