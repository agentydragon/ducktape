//! End-to-end coverage for `debundle spec selector-codemod`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

fn debundle_binary() -> PathBuf {
    let runfiles_path = std::env::var("RUNFILES_DIR")
        .or_else(|_| std::env::var("TEST_SRCDIR"))
        .expect("runfiles env var");
    let candidate = Path::new(&runfiles_path).join("_main/devinfra/js/debundle/debundle");
    assert!(
        candidate.exists(),
        "debundle binary not at {}",
        candidate.display()
    );
    candidate
}

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn run_codemod(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "selector-codemod",
        "--modules",
        modules.to_str().unwrap(),
    ];
    args.extend_from_slice(extra);
    let out = Command::new(debundle_binary())
        .args(&args)
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "non-zero exit\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

fn parse_stdout_json(out: &std::process::Output) -> Value {
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err})\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr),
        )
    })
}

fn fixture(modules: &Path) -> (PathBuf, PathBuf) {
    let target = modules.join("ui/widgets.yaml");
    let other = modules.join("other/untouched.yaml");
    write(
        &target,
        r#"members:
  - name: WidgetFactory
    selector:
      source_match:
        match: |
          const widgetFactory = makeWidgetFactory();
  - name: First
    selector:
      source_match:
        match: |
          const first = buildFirst(), second = buildSecond();
  - name: AlreadyDone
    selector:
      source_match:
        target_binding: alreadyDone
        match: |
          const alreadyDone = initAlreadyDone();
"#,
    );
    write(
        &other,
        r#"members:
  - name: OutsideFilter
    selector:
      source_match:
        match: |
          const outsideFilter = makeOutsideFilter();
"#,
    );
    (target, other)
}

fn anything_holes_fixture(modules: &Path) -> PathBuf {
    let target = modules.join("ui/selector_holes.yaml");
    write(
        &target,
        r#"members:
  - name: WidgetConfig
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: widgetConfig
        match: |
          const widgetConfig = makeWidget(EXPR, {
            stable: EXPR,
            OBJECT_PROPS,
            other: EXPR,
          }, ARGS);
  - name: NamedHolesStayReadable
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: namedConfig
        match: |
          const namedConfig = makeWidget(EXPR_VALUE, { OBJECT_PROPS_GENERATED });
"#,
    );
    target
}

#[test]
fn dry_run_reports_single_binding_rewrite_without_writing() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let (target, other) = fixture(&modules);
    let before_target = fs::read_to_string(&target).unwrap();
    let before_other = fs::read_to_string(&other).unwrap();

    let out = run_codemod(&modules, &["--file", "ui/widgets.yaml", "--format", "json"]);
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["action"], "dry_run", "{parsed}");
    assert_eq!(parsed["rewrite"], "single_target_binding", "{parsed}");
    assert_eq!(parsed["summary"]["dry_run"], true, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["source_match_members"], 3, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["skipped_candidates"], 2, "{parsed}");
    assert_eq!(
        parsed["summary"]["files_written"].as_array().unwrap().len(),
        0,
        "{parsed}"
    );
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|candidate| {
                candidate["action"] == "would_change"
                    && candidate["target_binding"] == "widgetFactory"
            }),
        "{parsed}"
    );
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|candidate| {
                candidate["action"] == "skipped"
                    && candidate["reason"] == "source_match declares 2 top-level bindings"
            }),
        "{parsed}"
    );
    assert_eq!(fs::read_to_string(&target).unwrap(), before_target);
    assert_eq!(fs::read_to_string(&other).unwrap(), before_other);
}

#[test]
fn apply_adds_target_binding_and_honors_module_prefix_filter() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let (target, other) = fixture(&modules);
    let before_other = fs::read_to_string(&other).unwrap();

    let out = run_codemod(
        &modules,
        &["--module-prefix", "ui", "--apply", "--format", "json"],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["summary"]["dry_run"], false, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(
        parsed["summary"]["files_written"].as_array().unwrap().len(),
        1,
        "{parsed}"
    );
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|candidate| {
                candidate["action"] == "changed" && candidate["target_binding"] == "widgetFactory"
            }),
        "{parsed}"
    );

    let target_doc: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&target).unwrap()).unwrap();
    let source_match = &target_doc["members"].as_sequence().unwrap()[0]["selector"]["source_match"];
    assert_eq!(source_match["target_binding"], "widgetFactory");
    assert_eq!(
        source_match["match"].as_str().unwrap().trim(),
        "const widgetFactory = makeWidgetFactory();"
    );
    assert_eq!(fs::read_to_string(&other).unwrap(), before_other);
}

#[test]
fn dry_run_reports_anonymous_holes_without_writing() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let target = anything_holes_fixture(&modules);
    let before_target = fs::read_to_string(&target).unwrap();

    let out = run_codemod(
        &modules,
        &[
            "--rewrite",
            "anything-holes",
            "--file",
            "ui/selector_holes.yaml",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["action"], "dry_run", "{parsed}");
    assert_eq!(parsed["rewrite"], "anything_holes", "{parsed}");
    assert_eq!(parsed["summary"]["source_match_members"], 2, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["skipped_candidates"], 1, "{parsed}");
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|candidate| {
                candidate["action"] == "would_change"
                    && candidate["replacement_count"] == 5
                    && candidate["rewritten_holes"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|hole| hole == "OBJECT_PROPS")
            }),
        "{parsed}"
    );
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|candidate| {
                candidate["action"] == "skipped"
                    && candidate["reason"]
                        == "no anonymous typed holes can be normalized to ANYTHING"
            }),
        "{parsed}"
    );
    assert_eq!(fs::read_to_string(&target).unwrap(), before_target);
}

#[test]
fn apply_rewrites_anonymous_typed_holes_to_anything() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let target = anything_holes_fixture(&modules);

    let out = run_codemod(
        &modules,
        &[
            "--rewrite",
            "anything-holes",
            "--module",
            "ui/selector_holes",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(
        parsed["summary"]["files_written"].as_array().unwrap().len(),
        1,
        "{parsed}"
    );

    let rewritten = fs::read_to_string(&target).unwrap();
    assert!(
        rewritten.contains("const widgetConfig = makeWidget(ANYTHING, {"),
        "{rewritten}"
    );
    assert!(rewritten.contains("stable: ANYTHING"), "{rewritten}");
    assert!(rewritten.contains("ANYTHING,"), "{rewritten}");
    assert!(rewritten.contains("}, ANYTHING);"), "{rewritten}");
    assert!(
        rewritten.contains("EXPR_VALUE"),
        "named expression holes should stay readable:\n{rewritten}"
    );
    assert!(
        rewritten.contains("OBJECT_PROPS_GENERATED"),
        "named object-property holes should stay readable:\n{rewritten}"
    );
}
