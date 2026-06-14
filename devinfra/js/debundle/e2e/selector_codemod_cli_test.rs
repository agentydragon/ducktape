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

fn run_synthesize_selectors(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "synthesize-selectors",
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

fn commented_single_target_fixture(modules: &Path) -> PathBuf {
    let target = modules.join("app/bootstrap.yaml");
    write(
        &target,
        r#"# module-level note must survive
# another note that used to be lost by serde rewrites
comment: |
  Keep this module grouped with startup.
members:
  # keep the member comment
  - name: StartupFactory
    selector:
      source_match:
        identifiers: alpha_all
        # keep the selector comment
        match: |
          const startupFactory = createStartupFactory();
  - name: AlreadyAnchored
    selector:
      source_match:
        target_binding: alreadyAnchored
        match: |
          const alreadyAnchored = createAlreadyAnchored();

anonymous_statements:
  - match: |
      initializeRuntime();
"#,
    );
    target
}

fn synthesis_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"const beforeConfig = helper("before"),
  runtimePrimary = buildConfig({ stable: "primary", generated: "ignore-a" }),
  middleConfig = helper("middle"),
  runtimeSecondary = buildConfig({ stable: "secondary", generated: "ignore-b" }),
  afterConfig = helper("after");
function runtimeFormatter(value) {
  return value.trim().toUpperCase();
}
function helper(value) {
  return value;
}
function buildConfig(value) {
  return value;
}
console.log(runtimePrimary, runtimeSecondary, runtimeFormatter(" ok "));
export { runtimePrimary, runtimeSecondary, runtimeFormatter };
"#,
    );
    let modules = root.join("modules");
    let module = modules.join("app/config.yaml");
    write(
        &module,
        r#"members:
  - name: PrimaryConfig
    comment: Primary config comment
    selector:
      binding:
        name: runtimePrimary
  - name: SecondaryConfig
    comment: Secondary config comment
    selector:
      binding:
        name: runtimeSecondary
  - name: FormatValue
    selector:
      binding:
        name: runtimeFormatter
"#,
    );
    write(
        &modules.join("ignored/noisy.yaml"),
        r#"members:
  - name: NoisyOne
    selector:
      binding:
        name: noisyOne
  - name: NoisyTwo
    selector:
      binding:
        name: noisyTwo
"#,
    );
    (modules, source)
}

fn synthesis_single_member_text_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"function runtimeFormatter(value) {
  return value.trim().toUpperCase();
}
function untouchedBinding() {
  return "still name-only";
}
export { runtimeFormatter, untouchedBinding };
"#,
    );
    let modules = root.join("modules");
    let module = modules.join("app/bootstrap.yaml");
    write(
        &module,
        r#"# merged from: legacy/bootstrap.yaml
comment: |
  Keep this module grouped with startup.
members:
  # keep the neighboring member untouched
  - name: UntouchedBinding
    selector:
      binding:
        name: untouchedBinding
  # keep the selected member's surrounding YAML comment
  - name: FormatValue
    comment: Keep readable comment field.
    # keep the pre-selector YAML comment
    selector:
      binding:
        name: runtimeFormatter
anonymous_statements:
  - match: |
      initializeRuntime();
"#,
    );
    (modules, source)
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
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["members_scanned"], 3, "{parsed}");
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
fn synthesize_selectors_apply_single_member_preserves_unrelated_yaml_text() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_single_member_text_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/bootstrap:FormatValue",
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

    let rewritten = fs::read_to_string(modules.join("app/bootstrap.yaml")).unwrap();
    assert_eq!(
        rewritten,
        r#"# merged from: legacy/bootstrap.yaml
comment: |
  Keep this module grouped with startup.
members:
  # keep the neighboring member untouched
  - name: UntouchedBinding
    selector:
      binding:
        name: untouchedBinding
  # keep the selected member's surrounding YAML comment
  - name: FormatValue
    comment: Keep readable comment field.
    # keep the pre-selector YAML comment
    selector:
      source_match:
        identifiers: alpha_all
        target_binding: FormatValue
        match: |-
          function FormatValue(value) {
            return value.trim().toUpperCase();
          }
anonymous_statements:
  - match: |
      initializeRuntime();
"#
    );
}

#[test]
fn synthesize_selectors_dry_run_reports_grouped_unique_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_fixture(dir.path());

    let out = run_codemod(
        &modules,
        &[
            "--rewrite",
            "name-binding-to-source-match",
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/config:PrimaryConfig",
            "--item",
            "app/config:SecondaryConfig",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(
        parsed["rewrite"], "name_binding_to_source_match",
        "{parsed}"
    );
    assert_eq!(parsed["action"], "dry_run", "{parsed}");
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["members_scanned"], 2, "{parsed}");
    assert_eq!(parsed["summary"]["name_binding_members"], 2, "{parsed}");
    assert_eq!(parsed["summary"]["synthesized_groups"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 2, "{parsed}");
    let candidates = parsed["candidates"].as_array().unwrap();
    assert_eq!(candidates.len(), 2, "{parsed}");
    for candidate in candidates {
        assert_eq!(candidate["action"], "would_change", "{candidate}");
        assert_eq!(candidate["group_id"], 0, "{candidate}");
        assert_eq!(candidate["matched_body_index"], 0, "{candidate}");
        assert_eq!(candidate["candidate_count"], 1, "{candidate}");
        assert!(
            candidate["rewritten_holes"]
                .as_array()
                .unwrap()
                .iter()
                .any(|hole| hole == "DECLARATORS_BEFORE"),
            "{candidate}"
        );
    }
}

#[test]
fn synthesize_selectors_command_alias_uses_name_binding_rewrite() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/config:FormatValue",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(
        parsed["rewrite"], "name_binding_to_source_match",
        "{parsed}"
    );
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["candidates"][0]["target_binding"], "FormatValue");
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["members_scanned"], 3, "{parsed}");
}

#[test]
fn synthesize_selectors_module_prefix_limits_file_scan() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--module-prefix",
            "app",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["members_scanned"], 3, "{parsed}");
    assert_eq!(parsed["summary"]["name_binding_members"], 3, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 3, "{parsed}");
    assert!(
        parsed["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .all(|candidate| candidate["module"] == "app/config"),
        "{parsed}"
    );
}

#[test]
fn synthesize_selectors_apply_groups_multideclarator_and_preserves_comments() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_fixture(dir.path());

    let out = run_codemod(
        &modules,
        &[
            "--rewrite",
            "name-binding-to-source-match",
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/config:PrimaryConfig",
            "--item",
            "app/config:SecondaryConfig",
            "--item",
            "app/config:FormatValue",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 3, "{parsed}");

    let module = modules.join("app/config.yaml");
    let doc: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(module).unwrap()).unwrap();
    let members = doc["members"].as_sequence().unwrap();
    assert_eq!(members.len(), 1, "{doc:?}");
    assert_eq!(members[0]["name"], "FormatValue");
    assert_eq!(
        members[0]["selector"]["source_match"]["target_binding"],
        "FormatValue"
    );
    assert!(
        members[0]["selector"]["source_match"]["match"]
            .as_str()
            .unwrap()
            .contains("function FormatValue")
    );

    let groups = doc["binding_groups"].as_sequence().unwrap();
    assert_eq!(groups.len(), 1, "{doc:?}");
    let group = &groups[0];
    let match_source = group["source_match"]["match"].as_str().unwrap();
    assert!(
        match_source.contains("DECLARATORS_BEFORE"),
        "{match_source}"
    );
    assert!(
        match_source.contains("PrimaryConfig = buildConfig"),
        "{match_source}"
    );
    assert!(
        match_source.contains("SecondaryConfig = buildConfig"),
        "{match_source}"
    );
    assert_eq!(group["exports"]["PrimaryConfig"], "PrimaryConfig");
    assert_eq!(group["exports"]["SecondaryConfig"], "SecondaryConfig");
    assert_eq!(group["comments"]["PrimaryConfig"], "Primary config comment");
    assert_eq!(
        group["comments"]["SecondaryConfig"],
        "Secondary config comment"
    );
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
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
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
fn apply_single_target_binding_preserves_comments_and_local_text() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let target = commented_single_target_fixture(&modules);

    let out = run_codemod(
        &modules,
        &[
            "--rewrite",
            "single-target-binding",
            "--module",
            "app/bootstrap",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["files_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["modules_scanned"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["skipped_candidates"], 1, "{parsed}");

    let rewritten = fs::read_to_string(&target).unwrap();
    assert_eq!(
        rewritten,
        r#"# module-level note must survive
# another note that used to be lost by serde rewrites
comment: |
  Keep this module grouped with startup.
members:
  # keep the member comment
  - name: StartupFactory
    selector:
      source_match:
        identifiers: alpha_all
        # keep the selector comment
        target_binding: startupFactory
        match: |
          const startupFactory = createStartupFactory();
  - name: AlreadyAnchored
    selector:
      source_match:
        target_binding: alreadyAnchored
        match: |
          const alreadyAnchored = createAlreadyAnchored();

anonymous_statements:
  - match: |
      initializeRuntime();
"#
    );
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
