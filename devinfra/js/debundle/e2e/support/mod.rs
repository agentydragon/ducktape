//! Black-box harness for the `debundle` binary.
//!
//! Drives the CLI through a YAML spec and asserts on the emitted file
//! tree by reading files and re-running them under `node`.

mod ast_assertions;
mod fixture_spec;
mod graph_fixture;
pub use graph_fixture::GraphFixture;

pub use ast_assertions::{
    VariableDeclarationKind, VariableInitializerKind, assert_export_named_specifiers,
    assert_module_variable_declarators, parse_module,
};
use fixture_spec::build_spec;
pub use fixture_spec::{
    BindingGroup, ChunkExportPurityBuilder, FixtureOpts, LogicalModuleEntry, Member,
    logical_module, logical_module_with_anon, logical_module_with_anon_alpha,
    logical_module_with_anon_comment, logical_module_with_binding_groups,
    logical_module_with_comment, mixed_selector_failure_fixture, unassigned_mode_catchall_file,
    unassigned_mode_inline, unassigned_mode_mini_factors,
};
pub use fixture_spec::{ChunkRenameEntry, chunk_rename, chunk_rename_with_purity, chunk_renames};

use analysis::OwnerGraphReport;
use artifact::PackageManifest;
use runfiles::{Runfiles, rlocation};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;

/// Re-exported so test files can reference the spec enums behind
/// `Member::with_purity` / `Member::with_effect` without a direct `spec` dep.
pub use spec::{MemberEffect, MemberPurity};

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use tempfile::TempDir;

const DEBUNDLER_RLOCATION: &str = "_main/devinfra/js/debundle/debundle";
const NODE_RLOCATION: &str = "nodejs_linux_amd64/bin/node";

static MODULE_EXPORT_PROBE_COUNTER: AtomicUsize = AtomicUsize::new(0);
static GENERATED_MODULE_SCRIPT_COUNTER: AtomicUsize = AtomicUsize::new(0);

pub struct Fixture {
    pub chunk_id: String,
    pub entry_path: PathBuf,
    pub out_root: PathBuf,
    pub report_root: PathBuf,
    /// The debundler's stderr from the successful run, for asserting
    /// on warnings/notices (e.g. admission-override notices).
    pub stderr: String,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

pub struct RejectedFixture {
    pub chunk_id: String,
    pub stderr: String,
    pub report_root: PathBuf,
    /// The `write_js_tree` output root, exposed so dry-run callers can
    /// assert the no-output contract (only `reports/` may exist).
    pub out_root: PathBuf,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

impl Fixture {
    pub fn owner_graph(&self) -> OwnerGraphReport {
        read_owner_graph(&self.report_root, &self.chunk_id)
    }
}

impl RejectedFixture {
    pub fn owner_graph(&self) -> OwnerGraphReport {
        read_owner_graph(&self.report_root, &self.chunk_id)
    }

    /// `cycles.json`: one entry per blocking SCC, with its `modules` and `cut`.
    pub fn cycles(&self) -> Vec<Value> {
        read_json(&self.report_root.join(&self.chunk_id).join("cycles.json"))
    }

    /// `atomic_unit_conflicts.json`: one entry per atomic unit the spec splits, with its
    /// `claims` (owner, binding names, module) and `causes`.
    pub fn atomic_unit_conflicts(&self) -> Vec<Value> {
        read_json(
            &self
                .report_root
                .join(&self.chunk_id)
                .join("atomic_unit_conflicts.json"),
        )
    }

    /// The `outcomes` of `selector_diagnostics.json`.
    pub fn selector_outcomes(&self) -> Vec<Value> {
        read_chunk_selector_outcomes(&self.report_root, &self.chunk_id)
    }
}

fn read_owner_graph(report_root: &Path, chunk_id: &str) -> OwnerGraphReport {
    read_json(&report_root.join(chunk_id).join("owner_graph.json"))
}

pub struct DryRunFixture {
    pub stderr: String,
    pub report_root: PathBuf,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

pub fn run_fixture(opts: FixtureOpts<'_>) -> Fixture {
    let setup = prepare_fixture(&opts);

    let result = spawn_transform(&setup.spec_path);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let app_root = setup.out_root.join("app");

    // Mirror `extra_files` into app_root after the transform runs, so
    // re-imports emitted by the materializer can resolve through
    // their relative paths under the runtime app tree.
    for (rel_path, content) in opts.extra_files {
        write_text_file(&app_root.join(rel_path), content);
    }

    let entry_path = app_root
        .join(opts.chunk_id.split('/').collect::<PathBuf>())
        .join("entry.js");
    Fixture {
        chunk_id: opts.chunk_id.to_string(),
        entry_path,
        out_root: app_root,
        report_root: setup.report_root,
        stderr: result.stderr,
        _root: setup.root,
    }
}

/// Runs `opts` and asserts the realizability gate rejected it: returns the blocking SCC of
/// `cycles.json` whose modules include every one of `modules`.
pub fn expect_cycle_rejection(opts: FixtureOpts<'_>, modules: &[&str]) -> Value {
    let cycles = run_rejection_fixture(opts).cycles();
    cycles
        .iter()
        .find(|scc| {
            let members = scc["modules"].as_array().expect("SCC modules");
            modules.iter().all(|module| {
                members
                    .iter()
                    .any(|member| member.as_str() == Some(*module))
            })
        })
        .cloned()
        .unwrap_or_else(|| panic!("no blocking SCC contains {modules:?}: {cycles:#?}"))
}

/// [`expect_cycle_rejection`] for a cycle whose cut has a side-effect ordering (`sequenced`) edge.
pub fn expect_sequenced_cycle_rejection(opts: FixtureOpts<'_>, modules: &[&str]) {
    let scc = expect_cycle_rejection(opts, modules);
    assert!(
        scc["cut"]
            .as_array()
            .expect("SCC cut")
            .iter()
            .any(|edge| edge["kind"] == "sequenced"),
        "no sequenced edge in the cut: {scc:#}"
    );
}

/// Runs `opts` and asserts the atomic-unit check rejected it: some conflict of
/// `atomic_unit_conflicts.json` has claims in every one of `modules` and every one of `causes`.
pub fn expect_atomic_conflict_rejection(opts: FixtureOpts<'_>, modules: &[&str], causes: &[&str]) {
    let conflicts = run_rejection_fixture(opts).atomic_unit_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            let conflict_causes = conflict["causes"].as_array().expect("conflict causes");
            modules.iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            }) && causes.iter().all(|cause| {
                conflict_causes
                    .iter()
                    .any(|candidate| candidate.as_str() == Some(*cause))
            })
        }),
        "no atomic-unit conflict claimed by {modules:?} with causes {causes:?}: {conflicts:#?}"
    );
}

/// Runs `opts` and asserts the selector pass rejected it: returns the outcome of `kind` placed
/// in `logical_module`.
pub fn expect_selector_outcome(opts: FixtureOpts<'_>, kind: &str, logical_module: &str) -> Value {
    let outcomes = run_rejection_fixture(opts).selector_outcomes();
    find_outcome_in_module(&outcomes, kind, logical_module).clone()
}

/// Runs `opts` and asserts it is rejected with stderr naming every one of `tokens`: identifiers,
/// paths or code tokens, matched case-sensitively. For a rejection with no report under the
/// report root, or a rendering whose identifiers are under test; otherwise assert on the report.
pub fn expect_rejection_containing_all(opts: FixtureOpts<'_>, tokens: &[&str]) {
    let rejected = run_rejection_fixture(opts);
    let missing: Vec<&str> = tokens
        .iter()
        .copied()
        .filter(|token| !rejected.stderr.contains(token))
        .collect();
    assert!(
        missing.is_empty(),
        "stderr missing {missing:?}\nstderr:\n{}",
        rejected.stderr,
    );
}

pub fn run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &[])
}

/// Like [`run_rejection_fixture`] but with `debundle run --dry-run`.
/// Dry-run skips all emitted-JS and accept-path report writes but
/// still materializes rejection evidence (`owner_graph.json` +
/// `cycles.json` / `atomic_unit_conflicts.json`) under the standard
/// per-chunk report layout.
pub fn run_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run"])
}

/// Like [`run_dry_run_rejection_fixture`] but opts out of the default
/// keep-going diagnostics and stops at the first supported failure.
fn run_fail_fast_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run", "--fail-fast"])
}

/// Runs `fixture` keep-going and with `--fail-fast`, both dry. Keep-going
/// reports every outcome, at least two; fail-fast fails with exactly one of
/// those lines, of `kind`, and no other. Returns that line.
pub fn assert_fail_fast_stops_at_first_outcome<'a>(
    fixture: impl Fn() -> FixtureOpts<'a>,
    kind: &str,
) -> String {
    let keep_going = run_dry_run_rejection_fixture(fixture());
    assert_stderr_lists_every_outcome(
        &keep_going.stderr,
        &read_selector_outcomes(&keep_going.report_root),
    );
    let reported = reported_outcome_lines(&keep_going.stderr);
    assert!(
        reported.len() >= 2,
        "the fixture needs outcomes after the first: {reported:#?}"
    );

    let fail_fast = run_fail_fast_dry_run_rejection_fixture(fixture());
    let stopped_at = reported
        .iter()
        .filter(|line| fail_fast.stderr.contains(line.as_str()))
        .collect::<Vec<_>>();
    let [line] = stopped_at[..] else {
        panic!(
            "fail-fast must report exactly one outcome, got {stopped_at:#?}\nstderr:\n{}",
            fail_fast.stderr
        );
    };
    assert!(
        line.starts_with(&format!("[{kind}] ")),
        "fail-fast stopped at {line:?}, expected a {kind} outcome"
    );
    line.clone()
}

/// Run `debundle run --dry-run` over `opts` and assert it succeeds. The report
/// root holds whatever the pass still writes on success, such as selector
/// warnings in `selector_diagnostics.json`.
pub fn run_dry_run_fixture(opts: FixtureOpts<'_>) -> DryRunFixture {
    let setup = prepare_fixture(&opts);
    let result = spawn_transform_with_args(&setup.spec_path, &["--dry-run"]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    DryRunFixture {
        stderr: result.stderr,
        report_root: setup.report_root,
        _root: setup.root,
    }
}

fn run_rejection_fixture_with_args(opts: FixtureOpts<'_>, extra_args: &[&str]) -> RejectedFixture {
    let setup = prepare_fixture(&opts);

    let result = spawn_transform_with_args(&setup.spec_path, extra_args);
    assert!(
        !result.status.success(),
        "expected spec to be rejected\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    RejectedFixture {
        chunk_id: opts.chunk_id.to_string(),
        stderr: result.stderr,
        report_root: setup.report_root,
        out_root: setup.out_root,
        _root: setup.root,
    }
}

/// A written transform spec ready to feed to a `debundle` subcommand other
/// than `run` (e.g. `spec validate`). The held [`TempDir`] keeps the spec,
/// source snapshot, and js-list alive for the duration of the test.
pub struct ValidateFixture {
    pub spec_path: PathBuf,
    _root: TempDir,
}

/// Materialize `opts` into an on-disk transform spec without running the
/// pipeline. Lets a CLI test point `debundle spec validate --spec <path>` at
/// exactly the same fixture shape the keep-going materialize tests build.
pub fn write_validate_fixture_spec(opts: FixtureOpts<'_>) -> ValidateFixture {
    let setup = prepare_fixture(&opts);
    ValidateFixture {
        spec_path: setup.spec_path,
        _root: setup.root,
    }
}

/// `debundle spec validate --spec --format json` over `opts`, parsed; panics on a
/// non-zero exit.
pub fn validate_json(opts: FixtureOpts<'_>) -> Value {
    let fixture = write_validate_fixture_spec(opts);
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );
    serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
}

/// The `outcomes` array of a selector-outcome report.
pub fn outcomes(report: &Value) -> &[Value] {
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
}

/// Run `debundle spec validate --spec <path> <extra_args>` and return its
/// captured stdio + exit status.
pub fn run_spec_validate(spec_path: &Path, extra_args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    command_result(
        Command::new(&bin)
            .args(["spec", "validate", "--spec"])
            .arg(spec_path)
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

pub fn assert_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.entry_path, expected_stdout, "");
}

/// Run `node --check` against every emitted JavaScript file in a fixture.
pub fn assert_all_emitted_js_checks(fixture: &Fixture) {
    fn visit(dir: &Path, files: &mut Vec<PathBuf>) {
        for entry in fs::read_dir(dir).unwrap_or_else(|err| panic!("read {}: {err}", dir.display()))
        {
            let entry = entry.expect("read directory entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path, files);
            } else if path.extension().is_some_and(|extension| extension == "js") {
                files.push(path);
            }
        }
    }
    let mut files = Vec::new();
    visit(&fixture.out_root, &mut files);
    files.sort();
    let node = node_path();
    for file in files {
        let output = Command::new(&node)
            .arg("--check")
            .arg(&file)
            .output()
            .unwrap_or_else(|err| panic!("spawn node --check {}: {err}", file.display()));
        assert!(
            output.status.success(),
            "node --check failed for {}\nstdout:\n{}\nstderr:\n{}",
            file.display(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }
}

pub fn list_module_exports(out_root: &Path, module_path: &str) -> Vec<String> {
    let counter = MODULE_EXPORT_PROBE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let probe_path = out_root.join(format!("__probe_module_exports_{counter}.mjs"));
    let probe = format!(
        "const mod = await import({});\nprocess.stdout.write(JSON.stringify(Object.keys(mod)));\n",
        serde_json::to_string(&format!("./{module_path}")).unwrap(),
    );
    fs::write(&probe_path, probe).unwrap();
    let result = run_node_script(&probe_path);
    assert!(
        result.status.success(),
        "probing {} exited {:?}\nstderr:\n{}",
        module_path,
        result.status.code(),
        result.stderr,
    );
    serde_json::from_str(&result.stdout).expect("probe must emit JSON array")
}

pub fn assert_module_exports(
    out_root: &Path,
    module_path: &str,
    includes: &[&str],
    excludes: &[&str],
) {
    let exported: BTreeSet<String> = list_module_exports(out_root, module_path)
        .into_iter()
        .collect();
    let summary = if exported.is_empty() {
        "<none>".to_string()
    } else {
        exported.iter().cloned().collect::<Vec<_>>().join(", ")
    };
    for name in includes {
        assert!(
            exported.contains(*name),
            "expected {module_path} to export {name}; actual exports: {summary}",
        );
    }
    for name in excludes {
        assert!(
            !exported.contains(*name),
            "expected {module_path} to not export {name}; actual exports: {summary}",
        );
    }
}

pub fn assert_module_source(
    out_root: &Path,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    for needle in contains {
        assert!(
            code.contains(*needle),
            "{module_path} did not contain {needle:?}\n--- {module_path} ---\n{code}",
        );
    }
    for needle in does_not_contain {
        assert!(
            !code.contains(*needle),
            "{module_path} unexpectedly contained {needle:?}\n--- {module_path} ---\n{code}",
        );
    }
}

/// Asserts that the one line of `module_path` starting with `below_prefix`
/// is immediately preceded by the line `above` (e.g. a `// comment` line).
pub fn assert_line_directly_above(
    out_root: &Path,
    module_path: &str,
    above: &str,
    below_prefix: &str,
) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    let lines: Vec<&str> = code.lines().collect();
    let below_indices: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with(below_prefix))
        .map(|(index, _)| index)
        .collect();
    let [below_index] = below_indices[..] else {
        panic!(
            "expected exactly one line starting with {below_prefix:?} in {module_path}, found {}\n--- {module_path} ---\n{code}",
            below_indices.len(),
        );
    };
    assert!(
        below_index > 0 && lines[below_index - 1] == above,
        "expected {above:?} directly above {below_prefix:?} in {module_path}\n--- {module_path} ---\n{code}",
    );
}

pub fn assert_pure_cycle_break(
    source: &str,
    logical_modules: Vec<LogicalModuleEntry>,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
    expected_stdout: &str,
) -> Fixture {
    assert_pure_cycle_break_with_opts(
        FixtureOpts::new(source, logical_modules),
        module_path,
        contains,
        does_not_contain,
        expected_stdout,
    )
}

pub fn assert_pure_cycle_break_with_opts(
    opts: FixtureOpts<'_>,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
    expected_stdout: &str,
) -> Fixture {
    let fixture = run_fixture(opts);
    assert_module_source(
        &fixture.out_root,
        &format!("{}/modules/{module_path}.js", fixture.chunk_id),
        contains,
        does_not_contain,
    );
    assert_entry_output(&fixture, expected_stdout);
    fixture
}

pub fn expect_pure_cycle_rejection(opts: FixtureOpts<'_>, module_path: &str) {
    expect_cycle_rejection(opts, &[module_path, "residual"]);
}

pub fn assert_file_ends_with_single_newline(out_root: &Path, module_path: &str) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    let terminal_newlines = code
        .as_bytes()
        .iter()
        .rev()
        .take_while(|&&byte| byte == b'\n')
        .count();
    assert_eq!(
        terminal_newlines, 1,
        "{module_path} must end with exactly one newline:\n{code:?}",
    );
}

fn assert_generated_module_script(out_root: &Path, source: &str, expected_stdout: &str) {
    let counter = GENERATED_MODULE_SCRIPT_COUNTER.fetch_add(1, Ordering::Relaxed);
    let assertion_path = out_root.join(format!("assert_generated_module_{counter}.mjs"));
    fs::write(&assertion_path, source).unwrap();
    assert_node_output(&assertion_path, expected_stdout, "");
}

pub fn assert_generated_module_after_entry_script(
    fixture: &Fixture,
    source: &str,
    expected_stdout: &str,
) {
    let entry_specifier = format!("./{}/entry.js", fixture.chunk_id);
    // Silences the entry's own console.log (the entry already executes its
    // top-level effect) before running the caller-supplied probe script.
    let wrapped = format!(
        "const __log = console.log;\n\
         console.log = () => {{}};\n\
         await import({});\n\
         console.log = __log;\n\
         {source}",
        serde_json::to_string(&entry_specifier).unwrap(),
    );
    assert_generated_module_script(&fixture.out_root, &wrapped, expected_stdout);
}

/// Append a marker print to each listed emitted module file, run the
/// entry under Node, and return the order in which the instrumented
/// module bodies finished evaluating — the observable ECMA-262
/// Phase-2 evaluation post-order (docs/design.md "Lemma 1").
///
/// `modules` are logical-module paths under the chunk's `modules/`
/// directory; the special name `"entry"` resolves to the chunk's
/// `entry.js` — the ESM DFS root, which hosts residual's unclaimed
/// anonymous statements and corresponds to the gate simulator's
/// residual node. The instrumentation happens after the debundler
/// has run, so it perturbs neither the analyzed graph nor the
/// realizability verdict — but it does mutate the emitted files, so
/// run `assert_entry_output`-style checks before calling this.
pub fn node_module_evaluation_order(fixture: &Fixture, modules: &[&str]) -> Vec<String> {
    const MARKER: &str = "__module_eval__:";
    for label in modules {
        let rel_path = if *label == "entry" {
            format!("{}/entry.js", fixture.chunk_id)
        } else {
            format!("{}/modules/{label}.js", fixture.chunk_id)
        };
        let path = fixture.out_root.join(&rel_path);
        let mut code = fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("read emitted module {rel_path}: {err}"));
        code.push_str(&format!("\nconsole.log(\"{MARKER}{label}\");\n"));
        fs::write(&path, code).unwrap();
    }
    let result = run_node_script(&fixture.entry_path);
    assert!(
        result.status.success(),
        "node {} exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.entry_path.display(),
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    result
        .stdout
        .lines()
        .filter_map(|line| line.strip_prefix(MARKER))
        .map(str::to_string)
        .collect()
}

pub fn assert_node_output(path: &Path, expected_stdout: &str, expected_stderr: &str) {
    let result = run_node_script(path);
    assert!(
        result.status.success(),
        "node {} exited {:?}\nstdout:\n{}\nstderr:\n{}",
        path.display(),
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    assert_eq!(result.stdout, expected_stdout, "stdout mismatch");
    assert_eq!(result.stderr, expected_stderr, "stderr mismatch");
}

/// Real source files plus a serialized spec, ready for any CLI execution mode.
struct PreparedFixture {
    root: TempDir,
    spec_path: PathBuf,
    out_root: PathBuf,
    report_root: PathBuf,
    snapshot_root: PathBuf,
    js_list_path: PathBuf,
}

fn prepare_fixture(opts: &FixtureOpts<'_>) -> PreparedFixture {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let extracted_root = root.path().join("extracted");
    let out_root = root.path().join("out");
    let report_root = out_root.join("reports").join("tree");
    let snapshot_root = root.path().join("snapshot");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();

    // Mark the snapshot tree as ESM so node loads emitted .js files as modules.
    write_text_file(
        &snapshot_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&PackageManifest {
                module_type: "module"
            })
            .unwrap()
        ),
    );

    let entry_file = format!("{}.js", opts.chunk_id);
    write_text_file(&snapshot_root.join(&entry_file), opts.source);
    for (rel_path, content) in opts.extra_files {
        write_text_file(&snapshot_root.join(rel_path), content);
    }

    // `extra_chunks` are listed in js-files.txt so they are parsed and
    // analyzed (their exports feed the cross-module oracle), unlike
    // `extra_files` which are runtime-only siblings.
    let mut js_list = format!("{entry_file}\n");
    for (chunk_id, source) in opts.extra_chunks {
        let chunk_file = format!("{chunk_id}.js");
        write_text_file(&snapshot_root.join(&chunk_file), source);
        js_list.push_str(&chunk_file);
        js_list.push('\n');
    }
    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &js_list);

    let setup = PreparedFixture {
        spec_path: root.path().join("transform_spec.yaml"),
        root,
        out_root,
        report_root,
        snapshot_root,
        js_list_path,
    };
    write_yaml_file(&setup.spec_path, &build_spec(opts, &setup));
    setup
}

/// Slugified test path for the current `#[test]` thread, used as the
/// tempdir prefix so failed runs are easy to pick out of `/tmp` without
/// each test having to repeat its own name.
fn current_test_prefix() -> String {
    let thread = std::thread::current();
    let slug: String = thread
        .name()
        .unwrap_or("unknown")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let compact = slug
        .split('-')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    format!("debundle-e2e-{compact}-")
}

pub fn write_text_file(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

/// Parse a CLI invocation's stdout as JSON, panicking with both stdio streams
/// on failure so a non-JSON (e.g. error) stdout is legible in the test log.
pub fn parse_stdout_json(out: &std::process::Output) -> Value {
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err})\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr),
        )
    })
}

/// Run `debundle <args>` and return its raw output.
pub fn run_debundle(args: &[&str]) -> std::process::Output {
    Command::new(debundler_path())
        .args(args)
        .output()
        .expect("spawn debundle")
}

/// Run `debundle spec synthesize-selectors --modules <dir> [extra...]`, asserting
/// success and returning the raw output for the caller to parse.
pub fn run_synthesize_selectors(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "synthesize-selectors",
        "--modules",
        modules.to_str().unwrap(),
    ];
    args.extend_from_slice(extra);
    let out = run_debundle(&args);
    assert!(
        out.status.success(),
        "non-zero exit\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

/// Owner-graph node id that declares `binding`, panicking (with the node dump)
/// if none does.
pub fn owner_for_binding<'a>(graph: &'a OwnerGraphReport, binding: &str) -> &'a str {
    let node = graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
        .unwrap_or_else(|| {
            panic!(
                "no owner-graph node declares binding `{binding}`; \
                 nodes: {:#?}",
                graph.nodes,
            )
        });
    node.id.as_str()
}

pub fn write_yaml_file<T: Serialize + ?Sized>(path: &Path, value: &T) {
    // `serde_json` is built with `arbitrary_precision` workspace-wide (feature
    // unification), which makes `serde_yaml` emit a `serde_json::Value::Number`
    // as a map — so a spec carrying a number (e.g. `passed_to_call.arg_index`)
    // round-trips as garbage. Serialize to JSON first (serde_json serializes its
    // own arbitrary-precision numbers correctly), then re-emit as YAML. JSON is a
    // YAML subset, so the debundler's serde_yaml reader parses it unchanged.
    let json = serde_json::to_string(value).expect("serialize spec to JSON");
    let yaml: serde_yaml::Value = serde_yaml::from_str(&json).expect("reparse JSON as YAML");
    fs::write(path, format!("{}\n", serde_yaml::to_string(&yaml).unwrap())).unwrap();
}

pub fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

pub fn debundler_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, DEBUNDLER_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve debundler runfile: {DEBUNDLER_RLOCATION}"))
}

/// `debundle` as a release download runs it: a copy of the binary alone in an
/// otherwise empty directory, spawned with an empty environment (no runfiles).
pub struct StandaloneDebundle {
    dir: TempDir,
}

impl StandaloneDebundle {
    pub fn install() -> Self {
        let dir = TempDir::with_prefix(current_test_prefix()).expect("create install dir");
        fs::copy(debundler_path(), dir.path().join("debundle")).expect("copy debundle binary");
        Self { dir }
    }

    pub fn run(&self, args: &[&str]) -> CommandResult {
        let bin = self.dir.path().join("debundle");
        let output = Command::new(&bin)
            .args(args)
            .env_clear()
            .output()
            .unwrap_or_else(|e| panic!("spawn debundle {}: {e}", bin.display()));
        command_result(output)
    }
}

fn node_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, NODE_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve node runfile: {NODE_RLOCATION}"))
}

pub struct CommandResult {
    pub stdout: String,
    pub stderr: String,
    pub status: std::process::ExitStatus,
}

fn command_result(output: std::process::Output) -> CommandResult {
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

/// `debundle spec validate --modules <modules_root> --source-file <source_file>`:
/// the source-only preflight.
pub fn run_source_only_validate(
    modules_root: &Path,
    source_file: &Path,
    extra_args: &[&str],
) -> CommandResult {
    let bin = debundler_path();
    let output = Command::new(&bin)
        .args(["spec", "validate", "--modules"])
        .arg(modules_root)
        .arg("--source-file")
        .arg(source_file)
        .args(extra_args)
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    command_result(output)
}

/// `debundle spec match-selector --source-file <source_file> --match <selector>
/// --format json`, parsed; panics on a non-zero exit.
pub fn run_match_selector(source_file: &Path, selector: &str, extra_args: &[&str]) -> Value {
    let bin = debundler_path();
    let result = command_result(
        Command::new(&bin)
            .args(["spec", "match-selector", "--source-file"])
            .arg(source_file)
            .args(["--match", selector, "--format", "json"])
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    );
    assert!(
        result.status.success(),
        "match-selector exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    serde_json::from_str(&result.stdout).unwrap_or_else(|e| {
        panic!(
            "match-selector stdout is not JSON ({e}):\n{}",
            result.stdout
        )
    })
}

/// The `outcomes` of the `static/app` chunk's `selector_diagnostics.json`
/// under a `debundle run --dry-run` report root.
pub fn read_selector_outcomes(report_root: &Path) -> Vec<Value> {
    read_chunk_selector_outcomes(report_root, "static/app")
}

/// The `outcomes` of `chunk`'s `selector_diagnostics.json` under a report root.
pub fn read_chunk_selector_outcomes(report_root: &Path, chunk: &str) -> Vec<Value> {
    let report: Value = read_json(&report_root.join(chunk).join("selector_diagnostics.json"));
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
        .clone()
}

/// The outcome lines of a `debundle run` stderr report, each from its `[kind]` tag on.
pub fn reported_outcome_lines(stderr: &str) -> Vec<String> {
    stderr
        .lines()
        .filter_map(|line| line.strip_prefix("  - ["))
        .map(|line| format!("[{line}"))
        .collect()
}

/// Asserts the stderr report has no outcome line beyond `outcomes`' records, and for each
/// record exactly one line that opens with its kind, chunk and logical module and names its
/// entity before the description. Names the description mentions (a conflict's other
/// selectors) do not count.
pub fn assert_stderr_lists_every_outcome(stderr: &str, outcomes: &[Value]) {
    let lines = reported_outcome_lines(stderr);
    assert_eq!(
        lines.len(),
        outcomes.len(),
        "the report must have one line per recorded outcome\nstderr:\n{stderr}\nrecorded: {outcomes:#?}"
    );
    for record in outcomes {
        let opening = format!(
            "[{}] {}::{} ",
            record["outcome"]["kind"].as_str().unwrap(),
            record["chunk"].as_str().unwrap(),
            record["placement"]["logical_module"].as_str().unwrap(),
        );
        let entity = &record["placement"]["entity"];
        let entity = match entity["export"].as_str() {
            Some(name) => format!("`{name}`"),
            None => format!("anonymous_statements[{}]", entity["anonymous_statement"]),
        };
        let matching = lines
            .iter()
            .filter(|line| {
                let (head, _description) = line.split_once(": ").unwrap_or((line, ""));
                head.starts_with(&opening) && head.contains(&entity)
            })
            .count();
        assert_eq!(
            matching, 1,
            "the report must have exactly one line for {record:#}\nstderr:\n{stderr}"
        );
    }
}

/// The outcome record of `kind` placed in `logical_module`.
pub fn find_outcome_in_module<'a>(
    outcomes: &'a [Value],
    kind: &str,
    logical_module: &str,
) -> &'a Value {
    outcomes
        .iter()
        .find(|record| {
            record["outcome"]["kind"] == kind
                && record["placement"]["logical_module"] == logical_module
        })
        .unwrap_or_else(|| panic!("missing {kind} outcome in {logical_module}: {outcomes:#?}"))
}

/// The outcome record of `kind` whose entity is the export `export_name`.
pub fn find_outcome<'a>(outcomes: &'a [Value], kind: &str, export_name: &str) -> &'a Value {
    outcomes
        .iter()
        .find(|record| {
            record["outcome"]["kind"] == kind
                && record["placement"]["entity"]["export"] == export_name
        })
        .unwrap_or_else(|| panic!("missing {kind} outcome for export {export_name}: {outcomes:#?}"))
}

fn spawn_transform(spec_path: &Path) -> CommandResult {
    run_debundler(spec_path, &[])
}

fn spawn_transform_with_args(spec_path: &Path, extra_args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    command_result(
        Command::new(&bin)
            .arg("run")
            .arg("--spec")
            .arg(spec_path)
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

/// Run `debundle run --spec <path> [--package-root <name>=<dir> ...]` and return its
/// captured stdio + exit status. Used by tests that exercise pipeline stages
/// outside the logical-modules harness in [`run_fixture`].
pub fn run_debundler(spec_path: &Path, package_roots: &[(&str, &Path)]) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command.arg("run").arg("--spec").arg(spec_path);
    for (name, dir) in package_roots {
        command
            .arg("--package-root")
            .arg(format!("{name}={}", dir.display()));
    }
    command_result(
        command
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

/// A tree-authored spec over several chunks: `chunks` are `(chunk id,
/// source)`, `module_roots` are `(tree root, chunk id)`, and `modules` are
/// `(path below the modules root, module YAML)`. Every chunk is inlined into
/// its entry when unassigned; the first chunk is `main_chunk_id`.
pub struct TreeFixture<'a> {
    pub chunks: &'a [(&'a str, &'a str)],
    pub module_roots: &'a [(&'a str, &'a str)],
    pub modules: &'a [(&'a str, &'a str)],
}

pub struct TreeRun {
    _root: TempDir,
    pub out_root: PathBuf,
    pub report_root: PathBuf,
    pub result: CommandResult,
}

/// Writes `fixture` and runs `debundle run` on it in tree form with
/// `extra_args`, writing the JS tree.
pub fn run_tree_fixture(fixture: &TreeFixture<'_>, extra_args: &[&str]) -> TreeRun {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let snapshot = root.path().join("snapshot");
    let modules = root.path().join("modules");
    let out_root = root.path().join("out");
    let mut js_list = String::new();
    let mut unassigned_mode = String::new();
    for (chunk, source) in fixture.chunks {
        write_text_file(&snapshot.join(format!("{chunk}.js")), source);
        js_list.push_str(&format!("{chunk}.js\n"));
        unassigned_mode.push_str(&format!("  {chunk}: {{ kind: inline_in_entry }}\n"));
    }
    write_text_file(&root.path().join("js-files.txt"), &js_list);
    let module_roots = fixture
        .module_roots
        .iter()
        .map(|(tree, chunk)| format!("  {tree}: {chunk}\n"))
        .collect::<String>();
    for (path, body) in fixture.modules {
        write_text_file(&modules.join(path), body);
    }
    let config = root.path().join("spec_config.yaml");
    write_text_file(
        &config,
        &format!(
            "main_chunk_id: {}\nmodule_roots:\n{module_roots}inputs:\n  root: snapshot\n  \
             js_list_path: js-files.txt\nwrite_js_tree: true\nunassigned_mode:\n{unassigned_mode}",
            fixture.chunks[0].0,
        ),
    );
    let vendor_marks = root.path().join("vendor_marks.yaml");
    write_text_file(&vendor_marks, "vendor_marks: []\n");
    let bin = debundler_path();
    let output = Command::new(&bin)
        .arg("run")
        .arg("--tree-config")
        .arg(&config)
        .arg("--tree-modules")
        .arg(&modules)
        .arg("--tree-vendor-marks")
        .arg(&vendor_marks)
        .arg("--tree-source-root")
        .arg(root.path())
        .arg("--out-root")
        .arg(&out_root)
        .args(extra_args)
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    TreeRun {
        report_root: out_root.join("reports").join("tree"),
        out_root,
        result: command_result(output),
        _root: root,
    }
}

fn run_node_script(path: &Path) -> CommandResult {
    let node = node_path();
    command_result(
        Command::new(&node)
            .arg(path)
            .output()
            .unwrap_or_else(|e| panic!("spawn node {}: {e}", node.display())),
    )
}
