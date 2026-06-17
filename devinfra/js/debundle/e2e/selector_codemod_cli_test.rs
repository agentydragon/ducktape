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

fn assert_no_trailing_whitespace(text: &str) {
    assert!(
        text.lines()
            .all(|line| !line.ends_with(' ') && !line.ends_with('\t')),
        "rewritten YAML contains trailing whitespace:\n{text}"
    );
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
    // Keep `concat!` with explicit `\n`: lines carry intentional trailing
    // whitespace/tabs (exercising selector whitespace normalization) that a raw
    // string or `.js` include would lose to the trim-trailing-whitespace hook.
    write(
        &source,
        concat!(
            "const beforeConfig = helper(\"before\"),\n",
            "  runtimePrimary = buildConfig({ stable: \"primary\", generated: \"ignore-a\" }),  \n",
            "  middleConfig = helper(\"middle\"),\n",
            "  runtimeSecondary = buildConfig({ stable: \"secondary\", generated: \"ignore-b\" }),\t\n",
            "  afterConfig = helper(\"after\");\n",
            "function runtimeFormatter(value) {\n",
            "  return value.trim().toUpperCase();\n",
            "}\n",
            "function helper(value) {\n",
            "  return value;\n",
            "}\n",
            "function buildConfig(value) {\n",
            "  return value;\n",
            "}\n",
            "console.log(runtimePrimary, runtimeSecondary, runtimeFormatter(\" ok \"));\n",
            "export { runtimePrimary, runtimeSecondary, runtimeFormatter };\n",
        ),
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
    // Keep `concat!` with explicit `\n`: the body lines carry intentional
    // trailing whitespace/tabs that a raw string or `.js` include would lose to
    // the trim-trailing-whitespace hook.
    write(
        &source,
        concat!(
            "function runtimeFormatter(value) {\n",
            "  const trimmed = value.trim();  \n",
            "  \t\n",
            "  return trimmed.toUpperCase(); \t\n",
            "}\n",
            "function untouchedBinding() {\n",
            "  return \"still name-only\";\n",
            "}\n",
            "export { runtimeFormatter, untouchedBinding };\n",
        ),
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

fn synthesis_function_minimization_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"function runtimeFormatter(value) {
  const normalized = value.trim().toUpperCase();
  if (normalized.length > 8) {
    return normalized.slice(0, 8);
  }
  return normalized.padEnd(8, "_");
}
const unrelatedValue = "kept";
console.log(runtimeFormatter(" ok "), unrelatedValue);
export { runtimeFormatter };
"#,
    );
    let modules = root.join("modules");
    write(
        &modules.join("app/format.yaml"),
        r#"members:
  - name: FormatValue
    selector:
      binding:
        name: runtimeFormatter
"#,
    );
    (modules, source)
}

fn synthesis_object_anchor_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"const selectedConfig = buildConfig({
  stableKey: expensiveValue("selected", { noisy: [1, 2, 3] }),
  generatedPayload: createPayload(() => Math.random()),
  extraNested: { generated: computeNested("selected"), count: 3 },
});
const otherConfig = buildConfig({
  otherKey: expensiveValue("selected", { noisy: [1, 2, 3] }),
  generatedPayload: createPayload(() => Math.random()),
  extraNested: { generated: computeNested("other"), count: 3 },
});
function buildConfig(value) { return value; }
function expensiveValue(value) { return value; }
function createPayload(value) { return value; }
function computeNested(value) { return value; }
console.log(selectedConfig.stableKey, otherConfig.otherKey);
export { selectedConfig };
"#,
    );
    let modules = root.join("modules");
    write(
        &modules.join("app/config.yaml"),
        r#"members:
  - name: SelectedConfig
    selector:
      binding:
        name: selectedConfig
"#,
    );
    (modules, source)
}

fn synthesis_group_anchor_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"const selectedA = makeThing({
  stableA: computeValue("a", { noisy: [1, 2, 3] }),
  volatileA: makeVolatile(() => Math.random()),
}),
  skipped = makeThing({ skippedKey: computeValue("skip") }),
  selectedB = makeThing({
    stableB: computeValue("b", { noisy: [4, 5, 6] }),
    volatileB: makeVolatile(() => Date.now()),
  });
const otherA = makeThing({ otherA: computeValue("a") }),
  otherB = makeThing({ otherB: computeValue("b") });
function makeThing(value) { return value; }
function computeValue(value) { return value; }
function makeVolatile(value) { return value; }
console.log(selectedA.stableA, selectedB.stableB, otherA.otherA, otherB.otherB);
export { selectedA, selectedB };
"#,
    );
    let modules = root.join("modules");
    write(
        &modules.join("app/group.yaml"),
        r#"members:
  - name: SelectedA
    selector:
      binding:
        name: selectedA
  - name: SelectedB
    selector:
      binding:
        name: selectedB
"#,
    );
    (modules, source)
}

fn synthesis_branch_and_bound_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    write(
        &source,
        r#"const selectedConfig = makeConfig({
  alphaKey: computeValue("selected-alpha"),
  betaKey: computeValue("selected-beta"),
  gammaKey: computeValue("selected-gamma"),
  deltaKey: computeValue("selected-delta"),
  epsilonKey: computeValue("selected-epsilon"),
});
const competitorOne = makeConfig({
  alphaKey: computeValue("one-alpha"),
  betaKey: computeValue("one-beta"),
  deltaKey: computeValue("one-delta"),
});
const competitorTwo = makeConfig({
  alphaKey: computeValue("two-alpha"),
  gammaKey: computeValue("two-gamma"),
  epsilonKey: computeValue("two-epsilon"),
});
const competitorThree = makeConfig({
  betaKey: computeValue("three-beta"),
});
const competitorFour = makeConfig({
  gammaKey: computeValue("four-gamma"),
});
const competitorAlphaBeta = makeConfig({ alphaKey: 1, betaKey: 1 });
const competitorAlphaGamma = makeConfig({ alphaKey: 1, gammaKey: 1 });
const competitorAlphaDelta = makeConfig({ alphaKey: 1, deltaKey: 1 });
const competitorAlphaEpsilon = makeConfig({ alphaKey: 1, epsilonKey: 1 });
const competitorBetaDelta = makeConfig({ betaKey: 1, deltaKey: 1 });
const competitorBetaEpsilon = makeConfig({ betaKey: 1, epsilonKey: 1 });
const competitorGammaDelta = makeConfig({ gammaKey: 1, deltaKey: 1 });
const competitorGammaEpsilon = makeConfig({ gammaKey: 1, epsilonKey: 1 });
const competitorDeltaEpsilon = makeConfig({ deltaKey: 1, epsilonKey: 1 });
function makeConfig(value) { return value; }
function computeValue(value) { return value; }
console.log(selectedConfig, competitorOne, competitorTwo, competitorThree, competitorFour);
export { selectedConfig };
"#,
    );
    let modules = root.join("modules");
    write(
        &modules.join("app/search.yaml"),
        r#"members:
  - name: SelectedConfig
    selector:
      binding:
        name: selectedConfig
"#,
    );
    (modules, source)
}

fn synthesis_regex_literal_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let source = root.join("chunks/app.js");
    // Several sibling var bindings whose values are string literals sharing a
    // stable per-binding prefix and a rebuild-volatile hex suffix. The target's
    // stable prefix (`primary-chunk-`) already discriminates it from the
    // siblings (`secondary-chunk-`, `vendor-chunk-`), so the minimizer can pin
    // the stable structure with a regex and wildcard the volatile hash.
    write(
        &source,
        r#"const selectedAsset = loadChunk("primary-chunk-a1b2c3d4");
const secondaryAsset = loadChunk("secondary-chunk-99887766");
const vendorAsset = loadChunk("vendor-chunk-deadbeef");
function loadChunk(value) { return value; }
console.log(selectedAsset, secondaryAsset, vendorAsset);
export { selectedAsset };
"#,
    );
    let modules = root.join("modules");
    write(
        &modules.join("app/assets.yaml"),
        r#"members:
  - name: SelectedAsset
    selector:
      binding:
        name: selectedAsset
"#,
    );
    (modules, source)
}

#[test]
fn synthesize_selectors_emits_regex_literal_anchor_for_volatile_suffix() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_regex_literal_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/assets:SelectedAsset",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    // Gate 1: the synthesized selector resolves uniquely to the intended binding.
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["candidates"][0]["candidate_count"], 1, "{parsed}");

    let rewritten = fs::read_to_string(modules.join("app/assets.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let match_source = doc["members"][0]["selector"]["source_match"]["match"]
        .as_str()
        .unwrap();

    // The minimizer pinned the volatile literal with a regex predicate rather
    // than the exact spelling.
    assert!(
        match_source.contains("STR_LITERAL_MATCHING_RE"),
        "expected a regex-literal anchor:\n{match_source}"
    );
    assert!(
        !match_source.contains("a1b2c3d4"),
        "the volatile suffix must not be pinned exactly:\n{match_source}"
    );

    // Extract the emitted pattern and assert its *semantics*: it matches the
    // target literal (and rebuild variants of the same prefix) while excluding
    // every sibling literal. This is the discrimination property, not a string
    // equality of the rendered selector.
    let pattern = extract_regex_literal_pattern(match_source);
    let re = regex::Regex::new(&pattern).expect("emitted pattern must be a valid regex");
    assert!(
        re.is_match("primary-chunk-a1b2c3d4"),
        "pattern must match the target literal: {pattern}"
    );
    assert!(
        re.is_match("primary-chunk-00000000"),
        "pattern must survive a rebuild of the volatile suffix: {pattern}"
    );
    assert!(
        !re.is_match("secondary-chunk-99887766"),
        "pattern must exclude the secondary sibling: {pattern}"
    );
    assert!(
        !re.is_match("vendor-chunk-deadbeef"),
        "pattern must exclude the vendor sibling: {pattern}"
    );
}

/// Pull the pattern string out of a `STR_LITERAL_MATCHING_RE("<pattern>")`
/// occurrence in a rendered selector.
fn extract_regex_literal_pattern(match_source: &str) -> String {
    let needle = "STR_LITERAL_MATCHING_RE(\"";
    let start = match_source
        .find(needle)
        .map(|idx| idx + needle.len())
        .unwrap_or_else(|| panic!("no regex predicate in:\n{match_source}"));
    let rest = &match_source[start..];
    let end = rest
        .find('"')
        .unwrap_or_else(|| panic!("unterminated regex pattern in:\n{match_source}"));
    // The pattern lives inside a JS string literal in the rendered selector, so
    // every backslash from `regex::escape` is doubled in the source text. The
    // matcher's parser un-escapes it before compiling; mirror that here so the
    // test compiles the same pattern the matcher does.
    rest[..end].replace("\\\\", "\\")
}

#[test]
fn synthesize_selectors_full_ast_fallback_flag_is_accepted() {
    // The minimizer almost always finds a sparse selector for synthesizable
    // declarations, so the full-AST fallback path is hard to trigger from a
    // small fixture (the gating itself is unit-tested in selector_codemod.rs).
    // Here we just confirm the `--full-ast-fallback` flag is wired into the CLI
    // and does not change the result when minimization succeeds.
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_function_minimization_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/format:FormatValue",
            "--full-ast-fallback",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["summary"]["skipped_candidates"], 0, "{parsed}");
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
fn synthesize_selectors_apply_single_member_preserves_unrelated_yaml_structure() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_single_member_text_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/bootstrap:FormatValue",
            "--no-minimize",
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

    // The apply path now loads, mutates, and dumps the whole document with
    // serde_yaml, so it intentionally does NOT preserve `#` comments or author
    // formatting. Assert the resulting structure/content rather than bytes:
    // explicit `comment:`/value fields survive, the unrelated member and
    // `anonymous_statements` are untouched, and only the selected member is
    // rewritten to a `source_match`.
    let rewritten = fs::read_to_string(modules.join("app/bootstrap.yaml")).unwrap();
    assert_no_trailing_whitespace(&rewritten);
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    assert_eq!(
        doc["comment"].as_str().unwrap().trim(),
        "Keep this module grouped with startup."
    );
    let members = doc["members"].as_sequence().unwrap();
    assert_eq!(members.len(), 2, "{doc:?}");

    // Unrelated member is byte-for-byte the same shape: still a name-binding.
    assert_eq!(members[0]["name"], "UntouchedBinding");
    assert_eq!(
        members[0]["selector"]["binding"]["name"],
        "untouchedBinding"
    );

    // Selected member is rewritten to a source_match, keeping its `comment:`
    // value field (a real YAML key, not a `#` comment).
    assert_eq!(members[1]["name"], "FormatValue");
    assert_eq!(members[1]["comment"], "Keep readable comment field.");
    let source_match = &members[1]["selector"]["source_match"];
    assert_eq!(source_match["identifiers"], "alpha_all");
    assert_eq!(source_match["target_binding"], "FormatValue");
    let match_source = source_match["match"].as_str().unwrap();
    assert!(
        match_source.contains("function FormatValue(value)"),
        "{match_source}"
    );
    assert!(
        match_source.contains("trimmed.toUpperCase()"),
        "{match_source}"
    );

    assert_eq!(
        doc["anonymous_statements"][0]["match"]
            .as_str()
            .unwrap()
            .trim(),
        "initializeRuntime();"
    );
}

#[test]
fn synthesize_selectors_minimizes_function_body_by_default() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_function_minimization_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/format:FormatValue",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert!(
        parsed["candidates"][0]["rewritten_holes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|hole| hole == "STMT_LIST"),
        "{parsed}"
    );

    let rewritten = fs::read_to_string(modules.join("app/format.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let match_source = doc["members"][0]["selector"]["source_match"]["match"]
        .as_str()
        .unwrap();
    assert!(
        match_source.contains("function FormatValue(ANYTHING)"),
        "{match_source}"
    );
    assert!(match_source.contains("STMT_LIST;"), "{match_source}");
    // The body is holed down to a single anchored statement, not copied whole:
    // `runtimeFormatter` is the only function in the chunk, so the bare scaffold
    // alone would resolve — but the robustness-anchor policy keeps one
    // discriminating value anchor (rather than the degenerate empty body) while
    // still dropping the irrelevant statements (`trim().toUpperCase()`, the
    // length `if`, `slice`) to `STMT_LIST`.
    assert!(
        !match_source.contains("toUpperCase") && !match_source.contains("slice"),
        "irrelevant function body statements should not be copied:\n{match_source}"
    );
}

#[test]
fn synthesize_selectors_keeps_object_key_anchor_when_erasing_it_is_ambiguous() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_object_anchor_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/config:SelectedConfig",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert!(
        parsed["candidates"][0]["rewritten_holes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|hole| hole == "ANYTHING"),
        "{parsed}"
    );

    let rewritten = fs::read_to_string(modules.join("app/config.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let match_source = doc["members"][0]["selector"]["source_match"]["match"]
        .as_str()
        .unwrap();
    // Re-baselined for the unified keep-shallow policy: the single-target var now
    // routes through the group path, which seeds direct shallow literals (here
    // `count: 3`) and escalates to the whole structural (object-key) tier, so the
    // selector over-pins `generatedPayload`/`extraNested` instead of the
    // exact-minimum `stableKey` alone. The load-bearing invariants still hold: the
    // discriminating `stableKey` anchor is kept, unstable values are wildcarded,
    // and the selector never falls back to the ambiguous sibling key `otherKey`.
    assert!(
        match_source.contains("stableKey:"),
        "stable key anchor should remain:\n{match_source}"
    );
    assert!(
        match_source.contains("ANYTHING"),
        "unstable values should be wildcarded:\n{match_source}"
    );
    assert!(
        !match_source.contains("otherKey"),
        "selector must not fall back to the ambiguous sibling key:\n{match_source}"
    );
}

#[test]
fn synthesize_selectors_minimizes_binding_group_to_needed_slot_anchors() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_group_anchor_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/group:SelectedA",
            "--item",
            "app/group:SelectedB",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 2, "{parsed}");
    for candidate in parsed["candidates"].as_array().unwrap() {
        assert!(
            candidate["rewritten_holes"]
                .as_array()
                .unwrap()
                .iter()
                .any(|hole| hole == "ANYTHING"),
            "{candidate}"
        );
        // Re-baselined: the unified group path reports holes via the canonical
        // `holes_present` extractor, which records the bare `DECLARATORS` keyword
        // (the match source below still emits a `DECLARATORS` gap declarator).
        assert!(
            candidate["rewritten_holes"]
                .as_array()
                .unwrap()
                .iter()
                .any(|hole| hole == "DECLARATORS"),
            "{candidate}"
        );
    }

    let rewritten = fs::read_to_string(modules.join("app/group.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    assert_eq!(doc["members"].as_sequence().unwrap().len(), 0, "{doc:?}");
    let groups = doc["binding_groups"].as_sequence().unwrap();
    assert_eq!(groups.len(), 1, "{doc:?}");
    let match_source = groups[0]["source_match"]["match"].as_str().unwrap();
    // Re-baselined for the unified keep-shallow policy: with no direct shallow
    // literal in either target slot, the group escalates to the whole structural
    // (object-key) tier, so both slots keep their `stableX`/`volatileX` keys
    // rather than the exact-minimum `stableX` alone. The skipped middle
    // declarator still collapses to a `DECLARATORS` gap and each slot
    // keeps its discriminating stable key.
    assert!(
        match_source.contains("stableA:"),
        "slot A stable key should remain:\n{match_source}"
    );
    assert!(
        match_source.contains("stableB:"),
        "slot B stable key should remain to distinguish it from the skipped declarator:\n{match_source}"
    );
    assert!(
        match_source.contains("DECLARATORS"),
        "irrelevant middle declarator should become a gap:\n{match_source}"
    );
    assert!(
        !match_source.contains("skipped"),
        "the skipped middle declarator binding must not be copied:\n{match_source}"
    );
}

// After the var read-off migration, a single-target var initialized by an
// object-bearing call (`makeConfig({…})`) reads its minimal anchor off the shape
// index. Each entry's value (`computeValue("selected-beta")`) carries a globally
// unique string, so the read-off pins ONE discriminating `key: value` and holes
// every sibling key to the object-property run hole (emitted as `ANYTHING`) —
// sparser than the keep-shallow "keep all
// keys" output and sparser than the {betaKey, gammaKey} key-set the B&B set-cover
// would compute (a unique value beats a multi-key presence cover). It still
// resolves uniquely to the intended binding. The min-cover guarantee still backs
// function bodies via `minimize_via_retention` → `cover_competitors` →
// `min_set_cover`.
#[test]
fn synthesize_selectors_var_object_keys_resolve_uniquely() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, source) = synthesis_branch_and_bound_fixture(dir.path());

    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            "app/search:SelectedConfig",
            "--apply",
            "--format",
            "json",
        ],
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["summary"]["changed_candidates"], 1, "{parsed}");
    assert_eq!(parsed["candidates"][0]["candidate_count"], 1, "{parsed}");

    let rewritten = fs::read_to_string(modules.join("app/search.yaml")).unwrap();
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    let match_source = doc["members"][0]["selector"]["source_match"]["match"]
        .as_str()
        .unwrap();
    // One discriminating `key: value` with a globally-unique string value is the
    // minimal read-off anchor; the rest collapse to the object-property run hole,
    // emitted as `ANYTHING` (the run-absorber form the minimizer now prefers in
    // object-property position).
    assert!(
        match_source.contains("selected-") && match_source.matches("ANYTHING").count() >= 1,
        "a single discriminating value anchor should remain, rest holed:\n{match_source}"
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
        // Re-baselined: the unified group path reports holes via the canonical
        // `holes_present` extractor, which records the bare `DECLARATORS` keyword
        // (the match source still emits a leading `DECLARATORS` gap declarator).
        assert!(
            candidate["rewritten_holes"]
                .as_array()
                .unwrap()
                .iter()
                .any(|hole| hole == "DECLARATORS"),
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
    let rewritten = fs::read_to_string(module).unwrap();
    assert_no_trailing_whitespace(&rewritten);
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
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
    assert!(match_source.contains("DECLARATORS"), "{match_source}");
    // The bare `buildConfig` callee holes to ANYTHING (a minified name the matcher
    // alpha-wildcards); each slot is still kept as its own named declarator.
    assert!(
        match_source.contains("PrimaryConfig = ANYTHING("),
        "{match_source}"
    );
    assert!(
        match_source.contains("SecondaryConfig = ANYTHING("),
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
fn apply_single_target_binding_rewrites_structure_via_serde() {
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

    // The apply path is now load-mutate-dump via serde_yaml: `#` comments and
    // author formatting are intentionally not preserved (binding/module notes
    // live in explicit `comment:` value fields instead). Assert structure and
    // content rather than exact bytes.
    let rewritten = fs::read_to_string(&target).unwrap();
    assert_no_trailing_whitespace(&rewritten);
    let doc: serde_yaml::Value = serde_yaml::from_str(&rewritten).unwrap();
    assert_eq!(
        doc["comment"].as_str().unwrap().trim(),
        "Keep this module grouped with startup."
    );
    let members = doc["members"].as_sequence().unwrap();
    assert_eq!(members.len(), 2, "{doc:?}");

    // First member: target_binding inserted ahead of the existing match.
    assert_eq!(members[0]["name"], "StartupFactory");
    let first = &members[0]["selector"]["source_match"];
    assert_eq!(first["identifiers"], "alpha_all");
    assert_eq!(first["target_binding"], "startupFactory");
    assert_eq!(
        first["match"].as_str().unwrap().trim(),
        "const startupFactory = createStartupFactory();"
    );

    // Second member already had a target_binding, so it is skipped (unchanged).
    assert_eq!(members[1]["name"], "AlreadyAnchored");
    let second = &members[1]["selector"]["source_match"];
    assert_eq!(second["target_binding"], "alreadyAnchored");
    assert_eq!(
        second["match"].as_str().unwrap().trim(),
        "const alreadyAnchored = createAlreadyAnchored();"
    );

    assert_eq!(
        doc["anonymous_statements"][0]["match"]
            .as_str()
            .unwrap()
            .trim(),
        "initializeRuntime();"
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
