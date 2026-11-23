//! Staged-analysis fixtures and command builders: `spec project-analysis`,
//! `analyze` and `run --analysis` invocations, and direct-versus-staged
//! output comparison.

use super::*;
use std::collections::BTreeMap;

pub struct StagedInvocation {
    pub result: CommandResult,
    pub harness_root: Option<PathBuf>,
    pub invocation_spec: Option<PathBuf>,
    pub _outputs: CaseWorkspace,
}

impl StagedInvocation {
    pub fn tree_command(args: &[String], outputs: CaseWorkspace) -> Self {
        Self {
            result: command(&args.iter().map(String::as_str).collect::<Vec<_>>()),
            _outputs: outputs,
            harness_root: None,
            invocation_spec: None,
        }
    }

    pub fn output_root(&self) -> PathBuf {
        self._outputs.path().join("out")
    }

    /// Install runtime-only siblings just as the original fixture run does.
    pub fn install_runtime_files(&self, files: &[(&str, &str)]) {
        mirror_runtime_extra_files(
            &self.output_root().join("app"),
            files,
            self.result.status.success(),
        );
    }

    pub fn harness_root(&self) -> Option<&Path> {
        self.harness_root.as_deref()
    }

    pub fn invocation_spec(&self) -> Option<&Path> {
        self.invocation_spec.as_deref()
    }
}

/// Two modules split across an at-init read, a cross-module pure import, and
/// a member annotation, so the artifact carries cross-module summaries,
/// declared assumptions and per-chunk facts.
pub fn split_with_pure_import() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"import { callWrap as wrap } from "./adapter.js";
const A = wrap(function () { return "a"; });
const B = wrap(function () { return "b"; });
function helper() { return A + B; }
console.log(A, B, helper());
export { A, B, helper };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A")]),
            logical_module("mod_b", &[Member::new("B"), Member::new("helper")]),
        ],
    )
    .with_extra_chunks(&[
        (
            "static/adapter",
            "import { wrap } from \"./vendorlib.js\";\n\
             export function callWrap(f) { return wrap(f); }\n",
        ),
        (
            "static/vendorlib",
            "export function wrap(f) { return { kind: \"wrapped\", impl: f }; }\n",
        ),
    ])
}

/// The impure variant: the gate rejects the interleaved split.
pub fn rejected_split() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"import { wrap } from "./vendorlib.js";
const A = wrap(function () { return "a"; });
const B = wrap(function () { return "b"; });
const C = wrap(function () { return "c"; });
console.log(A, B, C);
export { A, B, C };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("C")]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    )
    .with_extra_chunks(&[(
        "static/vendorlib",
        "export function wrap(f) { globalThis.__sink = f; return { impl: f }; }\n",
    )])
}

pub struct Staged {
    pub workspace: CaseWorkspace,
    pub spec: PathBuf,
    pub projection: PathBuf,
    pub artifact: PathBuf,
}

impl Staged {
    pub fn root(&self) -> &Path {
        self.spec.parent().expect("spec has a directory")
    }

    pub fn project(&self, out: &Path) -> CommandResult {
        command(&[
            "spec",
            "project-analysis",
            "--spec",
            path(&self.spec),
            "--out",
            path(out),
        ])
    }

    pub fn analyze(&self) -> CommandResult {
        let root = self.root();
        command_with(
            &analyzer_path(),
            &[
                "--projection",
                path(&self.projection),
                "--tree-source-root",
                path(root),
                "--out",
                path(&self.artifact),
            ],
        )
    }

    pub fn run(&self, extra: &[&str]) -> StagedInvocation {
        self.run_with_graphs(true, extra)
    }

    pub fn run_without_graphs(&self, extra: &[&str]) -> StagedInvocation {
        self.run_with_graphs(false, extra)
    }

    pub fn run_with_graphs(&self, graphs: bool, extra: &[&str]) -> StagedInvocation {
        let outputs = self.workspace.child();
        let invocation_spec = outputs.path().join("transform_spec.yaml");
        let harness_root = outputs.path().join("harness");
        let output_root = outputs.path().join("out");

        let mut spec: spec::TransformSpec =
            serde_yaml::from_slice(&fs::read(&self.spec).expect("read flat fixture spec"))
                .expect("deserialize typed flat fixture spec");
        spec.write_js_tree
            .as_mut()
            .expect("flat fixture writes a JS tree")
            .out_dir = output_root.clone();
        spec.materialize_logical_modules.report_out_dir = Some(output_root.join("reports/tree"));
        let has_harness = spec.emit_browser_harness.is_some();
        if let Some(harness) = spec.emit_browser_harness.as_mut() {
            harness.out_dir = harness_root.clone();
        }
        fs::write(
            &invocation_spec,
            serde_yaml::to_string(&spec).expect("serialize typed per-run fixture spec"),
        )
        .expect("write per-run fixture spec");

        let mut args = vec!["run"];
        if graphs {
            args.push("--graphs");
        }
        args.extend(["--spec", path(&invocation_spec)]);
        args.extend_from_slice(extra);
        StagedInvocation {
            result: command(&args),
            _outputs: outputs,
            harness_root: has_harness.then_some(harness_root),
            invocation_spec: Some(invocation_spec),
        }
    }
}

pub fn path(path: &Path) -> &str {
    path.to_str().expect("UTF-8 test path")
}

pub struct TreeStaged {
    pub run: TreeRun,
    pub modules: PathBuf,
    pub config: PathBuf,
    pub vendor_marks: PathBuf,
    pub graph: PathBuf,
    pub source: PathBuf,
    pub projection: PathBuf,
    pub artifact: PathBuf,
}

impl TreeStaged {
    pub fn analysis_args(&self, analysis: &Path, output_root: &Path) -> [String; 12] {
        [
            "--tree-config".to_string(),
            path(&self.config).to_string(),
            "--tree-modules".to_string(),
            path(&self.modules).to_string(),
            "--tree-vendor-marks".to_string(),
            path(&self.vendor_marks).to_string(),
            "--tree-source-root".to_string(),
            path(self.root()).to_string(),
            "--out-root".to_string(),
            path(output_root).to_string(),
            "--analysis".to_string(),
            path(analysis).to_string(),
        ]
    }

    pub fn root(&self) -> &Path {
        self.run.root()
    }

    /// Runs `words` (the verb and its leading positionals) with the staged graph and
    /// modules tree, then `trailing` verb arguments. The CLI accepts these options and
    /// positionals in any order.
    fn edit(
        &self,
        words: &[&str],
        trailing: &[&str],
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let root = self.root();
        let outputs = self.run.workspace().child();
        let output_root = outputs.path().join("out");
        let mut args = words.iter().map(ToString::to_string).collect::<Vec<_>>();
        args.extend([
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--format".to_string(),
            "json".to_string(),
        ]);
        args.extend(trailing.iter().map(ToString::to_string));
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            args.extend(self.analysis_args(analysis, &output_root));
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    pub fn assign(
        &self,
        triple: Option<&str>,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        self.edit(
            &["bindings", "assign"],
            triple.as_slice(),
            analysis,
            dry_run,
        )
    }

    pub fn unassign(&self, sym: &str, analysis: Option<&Path>, dry_run: bool) -> StagedInvocation {
        self.edit(&["bindings", "unassign", sym], &[], analysis, dry_run)
    }

    pub fn merge(
        &self,
        target: &str,
        source: &str,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let trailing = ["--target", target, source];
        self.edit(&["modules", "merge"], &trailing, analysis, dry_run)
    }

    pub fn delete(
        &self,
        module: &str,
        force: bool,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let trailing = force.then_some("--force");
        self.edit(
            &["modules", "delete", module],
            trailing.as_slice(),
            analysis,
            dry_run,
        )
    }

    pub fn apply_args(
        &self,
        batch: &Path,
        analysis: Option<&Path>,
        dry_run: bool,
        complete_context: bool,
        partial_context_without_analysis: bool,
        output_root: &Path,
    ) -> Vec<String> {
        let root = self.root();
        let mut args = vec![
            "modules".to_string(),
            "apply".to_string(),
            "--assignments".to_string(),
            path(batch).to_string(),
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--tree-config".to_string(),
            path(&self.config).to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            if complete_context {
                args.extend([
                    "--tree-vendor-marks".to_string(),
                    path(&self.vendor_marks).to_string(),
                    "--tree-source-root".to_string(),
                    path(root).to_string(),
                    "--out-root".to_string(),
                    path(output_root).to_string(),
                ]);
            }
            args.extend(["--analysis".to_string(), path(analysis).to_string()]);
        } else if partial_context_without_analysis {
            args.extend(["--tree-source-root".to_string(), path(root).to_string()]);
        }
        args
    }

    pub fn apply(
        &self,
        batch: &Path,
        analysis: Option<&Path>,
        dry_run: bool,
        complete_context: bool,
        partial_context_without_analysis: bool,
    ) -> StagedInvocation {
        let outputs = self.run.workspace().child();
        let output_root = outputs.path().join("out");
        let args = self.apply_args(
            batch,
            analysis,
            dry_run,
            complete_context,
            partial_context_without_analysis,
            &output_root,
        );
        StagedInvocation::tree_command(&args, outputs)
    }

    pub fn run_with_analysis(&self, analysis: &Path, emit_on_rejection: bool) -> StagedInvocation {
        let outputs = self.run.workspace().child();
        let output_root = outputs.path().join("out");
        let mut args = vec!["run".to_string(), "--graphs".to_string()];
        args.extend(self.analysis_args(analysis, &output_root));
        if emit_on_rejection {
            args.push("--emit-on-rejection".to_string());
        }
        StagedInvocation::tree_command(&args, outputs)
    }
}

pub fn staged_tree(chunks: &[(&str, &str)], modules: &[(&str, &str)]) -> TreeStaged {
    staged_tree_with_module_roots(chunks, &[], modules)
}

/// Two ordered functions assigned to separate modules for candidate edit tests.
pub fn two_function_ordering_tree() -> TreeStaged {
    staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[
            (
                "a_earlier.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "z_later.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
        ],
    )
}

/// An unrelated saved analysis for stale-artifact refusal tests.
pub fn foreign_analysis_tree() -> TreeStaged {
    staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    )
}

pub fn staged_tree_with_module_roots(
    chunks: &[(&str, &str)],
    module_roots: &[(&str, &str)],
    modules: &[(&str, &str)],
) -> TreeStaged {
    let workspace = write_tree_fixture(&TreeFixture {
        chunks,
        module_roots,
        modules,
    });
    let root = workspace.root().to_path_buf();
    let projection = root.join("analysis.projection");
    let artifact = root.join("analysis.dba");
    let mut project_args = vec!["spec".to_string(), "project-analysis".to_string()];
    project_args.extend(
        tree_spec_args(&root)
            .into_iter()
            .map(|arg| arg.into_string().expect("UTF-8 fixture path")),
    );
    project_args.extend([
        "--out-root".to_string(),
        path(&root.join("project-out")).to_string(),
        "--out".to_string(),
        path(&projection).to_string(),
    ]);
    assert_ok(
        &command(&project_args.iter().map(String::as_str).collect::<Vec<_>>()),
        "tree spec project-analysis",
    );
    assert_ok(
        &command_with(
            &analyzer_path(),
            &[
                "--projection",
                path(&projection),
                "--tree-source-root",
                path(&root),
                "--out",
                path(&artifact),
            ],
        ),
        "tree analyze",
    );
    let run = workspace.run(&["--analysis", path(&artifact), "--graphs"]);
    let staged = TreeStaged {
        modules: root.join("modules"),
        config: root.join("spec_config.yaml"),
        vendor_marks: root.join("vendor_marks.yaml"),
        graph: run.report_root.join("main/dependency_graph.json.gz"),
        source: root.join("snapshot/main.js"),
        projection,
        artifact,
        run,
    };
    assert!(staged.graph.is_file(), "tree graph was not written");
    staged
}

pub fn source_unit_for_binding(staged: &TreeStaged, binding: &str) -> String {
    let graph: Value = read_gzip_json(&staged.graph);
    let units = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|declared| {
                    declared
                        .iter()
                        .any(|declared| declared["binding"].as_str() == Some(binding))
                })
        })
        .map(|node| node["id"].as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    assert_eq!(units.len(), 1, "expected one source unit for {binding:?}");
    units.into_iter().next().unwrap()
}

pub fn module_snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    read_tree(root)
        .into_iter()
        .map(|(relative, bytes)| (root.join(relative), bytes))
        .collect()
}

pub fn assert_candidate_assessment_matches_run(
    assessment: &Value,
    run: &TreeStaged,
    expect_success: bool,
    emit_on_rejection: bool,
) -> StagedInvocation {
    let result = run.run_with_analysis(&run.artifact, emit_on_rejection);
    let out_root = result.output_root();
    assert_eq!(
        result.result.status.success(),
        expect_success,
        "candidate run failed unexpectedly:\n{}",
        result.result.stderr
    );
    let report = read_effect_report(&out_root);
    for field in [
        "decision",
        "emitted_order",
        "swapped_pairs",
        "relocation_dependencies",
        "premise_dependencies",
        "counts",
        "coverage",
    ] {
        assert_eq!(assessment[field], report[field], "assessment field {field}");
    }
    let placement_report: Value = serde_json::from_slice(
        &fs::read(out_root.join("reports/tree/main/candidate_relocation_assessment.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        assessment["placement"], placement_report["placement"],
        "candidate preview and emitted run must use the same typed placement result"
    );
    result
}

pub fn read_effect_report(out_root: &Path) -> Value {
    serde_json::from_slice(
        &fs::read(out_root.join("reports/tree/main/effect_translation_validation.json")).unwrap(),
    )
    .unwrap()
}

pub fn command(args: &[&str]) -> CommandResult {
    command_with(&debundler_path(), args)
}

pub fn command_with(binary: &Path, args: &[&str]) -> CommandResult {
    let mut command = Command::new(binary);
    command.args(args);
    let hint = case_commands::command_hint(&command);
    if !hint.as_os_str().is_empty() {
        command.current_dir(&hint);
    }
    let output =
        case_commands::debundle_fixture_output(&mut command, &hint).expect("spawn debundle tool");
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

pub fn assert_ok(result: &CommandResult, what: &str) {
    assert!(
        result.status.success(),
        "{what} exited {:?}\nstderr:\n{}",
        result.status.code(),
        result.stderr
    );
}

pub fn staged(opts: FixtureOpts<'_>) -> (Staged, ValidateFixture) {
    let fixture = write_validate_fixture_spec(opts);
    let root = fixture.spec_path.parent().unwrap().to_path_buf();
    let staged = Staged {
        workspace: fixture._root.clone(),
        spec: fixture.spec_path.clone(),
        projection: root.join("analysis.projection"),
        artifact: root.join("analysis.dba"),
    };
    assert_ok(&staged.project(&staged.projection), "spec project-analysis");
    assert_ok(&staged.analyze(), "analyze");
    (staged, fixture)
}

/// Every file under `root`, JSON reports without their `provenance` (it
/// records the generating command line, which differs by design).
pub fn tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    read_tree(root)
        .into_iter()
        .map(|(relative, bytes)| {
            let json = if relative
                .extension()
                .is_some_and(|extension| extension == "gz")
            {
                Ok(read_gzip_json::<Value>(&root.join(&relative)))
            } else {
                serde_json::from_slice::<Value>(&bytes)
            };
            let normalized = match json {
                Ok(Value::Object(mut report)) if report.contains_key("provenance") => {
                    report.remove("provenance");
                    serde_json::to_vec(&report).unwrap()
                }
                _ => bytes,
            };
            (relative, normalized)
        })
        .collect()
}

pub fn assert_same_invocation_trees(direct: &StagedInvocation, staged: &StagedInvocation) {
    let direct = invocation_tree(direct);
    let staged = invocation_tree(staged);
    assert_eq!(
        direct.keys().collect::<Vec<_>>(),
        staged.keys().collect::<Vec<_>>()
    );
    for (file, bytes) in &direct {
        assert!(
            staged[file] == *bytes,
            "{} differs between direct and staged runs",
            file.display()
        );
    }
}

pub fn invocation_tree(invocation: &StagedInvocation) -> BTreeMap<PathBuf, Vec<u8>> {
    let output_root = invocation.output_root();
    let mut files = tree(&output_root);
    let spec_path = invocation.invocation_spec();
    if let Some(spec_path) = spec_path {
        assert!(spec_path.is_file(), "invocation spec disappeared");
    }
    for (relative, bytes) in &mut files {
        if relative == Path::new("GATE_REJECTED") {
            *bytes = normalize_invocation_rejection_marker(invocation, bytes);
            continue;
        }
        if relative.starts_with("app") && relative.extension().is_some_and(|ext| ext == "js") {
            *bytes = normalize_invocation_gate_banner(invocation, bytes);
            continue;
        }
        // Gzipped reports are JSON payloads. Decode them so exact spec-path
        // metadata can be normalized without skipping any report fields.
        let mut report = if relative.extension().is_some_and(|ext| ext == "gz") {
            read_gzip_json::<Value>(&output_root.join(relative))
        } else {
            let Ok(report) = serde_json::from_slice::<Value>(bytes) else {
                continue;
            };
            report
        };
        if let Value::Object(fields) = &mut report {
            fields.remove("provenance");
        }
        if let Some(spec_path) = spec_path {
            normalize_invocation_spec_locations(&mut report, spec_path);
        }
        *bytes = serde_json::to_vec(&report).unwrap();
    }
    files
}

pub fn normalize_invocation_rejection_marker(
    invocation: &StagedInvocation,
    marker: &[u8],
) -> Vec<u8> {
    let marker = std::str::from_utf8(marker).expect("rejection marker is UTF-8");
    let output_root = invocation.output_root();
    let reports_root = output_root.join("reports");
    assert!(
        reports_root.is_dir(),
        "rejected invocation has no report tree"
    );
    let mut normalized = marker.to_string();
    for report in module_snapshot(&reports_root).keys() {
        assert!(report.starts_with(&reports_root) && report.is_file());
        let relative = report
            .strip_prefix(&output_root)
            .expect("invocation report is under its output root");
        normalized = normalized.replace(
            &report.to_string_lossy().to_string(),
            &format!("<invocation-output>/{}", relative.to_string_lossy()),
        );
    }
    assert!(
        !normalized.contains(&reports_root.to_string_lossy().to_string()),
        "rejection marker contains an unrecognized path under this invocation's report tree:\n{normalized}"
    );
    normalized.into_bytes()
}

pub fn normalize_invocation_gate_banner(invocation: &StagedInvocation, code: &[u8]) -> Vec<u8> {
    let code = std::str::from_utf8(code).expect("emitted JavaScript is UTF-8");
    let lines: Vec<_> = code.split_inclusive('\n').collect();
    let first_line = lines
        .first()
        .map(|line| line.trim_end_matches(['\r', '\n']))
        .unwrap_or_default();
    if !first_line.starts_with("// GATE_REJECTED: NOT RUNNABLE.") {
        return code.as_bytes().to_vec();
    }
    let banner_end = lines
        .iter()
        .position(|line| {
            line.trim_end_matches(['\r', '\n'])
                == "// See the GATE_REJECTED marker at the output root."
        })
        .expect("generated gate banner has its closing marker line");

    let mut normalized = String::with_capacity(code.len());
    for (index, raw_line) in lines.iter().enumerate() {
        let line = raw_line.trim_end_matches(['\r', '\n']);
        if index <= banner_end
            && let Some(path) = line.strip_prefix("// Evidence: ")
        {
            normalized.push_str("// Evidence: ");
            normalized.push_str(&normalize_invocation_report_path(invocation, path));
            normalized.push_str(&raw_line[line.len()..]);
        } else {
            normalized.push_str(raw_line);
        }
    }
    normalized.into_bytes()
}

pub fn normalize_invocation_report_path(invocation: &StagedInvocation, report: &str) -> String {
    let report = Path::new(report);
    let output_root = invocation.output_root();
    let reports_root = output_root.join("reports");
    assert!(
        report.starts_with(&reports_root) && report.is_file(),
        "evidence path {} does not identify this invocation's report tree",
        report.display()
    );
    format!(
        "<invocation-output>/{}",
        report
            .strip_prefix(&output_root)
            .expect("checked invocation output prefix")
            .to_string_lossy()
    )
}

pub fn normalize_invocation_spec_locations(report: &mut Value, spec_path: &Path) {
    match report {
        Value::Object(fields) => {
            for (field, value) in fields {
                if field == "spec_location" {
                    normalize_invocation_spec_location(value, spec_path);
                } else if field == "argument_input_spec_locations"
                    && let Value::Array(locations) = value
                {
                    for location in locations {
                        normalize_invocation_spec_location(location, spec_path);
                    }
                }
                normalize_invocation_spec_locations(value, spec_path);
            }
        }
        Value::Array(values) => {
            for value in values {
                normalize_invocation_spec_locations(value, spec_path);
            }
        }
        _ => {}
    }
}

pub fn normalize_invocation_spec_location(location: &mut Value, spec_path: &Path) {
    let Some(text) = location.as_str() else {
        return;
    };
    let prefix = format!("{}:", spec_path.display());
    let Some(suffix) = text.strip_prefix(&prefix) else {
        return;
    };
    *location = Value::String(format!("<invocation-spec>:{suffix}"));
}

pub fn assert_staged_run_matches(opts: FixtureOpts<'_>, run_args: &[&str]) {
    let (staged, _fixture) = staged(opts);
    let direct = staged.run(run_args);
    let direct_out = direct.output_root();
    assert!(
        tree(&direct_out).keys().any(|path| path
            .file_name()
            .is_some_and(|name| name == "effect_translation_validation.json")),
        "direct run must produce the facts-backed effect report before staged comparison"
    );
    if run_args.contains(&"--effect-conflicts-jsonl") {
        assert!(
            tree(&direct_out).keys().any(|path| path
                .file_name()
                .is_some_and(|name| name == "effect_conflicts.jsonl")),
            "explicit stream must exist before staged comparison"
        );
    }
    let artifact = path(&staged.artifact).to_owned();
    let mut staged_args = run_args.to_vec();
    staged_args.extend(["--analysis", artifact.as_str()]);
    let from_artifact = staged.run(&staged_args);
    assert_eq!(
        direct.result.status.code(),
        from_artifact.result.status.code()
    );
    assert_same_invocation_trees(&direct, &from_artifact);
}

/// Check the accepted fresh/saved CLI paths under Node, including the original
/// source, and compare their emitted trees and semantic reports.
pub fn assert_staged_entry_output(opts: FixtureOpts<'_>, expected: &str) {
    let (staged, _fixture) = staged(opts);
    assert_node_output(&staged.root().join("snapshot/static/app.js"), expected, "");
    let fresh = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    for invocation in [&fresh, &saved] {
        assert_ok(&invocation.result, "fresh/saved run");
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            expected,
            "",
        );
    }
    assert_same_invocation_trees(&fresh, &saved);
}

pub fn graph_node_for_binding<'a>(graph: &'a Value, binding: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|declared| declared["binding"] == binding)
                })
        })
        .unwrap_or_else(|| panic!("no graph node declares {binding:?}: {graph:#}"))
}

pub fn native_member_fixture() -> FixtureOpts<'static> {
    const SOURCE: &str =
        "var alias = Symbol(1);\nvar other = Symbol(2);\nconsole.log(typeof alias);\n";
    FixtureOpts::new(
        SOURCE,
        vec![logical_module(
            "native",
            &[Member::new("alias"), Member::new("other")],
        )],
    )
    .with_native_operation_inputs(
        "static/app",
        vec![NativeOperationInputContract {
            selector: SourceUnitSelector::ResolvedMember(ResolvedMemberSelector {
                module: "native".to_owned(),
                name: "alias".to_owned(),
            }),
            operation: NativeOperationKind::Symbol,
            audited_source_sha256: source_sha256(SOURCE),
            note: Some("The selected Symbol call is audited.".to_owned()),
        }],
    )
}
