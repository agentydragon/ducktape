//! `debundle spec project-analysis`, `debundle analyze` and `debundle run
//! --analysis` reproduce `debundle run` exactly, and an artifact stays valid
//! across exactly the spec edits analysis does not read.

use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use analysis::DependencyGraphReport;
use debundle_e2e_support::*;
use debundle_e2e_support::{TreeFixture, TreeRun, run_tree_graph_fixture};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use spec::{IntrinsicFamilies, IntrinsicFamily, IntrinsicsNeverWritten, ProgramPremises};
use tempfile::TempDir;

struct StagedInvocation {
    result: CommandResult,
    harness_root: Option<PathBuf>,
    invocation_spec: Option<PathBuf>,
    _outputs: TempDir,
}

impl StagedInvocation {
    fn tree_command(args: &[String], outputs: TempDir) -> Self {
        Self {
            result: command(&args.iter().map(String::as_str).collect::<Vec<_>>()),
            _outputs: outputs,
            harness_root: None,
            invocation_spec: None,
        }
    }

    fn output_root(&self) -> PathBuf {
        self._outputs.path().join("out")
    }

    fn harness_root(&self) -> Option<&Path> {
        self.harness_root.as_deref()
    }

    fn invocation_spec(&self) -> Option<&Path> {
        self.invocation_spec.as_deref()
    }
}

/// Two modules split across an at-init read, a cross-module pure import, and
/// a member annotation, so the artifact carries cross-module summaries,
/// declared assumptions and per-chunk facts.
fn split_with_pure_import() -> FixtureOpts<'static> {
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
fn rejected_split() -> FixtureOpts<'static> {
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

struct Staged {
    spec: PathBuf,
    projection: PathBuf,
    artifact: PathBuf,
}

impl Staged {
    fn root(&self) -> &Path {
        self.spec.parent().expect("spec has a directory")
    }

    fn project(&self, out: &Path) -> CommandResult {
        command(&[
            "spec",
            "project-analysis",
            "--spec",
            path(&self.spec),
            "--out",
            path(out),
        ])
    }

    fn analyze(&self) -> CommandResult {
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

    fn run(&self, extra: &[&str]) -> StagedInvocation {
        self.run_with_graphs(true, extra)
    }

    fn run_without_graphs(&self, extra: &[&str]) -> StagedInvocation {
        self.run_with_graphs(false, extra)
    }

    fn run_with_graphs(&self, graphs: bool, extra: &[&str]) -> StagedInvocation {
        let outputs = TempDir::new_in(self.root()).expect("create per-run output directory");
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

fn path(path: &Path) -> &str {
    path.to_str().expect("UTF-8 test path")
}

struct TreeStaged {
    run: TreeRun,
    modules: PathBuf,
    config: PathBuf,
    vendor_marks: PathBuf,
    graph: PathBuf,
    source: PathBuf,
    projection: PathBuf,
    artifact: PathBuf,
}

impl TreeStaged {
    fn analysis_args(&self, analysis: &Path, output_root: &Path) -> [String; 12] {
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

    fn root(&self) -> &Path {
        self.run.root()
    }

    fn assign(
        &self,
        triple: Option<&str>,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec![
            "bindings".to_string(),
            "assign".to_string(),
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        if let Some(triple) = triple {
            args.push(triple.to_string());
        }
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            args.extend(self.analysis_args(analysis, &output_root));
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    fn unassign(&self, sym: &str, analysis: Option<&Path>, dry_run: bool) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec![
            "bindings".to_string(),
            "unassign".to_string(),
            sym.to_string(),
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            args.extend(self.analysis_args(analysis, &output_root));
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    fn merge(
        &self,
        modules_root: &Path,
        target: &str,
        source: &str,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec![
            "modules".to_string(),
            "merge".to_string(),
            "--modules".to_string(),
            path(modules_root).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--target".to_string(),
            target.to_string(),
            source.to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            args.extend(self.analysis_args(analysis, &output_root));
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    fn delete(
        &self,
        module: &str,
        force: bool,
        analysis: Option<&Path>,
        dry_run: bool,
    ) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec![
            "modules".to_string(),
            "delete".to_string(),
            module.to_string(),
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--format".to_string(),
            "json".to_string(),
        ];
        if force {
            args.push("--force".to_string());
        }
        if dry_run {
            args.push("--dry-run".to_string());
        }
        if let Some(analysis) = analysis {
            args.extend(self.analysis_args(analysis, &output_root));
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    fn apply_args(
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
            "--batch".to_string(),
            path(batch).to_string(),
            "--modules".to_string(),
            path(&self.modules).to_string(),
            "--graph".to_string(),
            path(&self.graph).to_string(),
            "--source-root".to_string(),
            path(&root.join("snapshot")).to_string(),
            "--tree-config".to_string(),
            path(&self.config).to_string(),
            "--size-cap-lines".to_string(),
            "1".to_string(),
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

    fn apply(
        &self,
        batch: &Path,
        analysis: Option<&Path>,
        dry_run: bool,
        complete_context: bool,
        partial_context_without_analysis: bool,
    ) -> StagedInvocation {
        let outputs = TempDir::new_in(self.root()).expect("create per-invocation output directory");
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

    fn refine(
        &self,
        batch: &Path,
        analysis: Option<&Path>,
        dry_run: bool,
        partial_context_without_analysis: bool,
    ) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec![
            "modules".to_string(),
            "refine".to_string(),
            "--batch".to_string(),
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
            args.extend([
                "--tree-vendor-marks".to_string(),
                path(&self.vendor_marks).to_string(),
                "--tree-source-root".to_string(),
                path(root).to_string(),
                "--out-root".to_string(),
                path(&output_root).to_string(),
                "--analysis".to_string(),
                path(analysis).to_string(),
            ]);
        } else if partial_context_without_analysis {
            args.extend(["--tree-source-root".to_string(), path(root).to_string()]);
        }
        StagedInvocation::tree_command(&args, outputs)
    }

    fn run_with_analysis(&self, analysis: &Path, emit_on_rejection: bool) -> StagedInvocation {
        let root = self.root();
        let outputs = TempDir::new_in(root).expect("create per-invocation staged output directory");
        let output_root = outputs.path().join("out");
        let mut args = vec!["run".to_string(), "--graphs".to_string()];
        args.extend(self.analysis_args(analysis, &output_root));
        if emit_on_rejection {
            args.push("--emit-on-rejection".to_string());
        }
        StagedInvocation::tree_command(&args, outputs)
    }
}

fn staged_tree(chunks: &[(&str, &str)], modules: &[(&str, &str)]) -> TreeStaged {
    staged_tree_with_module_roots(chunks, &[], modules)
}

fn staged_tree_with_module_roots(
    chunks: &[(&str, &str)],
    module_roots: &[(&str, &str)],
    modules: &[(&str, &str)],
) -> TreeStaged {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks,
            module_roots,
            modules,
        },
        &[],
    );
    let root = run.root();
    let staged = TreeStaged {
        modules: root.join("modules"),
        config: root.join("spec_config.yaml"),
        vendor_marks: root.join("vendor_marks.yaml"),
        graph: run.report_root.join("main/dependency_graph.json.gz"),
        source: root.join("snapshot/main.js"),
        projection: root.join("analysis.projection"),
        artifact: root.join("analysis.dba"),
        run,
    };
    assert!(staged.graph.is_file(), "tree graph was not written");
    let root_path = staged.root().to_path_buf();
    let project_out = root_path.join("project-out");
    let project_args = [
        "spec",
        "project-analysis",
        "--tree-config",
        path(&staged.config),
        "--tree-modules",
        path(&staged.modules),
        "--tree-vendor-marks",
        path(&staged.vendor_marks),
        "--tree-source-root",
        path(&root_path),
        "--out-root",
        path(&project_out),
        "--out",
        path(&staged.projection),
    ];
    assert_ok(&command(&project_args), "tree spec project-analysis");
    let analyze_args = [
        "--projection",
        path(&staged.projection),
        "--tree-source-root",
        path(&root_path),
        "--out",
        path(&staged.artifact),
    ];
    assert_ok(
        &command_with(&analyzer_path(), &analyze_args),
        "tree analyze",
    );
    staged
}

fn source_unit_for_binding(staged: &TreeStaged, binding: &str) -> String {
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

fn residual_apply_request(
    staged: &TreeStaged,
    binding: &str,
    destination: &str,
    readable: &str,
) -> Value {
    let report = command(&[
        "modules",
        "propose",
        "--graph",
        path(&staged.graph),
        "--modules",
        path(&staged.modules),
        "--tree-config",
        path(&staged.config),
        "--source-root",
        path(&staged.root().join("snapshot")),
        "--size-cap-lines",
        "1",
        "--format",
        "json",
    ]);
    assert_ok(&report, "modules propose for fresh residual claim");
    let report: Value = serde_json::from_str(&report.stdout).unwrap();
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["landable_today"] == true
                && row["binding_ids"]
                    .as_array()
                    .is_some_and(|bindings| bindings.iter().any(|name| name == binding))
        })
        .unwrap_or_else(|| panic!("no landable residual proposal for {binding:?}: {report:#}"));
    json!({
        "proposal_id": proposal["proposed_module_id"],
        "source_unit_ids": proposal["source_unit_ids"],
        "module": destination,
        "readable_names": {(binding): readable},
    })
}

fn module_snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                pending.push(path);
            } else {
                files.insert(path.clone(), fs::read(&path).unwrap());
            }
        }
    }
    files
}

fn assert_candidate_assessment_matches_run(
    application: &Value,
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
    for (chunk, assessment) in application["chunks"].as_object().unwrap() {
        let report: Value = serde_json::from_slice(
            &fs::read(out_root.join(format!(
                "reports/tree/{chunk}/candidate_relocation_assessment.json"
            )))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            assessment, &report,
            "candidate preview and emitted run differ for chunk {chunk}"
        );
    }
    result
}

fn read_effect_report(out_root: &Path) -> Value {
    serde_json::from_slice(
        &fs::read(out_root.join("reports/tree/main/effect_translation_validation.json")).unwrap(),
    )
    .unwrap()
}

fn command(args: &[&str]) -> CommandResult {
    command_with(&debundler_path(), args)
}

fn command_with(binary: &Path, args: &[&str]) -> CommandResult {
    let output = std::process::Command::new(binary)
        .args(args)
        .output()
        .expect("spawn debundle tool");
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

fn assert_ok(result: &CommandResult, what: &str) {
    assert!(
        result.status.success(),
        "{what} exited {:?}\nstderr:\n{}",
        result.status.code(),
        result.stderr
    );
}

fn staged(opts: FixtureOpts<'_>) -> (Staged, ValidateFixture) {
    let fixture = write_validate_fixture_spec(opts);
    let root = fixture.spec_path.parent().unwrap().to_path_buf();
    let staged = Staged {
        spec: fixture.spec_path.clone(),
        projection: root.join("analysis.projection"),
        artifact: root.join("analysis.dba"),
    };
    assert_ok(&staged.project(&staged.projection), "spec project-analysis");
    assert_ok(&staged.analyze(), "analyze");
    (staged, fixture)
}

#[test]
fn projection_rejects_unresolved_and_duplicate_selectors_before_writing() {
    let source = "function first() { return 'same'; }\nfunction second() { return 'same'; }\n";
    for (label, modules, outcome) in [
        (
            "missing",
            vec![logical_module(
                "selected",
                &[Member::source_alpha(
                    "Missing",
                    "function selected() { return 'absent'; }",
                )],
            )],
            "no_match",
        ),
        (
            "ambiguous",
            vec![logical_module(
                "selected",
                &[Member::source_alpha(
                    "Ambiguous",
                    "function selected() { return 'same'; }",
                )],
            )],
            "ambiguous",
        ),
        (
            "duplicate",
            vec![
                logical_module("left", &[Member::renamed("Left", "first")]),
                logical_module("right", &[Member::renamed("Right", "first")]),
            ],
            "duplicate_claim",
        ),
    ] {
        let fixture = write_validate_fixture_spec(FixtureOpts::new(source, modules));
        let root = fixture.spec_path.parent().unwrap();
        for dedicated in [false, true] {
            let out = root.join(format!("{label}-{dedicated}.projection"));
            let args = ["--spec", path(&fixture.spec_path), "--out", path(&out)];
            let result = if dedicated {
                command_with(&projector_path(), &args)
            } else {
                let mut full_args = vec!["spec", "project-analysis"];
                full_args.extend(args);
                command(&full_args)
            };
            assert!(
                !result.status.success(),
                "{label} selector accepted by dedicated={dedicated}: {}",
                result.stderr
            );
            assert!(
                result.stderr.contains(&format!("[{outcome}]")),
                "{label} lost its selector diagnostic with dedicated={dedicated}: {}",
                result.stderr
            );
            assert!(!out.exists(), "{label} wrote a rejected projection");
        }
    }
}

fn assert_projector_matches_flat(staged: &Staged, label: &str) {
    let output = staged.root().join(format!("{label}.projector"));
    let args = ["--spec", path(&staged.spec), "--out", path(&output)];
    assert_ok(
        &command_with(&projector_path(), &args),
        &format!("{label} dedicated project-analysis"),
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(output).unwrap(),
        "{label} dedicated projector changed the canonical flat projection bytes",
    );
}

fn assert_projector_matches_tree(staged: &TreeStaged) {
    let output = staged.root().join("dedicated.projector");
    let project_out = staged.root().join("dedicated-project-out");
    let root = staged.root();
    let args = [
        "--tree-config",
        path(&staged.config),
        "--tree-modules",
        path(&staged.modules),
        "--tree-vendor-marks",
        path(&staged.vendor_marks),
        "--tree-source-root",
        path(root),
        "--out-root",
        path(&project_out),
        "--out",
        path(&output),
    ];
    assert_ok(
        &command_with(&projector_path(), &args),
        "tree dedicated project-analysis",
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(output).unwrap(),
        "dedicated projector changed the canonical tree projection bytes",
    );
}

/// Every file under `root`, JSON reports without their `provenance` (it
/// records the generating command line, which differs by design).
fn tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let entry = entry.unwrap().path();
            if entry.is_dir() {
                pending.push(entry);
                continue;
            }
            let bytes = fs::read(&entry).unwrap();
            let json = if entry.extension().is_some_and(|extension| extension == "gz") {
                Ok(read_gzip_json::<Value>(&entry))
            } else {
                serde_json::from_slice::<Value>(&bytes)
            };
            let bytes = match json {
                Ok(Value::Object(mut report)) if report.contains_key("provenance") => {
                    report.remove("provenance");
                    serde_json::to_vec(&report).unwrap()
                }
                _ => bytes,
            };
            files.insert(entry.strip_prefix(root).unwrap().to_path_buf(), bytes);
        }
    }
    files
}

fn assert_same_invocation_trees(direct: &StagedInvocation, staged: &StagedInvocation) {
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

fn invocation_tree(invocation: &StagedInvocation) -> BTreeMap<PathBuf, Vec<u8>> {
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

fn normalize_invocation_rejection_marker(invocation: &StagedInvocation, marker: &[u8]) -> Vec<u8> {
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

fn normalize_invocation_gate_banner(invocation: &StagedInvocation, code: &[u8]) -> Vec<u8> {
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

fn normalize_invocation_report_path(invocation: &StagedInvocation, report: &str) -> String {
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

fn normalize_invocation_spec_locations(report: &mut Value, spec_path: &Path) {
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

fn normalize_invocation_spec_location(location: &mut Value, spec_path: &Path) {
    let Some(text) = location.as_str() else {
        return;
    };
    let prefix = format!("{}:", spec_path.display());
    let Some(suffix) = text.strip_prefix(&prefix) else {
        return;
    };
    *location = Value::String(format!("<invocation-spec>:{suffix}"));
}

#[test]
fn linkage_export_effects_survive_saved_analysis() {
    assert_staged_run_matches(
        FixtureOpts::new(
            "const first = 1;\nexport { first };\nconst second = 2;\nconsole.log(first, second);\n",
            vec![logical_module("second", &[Member::new("second")])],
        ),
        &[],
    );
}

#[test]
fn activation_cell_instances_and_alias_conflicts_survive_saved_analysis() {
    for independent in [true, false] {
        let (source, modules, original, emitted) = if independent {
            (
                "function make(seed) { return { selected: () => seed }; } const { selected: first } = make(1); const { selected: middle } = make(2); const { selected: last } = make(3); console.log(first(), middle(), last());\n",
                vec![
                    logical_module("outer", &[Member::new("first"), Member::new("last")]),
                    logical_module("inner", &[Member::new("middle")]),
                ],
                "1 2 3\n",
                "1 2 3\n",
            )
        } else {
            (
                "function make() { let state = 1; return { read: () => state, write: () => state = 42 }; } const api = make(); const before = api.read(); const after = api.write(); console.log(before, after);\n",
                vec![logical_module(
                    "writer",
                    &[
                        Member::new("make"),
                        Member::new("api"),
                        Member::new("after"),
                    ],
                )],
                "1 42\n",
                "42 42\n",
            )
        };
        let opts = FixtureOpts::new(source, modules);
        let opts = if independent {
            opts
        } else {
            opts.with_unassigned_mode(unassigned_mode_inline())
        };
        let (staged, _fixture) = staged(opts);
        assert_node_output(&staged.root().join("snapshot/static/app.js"), original, "");
        let direct = staged.run(&["--emit-on-rejection", "--effect-conflicts-jsonl"]);
        let restored = staged.run(&[
            "--emit-on-rejection",
            "--effect-conflicts-jsonl",
            "--analysis",
            path(&staged.artifact),
        ]);
        for invocation in [&direct, &restored] {
            assert_eq!(
                invocation.result.status.code(),
                Some(if independent { 0 } else { 3 }),
                "{}",
                invocation.result.stderr
            );
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                emitted,
                "",
            );
            let graph: Value = read_gzip_json(
                &invocation
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let report = candidate_relocation_report(invocation, "static/app");
            assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
            if independent {
                assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
                let mut origins = BTreeSet::new();
                let mut instances = BTreeSet::new();
                for binding in ["first", "middle", "last"] {
                    let execution =
                        &graph_node_for_binding(&graph, binding)["synchronous_execution"];
                    assert_eq!(
                        execution["binding_effect_authority"]["authority"], "operation_receipt",
                        "{execution:#}"
                    );
                    let cell = execution["heap_frontier"]["composed_synchronous_calls"]["allocations"]["sample"].as_array().unwrap().iter().find(|cell| cell["kind"] == "lexical_cell").unwrap();
                    origins.insert(cell["origin"].as_u64().unwrap());
                    instances.insert(cell["instance"].as_u64().unwrap());
                }
                assert_eq!(origins.len(), 1);
                assert_eq!(instances.len(), 3);
            } else {
                assert_eq!(report["decision"]["status"], "conflict", "{report:#}");
                let before = &graph_node_for_binding(&graph, "before")["synchronous_execution"];
                let after = &graph_node_for_binding(&graph, "after")["synchronous_execution"];
                let cell = before["heap_frontier"]["non_assumed_heap_accesses"]["reads"]["sample"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|cell| cell["kind"] == "lexical_cell")
                    .unwrap();
                assert!(cell["instance"].is_number());
                assert!(
                    after["heap_frontier"]["non_assumed_heap_accesses"]["writes"]["sample"]
                        .as_array()
                        .unwrap()
                        .contains(cell)
                );
                let rows = read_jsonl(
                    &invocation
                        .output_root()
                        .join("reports/tree/static/app/effect_conflicts.jsonl"),
                );
                assert!(
                    rows.iter()
                        .flat_map(|row| row["cells"].as_array().unwrap())
                        .any(|access| &access["cell"] == cell
                            && !access["first_write"].is_null()
                            && !access["later_read"].is_null()),
                    "{rows:#?}"
                );
            }
        }
        assert_same_invocation_trees(&direct, &restored);
    }
}

#[test]
fn object_binding_root_targets_and_receipts_survive_saved_analysis() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const { left: first, right: second } = { left: () => 40, right: 2 }; console.log(first() + second);",
        vec![],
    ));
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct object binding");
    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let receipt = &graph_node_for_binding(&graph, "first")["synchronous_execution"]["binding_effect_authority"];
    assert_eq!(receipt["authority"], "operation_receipt");
    assert_eq!(
        receipt["receipt"]["root_kind"],
        "object_binding_initializer"
    );
    let targets = receipt["receipt"]["targets"].as_array().unwrap();
    assert_eq!(targets.len(), 2);
    assert_eq!(targets[0]["binding"], "first");
    assert_eq!(targets[1]["binding"], "second");
    let artifact = path(&staged.artifact).to_owned();
    let restored = staged.run(&["--analysis", artifact.as_str()]);
    assert_ok(&restored.result, "restored object binding");
    assert_same_invocation_trees(&direct, &restored);
}

fn assert_staged_run_matches(opts: FixtureOpts<'_>, run_args: &[&str]) {
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

fn graph_node_for_binding<'a>(graph: &'a Value, binding: &str) -> &'a Value {
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

#[test]
fn imported_impure_call_causes_survive_staged_analysis_as_shared_refs() {
    let provider = "let calls = 0;\n\
                    function leaf() { calls += 1; return calls; }\n\
                    function middle() { return leaf(); }\n\
                    export function sharedImpure() { return middle(); }\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            r#"import { sharedImpure } from "./provider.js";
const first = sharedImpure();
const second = sharedImpure();
console.log(first, second);
export { first, second };
"#,
            vec![logical_module(
                "results",
                &[Member::new("first"), Member::new("second")],
            )],
        )
        .with_extra_chunks(&[("static/provider", provider)]),
    );

    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct run of shared imported impure calls");
    let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(
        &loaded.result,
        "run shared imported impure calls from analysis",
    );
    assert_same_invocation_trees(&direct, &loaded);

    let direct_entry = direct.output_root().join("app/static/app/entry.js");
    let loaded_entry = loaded.output_root().join("app/static/app/entry.js");
    assert_node_output(&direct_entry, "1 2\n", "");
    assert_node_output(&loaded_entry, "1 2\n", "");

    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let first_reasons = graph_node_for_binding(&graph, "first")["purity"]["reasons"]
        .as_array()
        .expect("first verdict reasons");
    let second_reasons = graph_node_for_binding(&graph, "second")["purity"]["reasons"]
        .as_array()
        .expect("second verdict reasons");
    assert_eq!(first_reasons.len(), 1, "{first_reasons:#?}");
    assert_eq!(second_reasons.len(), 1, "{second_reasons:#?}");
    for reasons in [first_reasons, second_reasons] {
        assert_eq!(reasons[0]["rule"], "impure_function_call");
        assert!(
            reasons[0].get("cause").is_none(),
            "the JSON reason must point to a shared cause row instead of nesting it: {reasons:#?}"
        );
    }
    let shared_cause = first_reasons[0]["cause_ref"]
        .as_str()
        .expect("first imported call has a cause reference");
    assert_eq!(
        second_reasons[0]["cause_ref"].as_str(),
        Some(shared_cause),
        "both statements call the same imported impure callable"
    );

    let causes = graph["purity_causes"]
        .as_array()
        .expect("factored purity cause rows");
    assert_eq!(
        causes.len(),
        3,
        "the sharedImpure -> middle -> leaf chain appears once: {causes:#?}"
    );
    let causes_by_id = causes
        .iter()
        .map(|cause| (cause["id"].as_str().expect("cause row id"), cause))
        .collect::<BTreeMap<_, _>>();
    assert_eq!(causes_by_id.len(), causes.len(), "cause ids are unique");

    let mut pending = vec![shared_cause];
    let mut visited = BTreeSet::new();
    let mut cause_edges = 0;
    let mut saw_leaf_mutation = false;
    while let Some(id) = pending.pop() {
        if !visited.insert(id) {
            continue;
        }
        let cause = causes_by_id
            .get(id)
            .unwrap_or_else(|| panic!("unresolved purity cause {id:?}: {causes:#?}"));
        let reasons = cause["purity"]["reasons"]
            .as_array()
            .expect("cause purity reasons");
        for reason in reasons {
            assert!(
                reason.get("cause").is_none(),
                "nested causes stay factored in the table: {reason:#}"
            );
            if reason["rule"] == "assign_or_update" {
                saw_leaf_mutation = true;
            }
            if let Some(cause_ref) = reason["cause_ref"].as_str() {
                cause_edges += 1;
                pending.push(cause_ref);
            }
        }
    }
    assert_eq!(visited.len(), causes.len(), "all cause rows are reachable");
    assert!(
        cause_edges >= 2,
        "the provider explanation is nested: {causes:#?}"
    );
    assert!(
        saw_leaf_mutation,
        "the chain reaches leaf's state update: {causes:#?}"
    );
}

fn assert_lexical_authority_staged_parity(
    source: &'static str,
    members: &[Member],
    expected_authority: &str,
    expected_calls: Option<usize>,
) {
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module("all", members)],
    ));
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct lexical-authority run");
    let direct_out = direct.output_root();
    let direct_graph: Value =
        read_gzip_json(&direct_out.join("reports/tree/static/app/dependency_graph.json.gz"));
    let direct_result = graph_node_for_binding(&direct_graph, "result");
    let direct_execution = &direct_result["synchronous_execution"];
    assert_eq!(direct_execution["complete"], true, "{direct_execution:#}");
    assert_eq!(
        direct_execution["binding_effect_authority"]["authority"], expected_authority,
        "{direct_execution:#}"
    );
    if let Some(expected_calls) = expected_calls {
        assert_distinct_root_call_receipts(direct_execution, expected_calls);
    }

    let seed = graph_node_for_binding(&direct_graph, "seed");
    assert!(
        direct_graph["edges"]
            .as_array()
            .unwrap()
            .iter()
            .any(|edge| {
                edge["source"] == direct_result["id"]
                    && edge["target"] == seed["id"]
                    && edge["edge_kind"] == "eager_use"
                    && edge["binding"] == "seed"
            }),
        "the observed readSeed effect must remain in the graph: {direct_graph:#}"
    );

    let analysis = path(&staged.artifact).to_owned();
    let loaded = staged.run(&["--analysis", analysis.as_str()]);
    assert_ok(&loaded.result, "run from saved analysis artifact");
    assert_same_invocation_trees(&direct, &loaded);
    assert_node_output(&direct_out.join("app/static/app/entry.js"), "41\n", "");
    assert_node_output(
        &loaded.output_root().join("app/static/app/entry.js"),
        "41\n",
        "",
    );
    let loaded_graph: Value = read_gzip_json(
        &loaded
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let loaded_execution =
        &graph_node_for_binding(&loaded_graph, "result")["synchronous_execution"];
    assert_eq!(
        loaded_execution["binding_effect_authority"]["authority"], expected_authority,
        "the typed authority must survive the validated artifact round-trip: {loaded_execution:#}"
    );
    if let Some(expected_calls) = expected_calls {
        assert_distinct_root_call_receipts(loaded_execution, expected_calls);
    }
}

fn assert_distinct_root_call_receipts(execution: &Value, expected_calls: usize) {
    let calls = execution["binding_effect_authority"]["receipt"]["calls"]
        .as_array()
        .unwrap();
    assert_eq!(calls.len(), expected_calls, "{execution:#}");
    let call_spans = calls
        .iter()
        .map(|call| {
            (
                call["call_span"]["start"].as_u64().unwrap(),
                call["call_span"]["end"].as_u64().unwrap(),
            )
        })
        .collect::<BTreeSet<_>>();
    assert_eq!(call_spans.len(), expected_calls, "{execution:#}");
    assert!(
        calls.iter().all(|call| {
            call["caller"]["kind"] == "root_initializer" && call["target"]["kind"] == "callable"
        }),
        "{execution:#}"
    );
    let callees = calls
        .iter()
        .map(|call| {
            (
                call["target"]["key"].as_u64().unwrap(),
                call["target"]["owner"].as_u64().unwrap(),
            )
        })
        .collect::<BTreeSet<_>>();
    assert_eq!(callees.len(), 1, "{execution:#}");
}

#[test]
fn imported_binding_update_effects_survive_saved_analysis() {
    let source = "import { advance } from './peer.js'; const readLater = () => later; const result = advance(); const later = 1; console.log(result.before, result.after, result.state, readLater());\n";
    let chunks = [(
        "static/peer",
        "export function advance() { let state = 40; const before = state++; const after = ++state; return { before, after, state }; }",
    )];
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module("work", &[Member::new("readLater"), Member::new("result")]),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks),
    );
    let direct = staged.run(&["--effect-conflicts-jsonl"]);
    assert_ok(&direct.result, "direct imported update run");
    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let execution = &graph_node_for_binding(&graph, "result")["synchronous_execution"];
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
    let artifact = path(&staged.artifact).to_owned();
    let loaded = staged.run(&["--effect-conflicts-jsonl", "--analysis", &artifact]);
    assert_ok(&loaded.result, "imported update run from saved analysis");
    assert_same_invocation_trees(&direct, &loaded);
    for run in [&direct, &loaded] {
        assert_node_output(
            &run.output_root().join("app/static/app/entry.js"),
            "40 42 42 1\n",
            "",
        );
    }
}

#[test]
fn conditional_descriptor_body_effects_survive_saved_analysis() {
    let source = "import { wrap } from './peer.js'; const wrapped = wrap({ name: 'report', create() { return later(); }, get schema() { return later(); } }); var later = () => 42; console.log(wrapped.schema, wrapped.create());\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_descriptor_copy_factory("wrap", &["create"], &["create", "call"])
        .build()];
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("wrapped")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    let direct = staged.run(&["--effect-conflicts-jsonl"]);
    assert_ok(&direct.result, "direct conditional body run");
    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let execution = &graph_node_for_binding(&graph, "wrapped")["synchronous_execution"];
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
    assert!(
        execution["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|usage| usage["conclusion"] == "conditional_foreign_body"),
        "{execution:#}"
    );
    let artifact = path(&staged.artifact).to_owned();
    let loaded = staged.run(&["--effect-conflicts-jsonl", "--analysis", &artifact]);
    assert_ok(&loaded.result, "conditional body run from saved analysis");
    assert_same_invocation_trees(&direct, &loaded);
    for run in [&direct, &loaded] {
        assert_node_output(
            &run.output_root().join("app/static/app/entry.js"),
            "42 42\n",
            "",
        );
    }
}

#[test]
fn captured_cell_conflicts_keep_distinct_origins_through_saved_analysis() {
    let source = "function makeFirst(seed) { let state = seed; return { read: () => state, write: () => state = 42 }; }\nfunction makeSecond(seed) { let state = seed; return { read: () => state, write: () => state = 43 }; }\nconst first = makeFirst(1); const beforeFirst = first.read(); const afterFirst = first.write();\nconst second = makeSecond(2); const beforeSecond = second.read(); const afterSecond = second.write();\nconsole.log(beforeFirst, afterFirst, beforeSecond, afterSecond);\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "writer",
                &[
                    Member::new("makeFirst"),
                    Member::new("makeSecond"),
                    Member::new("first"),
                    Member::new("afterFirst"),
                    Member::new("second"),
                    Member::new("afterSecond"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let flags = ["--emit-on-rejection", "--effect-conflicts-jsonl"];
    let direct = staged.run(&flags);
    assert_eq!(
        direct.result.status.code(),
        Some(3),
        "{}",
        direct.result.stderr
    );
    let output = direct.output_root().join("reports/tree/static/app");
    let graph: Value = read_gzip_json(&output.join("dependency_graph.json.gz"));
    let rows = read_jsonl(&output.join("effect_conflicts.jsonl"));
    let mut origins = std::collections::BTreeSet::new();
    for (before, after) in [
        ("beforeFirst", "afterFirst"),
        ("beforeSecond", "afterSecond"),
    ] {
        let observer = &graph_node_for_binding(&graph, before)["statement_ordinal"];
        let writer = &graph_node_for_binding(&graph, after)["statement_ordinal"];
        let pair = rows
            .iter()
            .find(|row| {
                &row["runs_first"]["unit"] == writer && &row["runs_later"]["unit"] == observer
            })
            .unwrap_or_else(|| panic!("no captured-cell conflict for {after}/{before}: {rows:#?}"));
        assert_eq!(pair["havoc"], false, "{pair:#}");
        let cells = pair["cells"].as_array().unwrap();
        let cell = cells
            .iter()
            .find(|cell| {
                cell["cell"]["kind"] == "lexical_cell"
                    && !cell["first_write"].is_null()
                    && !cell["later_read"].is_null()
            })
            .unwrap_or_else(|| panic!("captured-cell effects must remain explicit: {pair:#}"));
        assert!(origins.insert(cell["cell"]["origin"].as_u64().unwrap()));
    }
    let artifact = path(&staged.artifact).to_owned();
    let loaded = staged.run(&[flags[0], flags[1], "--analysis", &artifact]);
    assert_eq!(loaded.result.status.code(), direct.result.status.code());
    assert_same_invocation_trees(&direct, &loaded);
}

#[test]
fn retained_effect_closure_survives_saved_analysis_without_marking_execution_complete() {
    assert_retained_effect_saved_parity("return seed;", "7\n", |next| format!("step{next}()"));
}

#[test]
fn composed_retained_effect_closure_survives_saved_analysis() {
    assert_retained_effect_saved_parity("return seed + 1;", "8\n", |next| {
        format!("(step{next}(), seed + 1)")
    });
}

fn assert_retained_effect_saved_parity(
    tail: &str,
    expected: &str,
    call_expression: impl Fn(usize) -> String,
) {
    let mut source = String::from("const seed = 7;\n");
    let mut members = vec![Member::new("seed"), Member::new("result")];
    for index in 0..66 {
        source.push_str(&format!(
            "function step{index}() {{ return {}; }}\n",
            call_expression(index + 1)
        ));
        members.push(Member::new(format!("step{index}").leak()));
    }
    source.push_str(&format!(
        "function step66() {{ {tail} }}\nconst result = step0();\nconsole.log(result);\n"
    ));
    members.push(Member::new("step66"));
    let (staged, _fixture) = staged(FixtureOpts::new(
        &source,
        vec![logical_module("all", &members)],
    ));
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct retained-effect run");
    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let execution = &graph_node_for_binding(&graph, "result")["synchronous_execution"];
    assert_eq!(execution["complete"], false, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "retained_effect_closure",
        "{execution:#}"
    );
    let analysis = path(&staged.artifact).to_owned();
    let loaded = staged.run(&["--analysis", analysis.as_str()]);
    assert_ok(&loaded.result, "retained-effect run from analysis");
    assert_same_invocation_trees(&direct, &loaded);
    assert_node_output(
        &loaded.output_root().join("app/static/app/entry.js"),
        expected,
        "",
    );
}

#[test]
fn lexical_effect_authority_and_fallback_survive_staged_artifact_round_trip() {
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction stop() { throw seed; }\nfunction unreachable() { return seed; }\nvar result = (() => { try { return (stop(), unreachable()); } catch (error) { return error; } })();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("stop"),
            Member::new("unreachable"),
            Member::new("result"),
        ],
        "operation_receipt",
        None,
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nconst values = [];\nfunction readSeed() { values.push(seed); return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("values"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
        None,
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
        None,
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = alias();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("later"),
            Member::new("readSeed"),
            Member::new("alias"),
            Member::new("result"),
        ],
        "ordered_root_initializer_uncovered",
        None,
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
        Some(2),
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nconst alias = readSeed;\nvar result = (alias(), alias());\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("alias"),
            Member::new("result"),
        ],
        "operation_receipt",
        Some(2),
    );
}

#[test]
fn captured_value_bindings_survive_saved_program_summary() {
    const PROVIDER: &str = "var captured = globalThis.runtimeSettings.state;\nexport function read() { return captured.value; }\n";
    const SETUP: &str = "globalThis.runtimeSettings = { state: { value: 41 } };\n";
    let input = ValueInputContract {
        source_match: serde_yaml::from_str(
            "match: var captured = globalThis.runtimeSettings.state;\nbindings: [captured]\n",
        )
        .expect("source selector"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: Sha256::digest(PROVIDER.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        expected_export: None,
        note: None,
    };
    let opts = FixtureOpts::new(
        "import './setup.js';\nimport { read } from './peer.js';\nconst result = read();\nconsole.log(result);\nexport { result };\n",
        vec![logical_module("all", &[Member::new("result")])],
    )
    .with_extra_chunks(&[("static/setup", SETUP), ("static/peer", PROVIDER)])
    .with_value_inputs("static/peer", vec![input]);
    let (staged, _fixture) = staged(opts);
    // The premise provider belongs to the static-request closure, but is not a
    // materialization root. Analyze must retain it from canonical source sites.
    let projection: Value = rmp_serde::from_slice(&fs::read(&staged.projection).unwrap()).unwrap();
    assert_eq!(projection["analysed_chunks"], json!(["static/app"]));
    assert_eq!(
        projection["source_sites"]["value_inputs"]["static/peer"][0],
        json!({"binding": "captured", "body_index": 0}),
    );
    assert!(projection["source_sites"]["source_sha256"]["static/peer"].is_string());
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct captured-value run");
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&saved.result, "saved captured-value run");
    assert_same_invocation_trees(&direct, &saved);
    assert_node_output(
        &saved.output_root().join("app/static/app/entry.js"),
        "41\n",
        "",
    );
}

fn assert_reexported_value_input_provider_is_in_saved_summary(bridge: &str) {
    const PROVIDER: &str = "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { ready: true });\nexport function read() { return captured.value; }\n";
    const SETUP: &str = "globalThis.runtimeSettings = { state: { value: 41 } };\n";
    let input = ValueInputContract {
        source_match: serde_yaml::from_str(
            "match: var captured = globalThis.runtimeSettings.state;\nbindings: [captured]\n",
        )
        .expect("source selector"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: Sha256::digest(PROVIDER.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        expected_export: None,
        note: None,
    };
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "import './setup.js';\nimport './bridge.js';\nconst result = 41;\nconsole.log(result, globalThis.runtimeSettings.state.ready);\nexport { result };\n",
            vec![logical_module("all", &[Member::new("result")])],
        )
        .with_extra_chunks(&[
            ("static/setup", SETUP),
            ("static/bridge", bridge),
            ("static/peer", PROVIDER),
        ])
        .with_value_inputs("static/peer", vec![input]),
    );

    // The source premise belongs to the request closure, while only the
    // consumer remains a selected analysis root.
    let projection: Value = rmp_serde::from_slice(&fs::read(&staged.projection).unwrap()).unwrap();
    assert_eq!(projection["analysed_chunks"], json!(["static/app"]));
    assert_eq!(
        projection["source_sites"]["value_inputs"]["static/peer"][0],
        json!({"binding": "captured", "body_index": 0}),
    );

    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct re-exported captured-value run");
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&saved.result, "saved re-exported captured-value run");
    assert_same_invocation_trees(&direct, &saved);

    let source_entry = staged.root().join("snapshot/static/app.js");
    let direct_entry = direct.output_root().join("app/static/app/entry.js");
    let saved_entry = saved.output_root().join("app/static/app/entry.js");
    for entry in [&source_entry, &direct_entry, &saved_entry] {
        assert_node_output(entry, "41 true\n", "");
    }

    for invocation in [&direct, &saved] {
        let program: Value = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/program_graph.json.gz"),
        );
        let witnesses = program["native_integrity_witnesses"]
            .as_array()
            .expect("native-integrity census");
        let peer_witnesses: Vec<_> = witnesses
            .iter()
            .filter(|witness| witness["chunk_id"] == "static/peer")
            .collect();
        assert_eq!(peer_witnesses.len(), 1, "{program:#}");
        let witness = peer_witnesses[0];
        assert_eq!(witness["operation"], "mutation_call", "{witness}");
        assert_eq!(witness["affected_roots"], json!([]), "{witness}");
        assert!(
            witness["value_input_refs"]
                .as_array()
                .is_some_and(|refs| refs.iter().any(|reference| {
                    reference["defining_chunk"] == "static/peer" && reference["input_index"] == 0
                })),
            "the peer writer must consume the selected captured-value input: {witness}"
        );
    }
}

#[test]
fn named_and_star_reexports_keep_provider_inputs_in_saved_analysis() {
    for bridge in [
        "export { read } from './peer.js';\n",
        "export * from './peer.js';\n",
    ] {
        assert_reexported_value_input_provider_is_in_saved_summary(bridge);
    }
}

fn assert_unproved_reexported_primitive_refusal_matches_saved(provider: &str) {
    let source = "import { limit as bytes } from './bridge.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\nconst computed = 4 * bytes + 256;\n\
         later = 42;\nconsole.log(computed, callback());\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "compute",
                    &[Member::new("callback"), Member::new("computed")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[
            ("static/bridge", "export { limit } from './provider.js';\n"),
            ("static/provider", provider),
        ]),
    );
    let artifact = fs::read(&staged.artifact).unwrap();
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);

    for (invocation, label) in [(&direct, "direct"), (&saved, "saved")] {
        assert_eq!(
            invocation.result.status.code(),
            Some(1),
            "the {label} run must refuse a re-exported primitive without an imported-read proof:\n{}",
            invocation.result.stderr
        );
        let report_root = invocation.output_root().join("reports/tree/static/app");
        for report in [
            "dependency_graph.json.gz",
            "atomic_group_conflicts.json",
            "effect_translation_validation.json",
            "candidate_relocation_assessment.json",
        ] {
            assert!(
                report_root.join(report).is_file(),
                "the {label} refusal must preserve {report}"
            );
        }
        let conflicts: Vec<Value> = serde_json::from_slice(
            &fs::read(report_root.join("atomic_group_conflicts.json")).unwrap(),
        )
        .unwrap();
        assert!(
            conflicts.iter().any(|conflict| {
                let claims = conflict["claims"].as_array().expect("conflict claims");
                let causes = conflict["causes"].as_array().expect("conflict causes");
                ["compute", "later"].iter().all(|module| {
                    claims
                        .iter()
                        .any(|claim| claim["module"].as_str() == Some(module))
                }) && causes
                    .iter()
                    .any(|cause| cause.as_str() == Some("eager_use"))
            }),
            "the {label} refusal must preserve the source ownership conflict: {conflicts:#?}"
        );
    }
    assert_same_invocation_trees(&direct, &saved);
    assert_eq!(fs::read(&staged.artifact).unwrap(), artifact);
}

#[test]
fn cyclic_and_unresolved_reexported_primitive_reads_refuse_with_saved_analysis() {
    for provider in [
        "import './app.js';\nexport const limit = 2;\n",
        "import './missing.js';\nexport const limit = 2;\n",
    ] {
        assert_unproved_reexported_primitive_refusal_matches_saved(provider);
    }
}

#[test]
fn provider_local_birth_certificate_survives_saved_program_summary() {
    const PROVIDER: &str = "export function make() { const record = { value: 1 }; const observed = record.value; record.value = 2; return record; }\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "import { make } from './peer.js';\nconst result = make();\nconsole.log(result.value);\n",
            vec![],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)]),
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct provider-local-birth run");

    let assert_sealed_certificate = |invocation: &StagedInvocation| {
        let graph: Value = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let execution = &graph_node_for_binding(&graph, "result")["synchronous_execution"];
        let receipts = execution["operation_support_receipts"]
            .as_array()
            .expect("execution retains provider operation-support diagnostics");
        for operation in ["own_data_read", "writable_own_data_store"] {
            assert!(
                receipts.iter().any(|receipt| {
                    receipt["operation"] == operation
                        && receipt["support"]["kind"] == "provider_local_birth"
                        && receipt["support"].get("origin").is_none()
                }),
                "{operation} must retain the address-free sealed certificate: {execution:#}"
            );
        }
    };
    assert_sealed_certificate(&direct);

    // `staged()` has already projected the fixture and written its
    // SavedProgramSummary analysis artifact through the dedicated analyzer.
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(
        &saved.result,
        "provider-local-birth run from saved analysis",
    );
    assert_same_invocation_trees(&direct, &saved);
    assert_node_output(
        &direct.output_root().join("app/static/app/entry.js"),
        "2\n",
        "",
    );
    assert_node_output(
        &saved.output_root().join("app/static/app/entry.js"),
        "2\n",
        "",
    );
    assert_sealed_certificate(&saved);
}

#[test]
fn accepted_split_matches_a_direct_run() {
    assert_staged_run_matches(split_with_pure_import(), &[]);
    assert_staged_run_matches(
        FixtureOpts::new(
            "const value = 1;\nconst closure = () => { throw value; };\nconst erased = (globalThis.marker = 1, 1);\nconst first = 1, second = 2;\nexport { value, closure, erased, first, second };\n",
            vec![logical_module(
                "all",
                &[
                    Member::new("value"),
                    Member::new("closure"),
                    Member::new("erased"),
                    Member::new("first"),
                    Member::new("second"),
                ],
            )],
        ),
        &[],
    );
}

#[test]
fn callback_factory_privacy_decision_matches_saved_analysis() {
    for initializer in [
        "factory(() => later)",
        "((factory)((() => later)))",
        "(0, factory(() => later))",
        "factory((globalThis.marker = 1, () => later))",
        "({get current() { return factory; }}).current(() => later)",
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             const wrapped = {initializer};\nconst later = 42;\nconsole.log(wrapped());\n"
        );
        assert_staged_run_matches(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[(
                "static/peer",
                "export function factory(callback) { return () => callback(); }\n",
            )]),
            &["--effect-conflicts-jsonl"],
        );
    }
}

#[test]
fn lexical_and_compound_class_heritage_match_saved_analysis() {
    for heritage in ["Core", "(globalThis.saved = Core, Core)", "holder.Base"] {
        let source = format!(
            "class Core {{ read() {{ return later; }} }}\n\
             const holder = {{ get Base() {{ return Core; }} }};\n\
             class Derived extends {heritage} {{ static snapshot = 1; }}\n\
             const later = 42;\nconsole.log(new Derived().read(), Derived.snapshot);\n"
        );
        assert_staged_run_matches(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "classes",
                    &[
                        Member::new("Core"),
                        Member::new("holder"),
                        Member::new("Derived"),
                        Member::new("later"),
                    ],
                )],
            ),
            &["--effect-conflicts-jsonl"],
        );
    }
}

#[test]
fn standalone_cli_and_dedicated_analyzer_write_identical_artifacts() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let dedicated = fs::read(&staged.artifact).unwrap();
    let standalone = StandaloneDebundle::install();
    assert_ok(
        &standalone.run(&[
            "analyze",
            "--projection",
            path(&staged.projection),
            "--tree-source-root",
            path(staged.root()),
            "--out",
            path(&staged.artifact),
        ]),
        "standalone analyze",
    );
    assert_eq!(dedicated, fs::read(&staged.artifact).unwrap());
    assert_ok(
        &standalone.run(&[
            "run",
            "--spec",
            path(&staged.spec),
            "--analysis",
            path(&staged.artifact),
        ]),
        "standalone staged run",
    );
}

#[test]
fn dedicated_projector_matches_flat_tree_and_resolved_member_inputs() {
    let (flat, _flat_fixture) = staged(FixtureOpts::new(
        "const value = 41;\nconsole.log(value);\n",
        vec![logical_module("all", &[Member::new("value")])],
    ));
    assert_projector_matches_flat(&flat, "flat");

    let tree = staged_tree(
        &[("main", "const value = 41;\nconsole.log(value);\n")],
        &[(
            "value.yaml",
            "members: [{selector: {binding: {name: value}}}]\n",
        )],
    );
    assert_projector_matches_tree(&tree);

    let (resolved_member, _resolved_member_fixture) = staged(native_member_fixture());
    let spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&resolved_member.spec).unwrap()).unwrap();
    assert_eq!(
        spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["resolved_member"]
            ["name"],
        "alias",
        "fixture must exercise the resolved-member native premise",
    );
    assert_projector_matches_flat(&resolved_member, "resolved-member");
}

#[test]
fn identity_mismatch_is_refused() {
    let (staged, _fixture) = staged(split_with_pure_import());
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "unmodified staged artifact",
    );
    let bytes = fs::read(&staged.artifact).unwrap();
    let (mut header, body) = staged_analysis::analysis_artifact::read_header(&bytes).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded = rmp_serde::to_vec_named(&header).unwrap();
    let mut corrupt = u32::try_from(encoded.len()).unwrap().to_le_bytes().to_vec();
    corrupt.extend(encoded);
    corrupt.extend(body);
    fs::write(&staged.artifact, corrupt).unwrap();
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused.result.status.success());
}

#[test]
fn source_mismatch_is_refused() {
    let (staged, _fixture) = staged(split_with_pure_import());
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "unmodified staged artifact",
    );
    let bytes = fs::read(&staged.artifact).unwrap();
    let (mut header, body) = staged_analysis::analysis_artifact::read_header(&bytes).unwrap();
    let source = header.chunk_sources.values_mut().next().unwrap();
    *source = "other-parser-input".to_string();
    let encoded = rmp_serde::to_vec_named(&header).unwrap();
    let mut corrupt = u32::try_from(encoded.len()).unwrap().to_le_bytes().to_vec();
    corrupt.extend(encoded);
    corrupt.extend(body);
    fs::write(&staged.artifact, corrupt).unwrap();
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused.result.status.success());
}

#[test]
fn staged_invocations_keep_outputs_and_refuse_reused_reports_destinations() {
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "const value = 1;\nconsole.log(value);\n",
            vec![logical_module("all", &[Member::new("value")])],
        )
        .with_browser_harness(),
    );

    let first = staged.run(&[]);
    assert_ok(&first.result, "first isolated staged run");
    let first_entry = first.output_root().join("app/static/app/entry.js");
    let first_entry_bytes = fs::read(&first_entry).unwrap();
    let first_spec_path = first
        .invocation_spec()
        .expect("flat run spec path")
        .to_path_buf();
    let first_spec_bytes = fs::read(&first_spec_path).unwrap();
    let first_harness = first
        .harness_root()
        .expect("fixture requests a browser harness");
    assert!(first_harness.is_dir());

    let second = staged.run(&[]);
    assert_ok(&second.result, "second isolated staged run");
    assert_ne!(first.output_root(), second.output_root());
    assert_ne!(first.invocation_spec(), second.invocation_spec());
    assert_ne!(first.harness_root(), second.harness_root());
    assert!(first_entry.is_file(), "the earlier run output was removed");
    assert_eq!(fs::read(&first_entry).unwrap(), first_entry_bytes);
    assert_eq!(fs::read(&first_spec_path).unwrap(), first_spec_bytes);
    assert!(second.harness_root().unwrap().is_dir());

    let original_artifact = fs::read(&staged.artifact).unwrap();
    let (mut header, body) =
        staged_analysis::analysis_artifact::read_header(&original_artifact).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded_header = rmp_serde::to_vec_named(&header).unwrap();
    let mut invalid_artifact = Vec::new();
    invalid_artifact.extend_from_slice(&u32::try_from(encoded_header.len()).unwrap().to_le_bytes());
    invalid_artifact.extend_from_slice(&encoded_header);
    invalid_artifact.extend_from_slice(body);
    fs::write(&staged.artifact, invalid_artifact).unwrap();

    let refused_first = staged.run(&["--analysis", path(&staged.artifact)]);
    let refused_second = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused_first.result.status.success());
    assert!(!refused_second.result.status.success());
    assert_ne!(refused_first.output_root(), refused_second.output_root());
    for refused in [&refused_first, &refused_second] {
        assert!(
            !refused.output_root().join("app").exists(),
            "refused invocation emitted an app under {}",
            refused.output_root().display(),
        );
        assert!(
            !refused
                .harness_root()
                .expect("fixture requests a browser harness")
                .exists(),
            "refused invocation emitted a browser harness under {}",
            refused.harness_root().unwrap().display(),
        );
    }
    assert_eq!(fs::read(&first_entry).unwrap(), first_entry_bytes);
    fs::write(&staged.artifact, original_artifact).unwrap();

    let reports_only = staged.root().join("reports-only-export");
    let report_args = [
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&reports_only),
    ];
    let exported = staged.run(&report_args);
    assert_ok(
        &exported.result,
        "reports-only export to an explicit destination",
    );
    let exported_tree = module_snapshot(&reports_only);
    assert!(!exported_tree.is_empty());

    let overwrite = staged.run(&report_args);
    assert!(!overwrite.result.status.success());
    assert!(
        overwrite
            .result
            .stderr
            .contains("Output directory is not empty"),
        "reports-only overwrite refusal changed: {}",
        overwrite.result.stderr
    );
    assert_eq!(module_snapshot(&reports_only), exported_tree);
}

#[test]
fn gate_rejected_split_matches_a_direct_run() {
    assert_staged_run_matches(rejected_split(), &["--emit-on-rejection"]);
}

#[test]
fn destructuring_split_refusal_matches_with_saved_facts() {
    let source = "const source = { left: 1, right: 2 };\n\
         const { left, right } = source;\n\
         console.log(left, right);\n\
         export { left, right };\n";
    // Build a matching analysis artifact while the source has one explicit
    // destructuring owner, then change only the module assignment to create the
    // conflicting request. Assignment edits do not change these saved facts.
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module("left_module", &[Member::new("left")])],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let right_module = serde_yaml::from_str(
        "members:\n  - name: right\n    selector:\n      binding:\n        name: right\n",
    )
    .unwrap();
    spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap()
        .insert(
            serde_yaml::Value::String("right_module".into()),
            right_module,
        );
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();
    let direct = staged.run(&[]);
    let from_artifact = staged.run(&["--analysis", path(&staged.artifact)]);

    assert!(
        !direct.result.status.success(),
        "direct run unexpectedly succeeded"
    );
    assert_eq!(
        direct.result.status.code(),
        from_artifact.result.status.code()
    );
    assert_same_invocation_trees(&direct, &from_artifact);
    for result in [&direct, &from_artifact] {
        assert!(
            result
                .result
                .stderr
                .contains("source/destination placement projection is unmappable")
                && result
                    .result
                    .stderr
                    .contains("OwnedBindingDestinationMismatch")
                && result
                    .result
                    .stderr
                    .contains("DestructuringDestinationSplit")
                && result.result.stderr.contains("left_module")
                && result.result.stderr.contains("right_module")
                && result.result.stderr.contains("unresolved checks: {}"),
            "expected the common typed placement refusal with complete destinations in stderr:\n{}",
            result.result.stderr
        );
    }
}

#[test]
fn nested_var_split_refusal_matches_with_saved_facts() {
    let source = "try { var left = 10; var right = 20; } catch (e) {}\n\
console.log(left, right);\nexport { left, right };\n";
    // Save analysis while both nested-var bindings have one coherent owner,
    // then change only their module assignments to create a split request.
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module(
            "left_module",
            &[Member::new("left"), Member::new("right")],
        )],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap();
    modules.insert(
        serde_yaml::Value::String("left_module".into()),
        serde_yaml::from_str(
            "members:\n  - name: left\n    selector:\n      binding:\n        name: left\n",
        )
        .unwrap(),
    );
    modules.insert(
        serde_yaml::Value::String("right_module".into()),
        serde_yaml::from_str(
            "members:\n  - name: right\n    selector:\n      binding:\n        name: right\n",
        )
        .unwrap(),
    );
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();

    let direct = staged.run(&[]);
    let from_artifact = staged.run(&["--analysis", path(&staged.artifact)]);

    assert!(
        !direct.result.status.success(),
        "direct run unexpectedly succeeded"
    );
    assert_eq!(
        direct.result.status.code(),
        from_artifact.result.status.code()
    );
    assert_same_invocation_trees(&direct, &from_artifact);
    for result in [&direct, &from_artifact] {
        assert!(
            result.result.stderr.contains("NestedVarDestinationSplit")
                && result.result.stderr.contains("left_module")
                && result.result.stderr.contains("right_module"),
            "expected the shared typed nested-var placement refusal in stderr:\n{}",
            result.result.stderr
        );
    }
}

#[test]
fn composed_global_sites_and_dependencies_round_trip() {
    assert_staged_run_matches(
        composed_global_access_fixture(),
        &["--effect-conflicts-jsonl", "--emit-on-rejection"],
    );
}

/// Renaming a logical module is an assignment edit: the projection keeps its
/// bytes and the artifact computed before the edit serves the edited spec.
#[test]
fn assignment_edit_keeps_the_projection_and_the_artifact() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .expect("chunk modules");
    let module = modules.remove("mod_a").expect("mod_a");
    modules.insert("mod_renamed".into(), module);
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("edited.projection");
    assert_ok(
        &staged.project(&edited),
        "spec project-analysis after the edit",
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap()
    );
    let artifact = path(&staged.artifact).to_owned();
    assert_ok(
        &staged.run(&["--analysis", &artifact]).result,
        "run --analysis after the edit",
    );
}

#[test]
fn graph_options_survive_the_source_only_analysis_boundary() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const a = (globalThis.left = 1, 1);\nconst b = (globalThis.right = 2, 2);\nconsole.log(a, b);\n",
        vec![
            logical_module("first", &[Member::new("a")]),
            logical_module("second", &[Member::new("b")]),
        ],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"] =
        serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    for enabled in [true, false] {
        spec["chunk_analysis_options"]["static/app"]["dataflow_aware_s_chain"] = enabled.into();
        write_yaml_file(&staged.spec, &spec);
        let edited = staged.root().join("edited.projection");
        assert_ok(&staged.project(&edited), "project changed graph options");
        assert!(
            fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
            "graph-only setting changed analysis identity"
        );
        let reused = staged.run(&["--analysis", path(&staged.artifact), "--emit-on-rejection"]);
        assert_ok(&reused.result, "source-order candidate in both graph modes");
        let report: Value = serde_json::from_slice(
            &fs::read(
                reused
                    .output_root()
                    .join("reports/tree/static/app/candidate_relocation_assessment.json"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(report["status"], "accepted_within_checked_scope");
        assert_eq!(report["decision"]["scope"], "no_reordering");
        assert_eq!(report["swapped_pairs"], 0);
        assert_node_output(
            &reused.output_root().join("app/static/app/entry.js"),
            "1 2\n",
            "",
        );
        let graph: DependencyGraphReport = read_gzip_json(
            &reused
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let a = debundle_e2e_graph_support::source_unit_for_binding(&graph, "a");
        let b = debundle_e2e_graph_support::source_unit_for_binding(&graph, "b");
        let has_edge = graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && ((edge.source == a && edge.target == b)
                    || (edge.source == b && edge.target == a))
        });
        assert_eq!(has_edge, !enabled, "dataflow graph setting was not applied");
        let direct = staged.run(&["--emit-on-rejection"]);
        assert_eq!(direct.result.status.code(), reused.result.status.code());
        assert_same_invocation_trees(&reused, &direct);
    }
}

#[test]
fn trusted_dataflow_summaries_reach_staged_facts_and_remain_in_the_projection() {
    let source = r#"class Holder1 { constructor() { this.kind = "h1"; } }
class Holder2 { constructor() { this.kind = "h2"; } }
const instA = new Holder1();
const instB = new Holder2();
console.log(instA.kind, instB.kind);
export { Holder1, Holder2, instA, instB };
"#;
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module("mod_a", &[Member::new("Holder1"), Member::new("instA")]),
                logical_module("mod_b", &[Member::new("Holder2"), Member::new("instB")]),
            ],
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact), "--emit-on-rejection"]);
    assert_eq!(
        reused.result.status.code(),
        Some(0),
        "{}",
        reused.result.stderr
    );
    assert_accepted_chunk_relocation(&reused, "static/app", json!([0, 2, 1, 3, 4, 5]), 1);
    let reused_graph: Value = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    assert_class_declaration_receipt(&reused_graph, "Holder1", 0, json!({"start": 1, "end": 54}));
    assert_class_declaration_receipt(
        &reused_graph,
        "Holder2",
        1,
        json!({"start": 55, "end": 108}),
    );
    assert_class_construction_receipt(&reused_graph, "instA", 2, 1, 0);
    assert_class_construction_receipt(&reused_graph, "instB", 3, 55, 1);
    assert_node_output(
        &reused.output_root().join("app/static/app/entry.js"),
        "h1 h2\n",
        "",
    );

    let graph: DependencyGraphReport = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let inst_a = debundle_e2e_graph_support::source_unit_for_binding(&graph, "instA");
    let inst_b = debundle_e2e_graph_support::source_unit_for_binding(&graph, "instB");
    assert!(
        graph.edges.iter().all(|edge| {
            edge.edge_kind != DepKind::Sequenced
                || !((edge.source == inst_a && edge.target == inst_b)
                    || (edge.source == inst_b && edge.target == inst_a))
        }),
        "trusted syntactic summaries should keep disjoint constructor calls unsequenced"
    );
    let direct = staged.run(&["--emit-on-rejection"]);
    assert_eq!(
        direct.result.status.code(),
        Some(0),
        "{}",
        direct.result.stderr
    );
    assert_accepted_chunk_relocation(&direct, "static/app", json!([0, 2, 1, 3, 4, 5]), 1);
    let direct_graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    assert_class_declaration_receipt(&direct_graph, "Holder1", 0, json!({"start": 1, "end": 54}));
    assert_class_declaration_receipt(
        &direct_graph,
        "Holder2",
        1,
        json!({"start": 55, "end": 108}),
    );
    assert_class_construction_receipt(&direct_graph, "instA", 2, 1, 0);
    assert_class_construction_receipt(&direct_graph, "instB", 3, 55, 1);
    assert_same_invocation_trees(&reused, &direct);
    assert_node_output(
        &direct.output_root().join("app/static/app/entry.js"),
        "h1 h2\n",
        "",
    );

    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["trusted_dataflow_summaries"] = false.into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("without-trusted-summaries.projection");
    assert_ok(
        &staged.project(&edited),
        "project without trusted dataflow summaries",
    );
    assert_ne!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap(),
        "trusted dataflow assumptions must remain in analysis identity"
    );
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(
        !refused.result.status.success(),
        "changed trusted dataflow assumptions reused saved facts"
    );
    assert!(
        refused.result.stderr.contains("another projection"),
        "{}",
        refused.result.stderr
    );
}

#[test]
fn graph_only_chunk_options_keep_analysis_identity_and_evidence() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const effects = { left: 0, right: 0 };\nglobalThis.record = (key) => { effects[key] = 1; };\nconst alpha = (globalThis.record(\"left\"), \"A\");\nconst beta = (globalThis.record(\"right\"), \"B\");\nconsole.log(alpha, beta, effects.left, effects.right);\nexport { alpha, beta };\n",
        vec![logical_module_with_anon(
            "all",
            &[Member::new("effects"), Member::new("alpha"), Member::new("beta")],
            &["globalThis.record = (key) => { effects[key] = 1; };"],
        )],
    ).with_unassigned_mode(unassigned_mode_inline()));
    let original = fs::read(&staged.projection).unwrap();
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"] =
        serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("empty-chunk-options.projection");
    assert_ok(&staged.project(&edited), "project empty chunk options");
    assert_eq!(
        original,
        fs::read(&edited).unwrap(),
        "an empty authored per-chunk entry changed source-analysis identity"
    );
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "reuse analysis after adding an empty chunk entry",
    );

    spec["chunk_analysis_options"]["static/app"]["commuting_statements"] =
        serde_yaml::to_value(vec![commuting_pair(
            member_source_unit("all", "alpha"),
            member_source_unit("all", "beta"),
        )])
        .unwrap();
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("commuting-options.projection");
    assert_ok(
        &staged.project(&edited),
        "project graph-only commuting option",
    );
    assert_eq!(
        original,
        fs::read(&edited).unwrap(),
        "a commuting premise changed source-analysis identity"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse analysis with commuting option");
    let graph: DependencyGraphReport = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    assert_eq!(
        graph.commuting_statements.len(),
        1,
        "the graph-only premise was not applied to the reused analysis"
    );
}

fn native_member_fixture() -> FixtureOpts<'static> {
    const SOURCE: &str = "var alias = Object.defineProperty;\nvar other = Object.getOwnPropertyDescriptor;\nconsole.log(typeof alias);\n";
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
            operation: NativeOperationKind::ObjectOwnDataFunctionRead,
            audited_source_sha256: Sha256::digest(SOURCE.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect(),
            note: Some("The selected native read is audited.".to_owned()),
        }],
    )
}

#[test]
fn resolved_member_layout_edits_keep_analysis_identity() {
    let (staged, _fixture) = staged(native_member_fixture());
    let original_artifact = fs::read(&staged.artifact).unwrap();
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap();
    let mut module = modules.remove("native").unwrap();
    module["members"][0]["name"] = "chosen".into();
    modules.insert("renamed".into(), module);
    let input = &mut spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0];
    input["resolved_member"]["module"] = "renamed".into();
    input["resolved_member"]["name"] = "chosen".into();
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("edited.projection");
    assert_ok(&staged.project(&edited), "project renamed native member");
    assert!(
        fs::read(&staged.projection).unwrap() == fs::read(&edited).unwrap(),
        "renaming the module and public member changed the analysis projection"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse native member analysis");
    let graph: Value = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let evidence = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .into_iter()
                .flatten()
        })
        .map(|input| &input["evidence"])
        .next()
        .expect("the native premise must be consumed");
    assert_eq!(
        evidence["selector"]["resolved_member"],
        json!({"module": "renamed", "name": "chosen"})
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct renamed native member run");
    assert_same_invocation_trees(&reused, &direct);
    fs::copy(edited, &staged.projection).unwrap();
    assert_ok(&staged.analyze(), "reanalyze renamed native member");
    assert!(
        original_artifact == fs::read(&staged.artifact).unwrap(),
        "layout edit changed analysis artifact bytes"
    );
}

#[test]
fn native_source_match_notes_keep_analysis_identity() {
    let mut opts = native_member_fixture();
    opts.native_operation_inputs[0].1[0].selector = serde_yaml::from_str(
        "source_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\n  note: Original selector rationale.\n",
    ).unwrap();
    let (staged, _fixture) = staged(opts);
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["source_match"]["note"] =
        "Revised selector rationale.".into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("edited.projection");
    assert_ok(
        &staged.project(&edited),
        "project revised selector rationale",
    );
    assert!(
        fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
        "selector rationale changed analysis identity"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse analysis after selector note edit");
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct run after selector note edit");
    assert_same_invocation_trees(&reused, &direct);
}

#[test]
fn analyze_refuses_provider_source_changed_after_site_projection() {
    let fixture = write_validate_fixture_spec(native_member_fixture());
    let root = fixture.spec_path.parent().unwrap().to_path_buf();
    let staged = Staged {
        spec: fixture.spec_path.clone(),
        projection: root.join("analysis.projection"),
        artifact: root.join("analysis.dba"),
    };
    assert_ok(
        &staged.project(&staged.projection),
        "project native source sites",
    );

    let provider_source = root.join("snapshot/static/app.js");
    let mut changed_source = fs::read_to_string(&provider_source).unwrap();
    changed_source.push_str("// changed after source-site projection\n");
    fs::write(&provider_source, changed_source).unwrap();

    let refused = staged.analyze();
    assert_eq!(refused.status.code(), Some(1), "{}", refused.stderr);
    assert!(
        refused
            .stderr
            .contains("analysis source-site provider static/app has stale parser input"),
        "expected source-site provenance refusal:\n{}",
        refused.stderr
    );
    assert!(
        !staged.artifact.exists(),
        "stale source sites wrote an artifact"
    );
}

#[test]
fn resolved_member_retargeting_refuses_saved_analysis() {
    let (staged, _fixture) = staged(native_member_fixture());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["resolved_member"]
        ["name"] = "other".into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("edited.projection");
    assert_ok(&staged.project(&edited), "project retargeted native member");
    assert_ne!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap()
    );
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(
        !refused.result.status.success(),
        "retargeted native premise reused old facts"
    );
    assert!(
        refused.result.stderr.contains("another projection"),
        "{}",
        refused.result.stderr
    );
    fs::copy(edited, &staged.projection).unwrap();
    assert_ok(&staged.analyze(), "analyze retargeted native member");
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "run retargeted native member",
    );
}

#[test]
fn resolved_member_premise_edits_refuse_saved_analysis() {
    let (staged, _fixture) = staged(native_member_fixture());
    let original: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    for (field, value) in [
        ("operation", "map_empty".to_owned()),
        ("audited_source_sha256", "0".repeat(64)),
    ] {
        let mut spec = original.clone();
        spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0][field] =
            value.into();
        write_yaml_file(&staged.spec, &spec);
        let refused = staged.run(&["--analysis", path(&staged.artifact)]);
        assert!(
            !refused.result.status.success(),
            "changed {field} reused old facts"
        );
        assert!(
            refused.result.stderr.contains("another projection"),
            "{}",
            refused.result.stderr
        );
    }
}

/// An edit analysis reads invalidates the artifact; the run names the field.
#[test]
fn analysis_edit_refuses_the_stale_artifact() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_export_purity"]["static/vendorlib"]["pure_exports"] =
        serde_yaml::to_value(["wrap"]).unwrap();
    write_yaml_file(&staged.spec, &spec);

    let artifact = path(&staged.artifact).to_owned();
    let refused = staged.run(&["--analysis", &artifact]);
    assert!(
        !refused.result.status.success(),
        "stale artifact was accepted"
    );
    assert!(
        refused
            .result
            .stderr
            .contains("chunk_export_purity.static/vendorlib"),
        "refusal does not name the differing field:\n{}",
        refused.result.stderr
    );
}

#[test]
fn full_effect_stream_matches_between_direct_and_staged_runs() {
    assert_staged_run_matches(split_with_pure_import(), &["--effect-conflicts-jsonl"]);
    assert_staged_run_matches(
        rejected_split(),
        &["--effect-conflicts-jsonl", "--emit-on-rejection"],
    );
}

#[test]
fn root_normal_completion_control_survives_saved_analysis() {
    let source = "let state = 0;\n\
function initialize() {\n\
  state = 1;\n\
  try { throw 'caught'; state = 99; } catch (error) { state = state + 1; }\n\
  return state;\n\
}\n\
const result = initialize();\n\
const independent = 2;\n\
console.log(result, state, independent);\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![logical_module("independent", &[Member::new("independent")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct root-normal control run");
    let report: Value = read_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/effect_translation_validation.json"),
    );
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");

    let artifact = path(&staged.artifact).to_owned();
    let restored = staged.run(&["--analysis", artifact.as_str()]);
    assert_ok(&restored.result, "saved root-normal control run");
    assert_same_invocation_trees(&direct, &restored);
    for invocation in [&direct, &restored] {
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "2 2 2\n",
            "",
        );
    }
}

#[test]
fn outward_throw_control_refusal_survives_saved_analysis() {
    let source = "console.log('prefix');\n\
function fail() { throw 'sentinel'; }\n\
const result = fail();\n\
const suffix = (console.log('suffix'), 1);\n\
export { result, suffix };\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![logical_module("suffix", &[Member::new("suffix")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let args = ["--emit-on-rejection", "--effect-conflicts-jsonl"];
    let direct = staged.run(&args);
    assert_eq!(
        direct.result.status.code(),
        Some(3),
        "{}",
        direct.result.stderr
    );
    let rows = read_jsonl(
        &direct
            .output_root()
            .join("reports/tree/static/app/effect_conflicts.jsonl"),
    );
    assert!(
        rows.iter().any(|row| {
            row["cells"].as_array().is_some_and(|cells| {
                cells.iter().any(|cell| {
                    cell["cell"]["kind"] == "execution_progress"
                        && cell["observed_conflict"] == true
                })
            })
        }),
        "the exact outward throw retains observed control evidence: {rows:#?}"
    );

    let artifact = path(&staged.artifact).to_owned();
    let mut saved_args = args.to_vec();
    saved_args.extend(["--analysis", artifact.as_str()]);
    let restored = staged.run(&saved_args);
    assert_eq!(
        restored.result.status.code(),
        Some(3),
        "{}",
        restored.result.stderr
    );
    assert_same_invocation_trees(&direct, &restored);
    for invocation in [&direct, &restored] {
        assert_node_error_with_stdout(
            &invocation.output_root().join("app/static/app/entry.js"),
            "sentinel",
            "suffix\nprefix\n",
        );
    }
}

#[test]
fn own_data_descriptor_support_report_matches_direct_and_saved_analysis() {
    let source = "const target = { value: 7 };\nconst descriptors = Object.getOwnPropertyDescriptors({ value: 42 });\nconst observed = target.value;\nconst applied = Object.defineProperties(target, descriptors);\nconsole.log(observed, applied.value);\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "a_mutator",
                &[
                    Member::new("target"),
                    Member::new("descriptors"),
                    Member::new("applied"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let run_args = ["--emit-on-rejection", "--effect-conflicts-jsonl"];
    let direct = staged.run(&run_args);
    assert_eq!(
        direct.result.status.code(),
        Some(3),
        "{}",
        direct.result.stderr
    );
    let direct_report = candidate_relocation_report(&direct, "static/app");
    assert_eq!(
        direct_report["decision"]["status"], "conflict",
        "{direct_report:#}"
    );
    assert!(
        direct_report["swapped_pairs"].as_u64().unwrap() > 0,
        "{direct_report:#}"
    );
    let assert_support_receipt = |invocation: &StagedInvocation| {
        let rows = read_jsonl(
            &invocation
                .output_root()
                .join("reports/tree/static/app/effect_conflicts.jsonl"),
        );
        assert!(
            rows.iter().any(|pair| {
                pair["cells"].as_array().is_some_and(|cells| {
                    cells.iter().any(|cell| {
                        cell["cell"]["kind"] == "object_shape"
                            && cell["later_operation_support_reads"]
                                .as_array()
                                .is_some_and(|receipts| {
                                    receipts.iter().any(|receipt| {
                                        receipt["operation"] == "own_data_read"
                                            && receipt["support"]["kind"] == "pooled_object_shape"
                                            && receipt["support"]["origin"]
                                                == cell["cell"]["origin"]
                                    })
                                })
                    })
                })
            }),
            "serialized conflict rows must retain the projected own-data support: {rows:#?}"
        );
    };
    assert_support_receipt(&direct);

    let artifact = path(&staged.artifact).to_owned();
    let mut saved_args = run_args.to_vec();
    saved_args.extend(["--analysis", artifact.as_str()]);
    let saved = staged.run(&saved_args);
    assert_eq!(
        saved.result.status.code(),
        Some(3),
        "{}",
        saved.result.stderr
    );
    assert_same_invocation_trees(&direct, &saved);
    assert_support_receipt(&saved);
}

fn candidate_relocation_report(invocation: &StagedInvocation, chunk: &str) -> Value {
    read_json(&invocation.output_root().join(format!(
        "reports/tree/{chunk}/candidate_relocation_assessment.json"
    )))
}

fn assert_accepted_chunk_relocation(
    invocation: &StagedInvocation,
    chunk: &str,
    emitted_order: Value,
    swapped_pairs: u64,
) {
    let report = candidate_relocation_report(invocation, chunk);
    assert_eq!(
        report["status"], "accepted_within_checked_scope",
        "{report:#}"
    );
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{report:#}"
    );
    assert_eq!(report["emitted_order"], emitted_order, "{report:#}");
    assert_eq!(report["swapped_pairs"], swapped_pairs, "{report:#}");
    assert_eq!(report["counts"]["pairs"], 0, "{report:#}");
}

fn assert_potential_suppression_chunk_relocation(
    invocation: &StagedInvocation,
    chunk: &str,
    expected_status: &str,
) {
    assert_eq!(
        invocation.result.status.code(),
        Some(3),
        "expected rejected inspection output for {chunk}: {}",
        invocation.result.stderr
    );
    let report = candidate_relocation_report(invocation, chunk);
    assert_eq!(report["decision"]["uncovered_havoc"], true, "{report:#}");
    let effects = read_json(&invocation.output_root().join(format!(
        "reports/tree/{chunk}/effect_translation_validation.json"
    )));
    assert_potential_suppression_rejection(&report, &effects, expected_status);
}

fn assert_class_declaration_receipt(
    graph: &Value,
    binding: &str,
    owner: u64,
    declaration_span: Value,
) {
    let node = graph_node_for_binding(graph, binding);
    assert_eq!(node["statement_ordinal"], owner, "{node:#}");
    let execution = &node["synchronous_execution"];
    assert_eq!(execution["complete"], true, "{execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(authority["authority"], "operation_receipt", "{execution:#}");
    let receipt = &authority["receipt"];
    assert_eq!(receipt["root_kind"], "class_declaration", "{receipt:#}");
    assert_eq!(receipt["binding"], binding, "{receipt:#}");
    assert_eq!(receipt["owner"], owner, "{receipt:#}");
    assert_eq!(receipt["declaration_span"], declaration_span, "{receipt:#}");
    assert!(receipt.get("initializer_span").is_none(), "{receipt:#}");
    assert_eq!(receipt["calls"], json!([]), "{receipt:#}");
}

fn assert_class_construction_receipt(
    graph: &Value,
    binding: &str,
    owner: u64,
    class: u64,
    class_owner: u64,
) {
    let node = graph_node_for_binding(graph, binding);
    assert_eq!(node["statement_ordinal"], owner, "{node:#}");
    let execution = &node["synchronous_execution"];
    assert_eq!(execution["complete"], true, "{execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(authority["authority"], "operation_receipt", "{execution:#}");
    let receipt = &authority["receipt"];
    assert_eq!(receipt["root_kind"], "binding_initializer", "{receipt:#}");
    assert_eq!(receipt["binding"], binding, "{receipt:#}");
    assert_eq!(receipt["owner"], owner, "{receipt:#}");
    assert!(receipt["initializer_span"].is_object(), "{receipt:#}");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 1, "{receipt:#}");
    assert_eq!(calls[0]["caller"]["kind"], "root_initializer", "{calls:#?}");
    assert_eq!(calls[0]["caller"]["owner"], owner, "{calls:#?}");
    assert_eq!(
        calls[0]["call_span"], receipt["initializer_span"],
        "{calls:#?}"
    );
    assert_eq!(
        calls[0]["target"],
        json!({"kind": "construction", "class": class, "owner": class_owner}),
        "{calls:#?}"
    );
    assert_eq!(calls[0]["dependencies"], json!({}), "{calls:#?}");
}

fn assert_export_filter_changes_only_sinks(
    export_args: &[&str],
    filter_flag: &str,
    filename: &str,
) {
    let extra_modules = [(
        "static/peer",
        vec![logical_module("provider", &[Member::new("value")])],
    )];
    let opts = FixtureOpts::new(
        "import { value } from './peer.js';\nconst first = value + 1;\nconst second = 2;\nconst tail = second;\nconsole.log(first, tail);\nexport { first, second, tail };\n",
        vec![logical_module("outer", &[Member::new("first"), Member::new("tail")]), logical_module("inner", &[Member::new("second")])],
    ).with_extra_chunks(&[("static/peer", "globalThis.externalMarker = 1;\nexport const value = 1;\n")]).with_extra_chunk_logical_modules(&extra_modules);
    let (staged, _fixture) = staged(opts);
    let mut flags = export_args.to_vec();
    flags.push("--emit-on-rejection");
    let all = staged.run(&flags);
    assert_export_filter_staged_statuses(&all);
    flags.extend([filter_flag, "static/app"]);
    let filtered = staged.run(&flags);
    assert_export_filter_staged_statuses(&filtered);
    let without_streams = |invocation: &StagedInvocation| {
        invocation_tree(invocation)
            .into_iter()
            .filter(|(path, _)| path.file_name().is_none_or(|name| name != filename))
            .collect::<BTreeMap<_, _>>()
    };
    // Normalize only invocation paths and command provenance. All graphs,
    // summary counts, native census and inspection JS must match.
    assert_eq!(without_streams(&all), without_streams(&filtered));
    let main_stream = PathBuf::from(format!("reports/tree/static/app/{filename}"));
    let peer_stream = PathBuf::from(format!("reports/tree/static/peer/{filename}"));
    assert_eq!(
        fs::read(all.output_root().join(&main_stream)).unwrap(),
        fs::read(filtered.output_root().join(&main_stream)).unwrap()
    );
    assert!(all.output_root().join(&peer_stream).exists());
    assert!(!filtered.output_root().join(&peer_stream).exists());
    let artifact = path(&staged.artifact).to_owned();
    let mut loaded_flags = flags.to_vec();
    loaded_flags.extend(["--analysis", &artifact]);
    let filtered_loaded = staged.run(&loaded_flags);
    assert_export_filter_staged_statuses(&filtered_loaded);
    assert_same_invocation_trees(&filtered, &filtered_loaded);
    let mut all_selected = export_args.to_vec();
    all_selected.push("--emit-on-rejection");
    all_selected.extend([filter_flag, "static/app", filter_flag, "static/peer"]);
    let all_selected = staged.run(&all_selected);
    assert_export_filter_staged_statuses(&all_selected);
    assert_same_invocation_trees(&all, &all_selected);
}

fn assert_export_filter_staged_statuses(invocation: &StagedInvocation) {
    assert_eq!(
        invocation.result.status.code(),
        Some(3),
        "{}",
        invocation.result.stderr
    );
    assert_accepted_chunk_relocation(invocation, "static/app", json!([2, 1, 3, 0, 4, 5]), 4);
    assert_potential_suppression_chunk_relocation(invocation, "static/peer", "blocked_by_effect");
    let peer_graph: Value = read_gzip_json(
        &invocation
            .output_root()
            .join("reports/tree/static/peer/dependency_graph.json.gz"),
    );
    let global_write = peer_graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["statement_ordinal"] == 0)
        .unwrap();
    let execution = &global_write["synchronous_execution"];
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_expression_uncovered",
        "the peer global write remains outside operation coverage: {execution:#}"
    );
    assert!(
        execution["precision_losses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|loss| {
                loss["reason"]["code"] == "unsupported_operation"
                    && loss["reason"]["operation"] == "property_write"
            }),
        "{execution:#}"
    );
    let conflicts = invocation
        .output_root()
        .join("reports/tree/static/peer/effect_conflicts.jsonl");
    if conflicts.is_file() {
        let rows = read_jsonl(&conflicts);
        let conflict = rows
            .iter()
            .find(|row| row["runs_first"]["unit"] == 1 && row["runs_later"]["unit"] == 0)
            .unwrap_or_else(|| {
                panic!("the exported value still crosses the peer global write: {rows:#?}")
            });
        assert_eq!(conflict["runs_first"]["module"], "provider", "{conflict:#}");
        assert_eq!(
            conflict["runs_later"]["module"], "anon_residual_sentinel",
            "{conflict:#}"
        );
        assert_eq!(conflict["havoc"], true, "{conflict:#}");
        assert_eq!(conflict["disposition"], "uncovered", "{conflict:#}");
    }
}

#[test]
fn export_filter_changes_only_jsonl_sinks_and_keeps_staged_facts() {
    assert_export_filter_changes_only_sinks(
        &["--effect-conflicts-jsonl"],
        "--effect-conflicts-chunk",
        "effect_conflicts.jsonl",
    );
}

#[test]
fn full_report_filter_changes_only_sinks_and_keeps_staged_facts() {
    assert_export_filter_changes_only_sinks(
        &["--full-report", "translation-validation"],
        "--full-report-chunk",
        "translation_validation.full.jsonl",
    );
}

#[test]
fn full_translation_report_matches_direct_and_staged_runs() {
    assert_staged_run_matches(
        rejected_split(),
        &[
            "--emit-on-rejection",
            "--full-report",
            "translation-validation",
        ],
    );
}

#[test]
fn full_reentry_export_matches_staged_and_filters_only_sinks() {
    assert_staged_run_matches(
        split_with_pure_import(),
        &["--full-report", "export-reentry"],
    );
    assert_staged_run_matches(
        rejected_split(),
        &["--emit-on-rejection", "--full-report", "export-reentry"],
    );
    assert_export_filter_changes_only_sinks(
        &["--full-report", "export-reentry"],
        "--full-report-chunk",
        "export_reentry.full.json",
    );
}

#[test]
fn default_reports_are_graph_free_and_full_export_reuses_the_analysis() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const first = 1;\nconst second = first + 1;\nconsole.log(second);\nexport { first, second };\n",
        vec![
            logical_module("first", &[Member::new("first")]),
            logical_module("second", &[Member::new("second")]),
        ],
    ));
    let suppressed_graphs = staged.run(&["--dry-run"]);
    assert_eq!(
        suppressed_graphs.result.status.code(),
        Some(1),
        "{}",
        suppressed_graphs.result.stderr
    );
    assert!(
        suppressed_graphs
            .result
            .stderr
            .contains("--graphs requested"),
        "{}",
        suppressed_graphs.result.stderr
    );
    let small = staged.run_without_graphs(&[]);
    assert_ok(&small.result, "small default run");
    assert_node_output(
        &small.output_root().join("app/static/app/entry.js"),
        "2\n",
        "",
    );
    let small_files = tree(&small.output_root());
    assert!(!small_files.keys().any(|p| {
        p.file_name()
            .is_some_and(|n| n == "dependency_graph.json.gz" || n == "program_graph.json.gz")
    }));
    let full_run = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&full_run.result, "full graphs from shared analysis");
    let full = tree(&full_run.output_root());
    let without_graphs = |files: BTreeMap<PathBuf, Vec<u8>>| {
        files
            .into_iter()
            .filter_map(|(p, mut bytes)| {
                if p.file_name().is_some_and(|n| {
                    n == "dependency_graph.json.gz" || n == "program_graph.json.gz"
                }) {
                    return None;
                }
                if p.file_name().is_some_and(|n| n == "graph_summary.json") {
                    let mut summary: Value = serde_json::from_slice(&bytes).unwrap();
                    summary.as_object_mut().unwrap().remove("full_graphs");
                    bytes = serde_json::to_vec(&summary).unwrap();
                }
                Some((p, bytes))
            })
            .collect::<BTreeMap<_, _>>()
    };
    assert_eq!(without_graphs(small_files), without_graphs(full));
    let small_export = staged.root().join("small-report-export");
    assert_ok(
        &command(&[
            "run",
            "--spec",
            path(&staged.spec),
            "--analysis",
            path(&staged.artifact),
            "--reports-only",
            path(&small_export),
        ]),
        "report-only selection without full graphs",
    );
    assert!(!small_export.join("app").exists());
    assert!(
        !small_export
            .join("reports/tree/program_graph.json.gz")
            .exists()
    );
    assert!(
        !small_export
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
    let export = staged.root().join("graph-export");
    assert_ok(
        &staged
            .run(&[
                "--analysis",
                path(&staged.artifact),
                "--reports-only",
                path(&export),
            ])
            .result,
        "reports-only full export",
    );
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
    let exported = tree(&export.join("reports/tree"));
    let program_reports = tree(&full_run.output_root().join("reports/tree"));
    // Program emission adds per-file metrics. Every report-only artifact is
    // identical to its canonical full-run counterpart after provenance removal.
    for (path, bytes) in exported {
        assert_eq!(
            Some(&bytes),
            program_reports.get(&path),
            "{}",
            path.display()
        );
    }
    let summary: Value = serde_json::from_slice(
        &fs::read(export.join("reports/tree/static/app/graph_summary.json")).unwrap(),
    )
    .unwrap();
    let graph: Value =
        read_gzip_json(&export.join("reports/tree/static/app/dependency_graph.json.gz"));
    assert_eq!(
        summary["source_units"],
        graph["nodes"].as_array().unwrap().len()
    );
    assert_eq!(summary["edges"], graph["edges"].as_array().unwrap().len());
}

#[test]
fn reports_only_keeps_typed_rejection_without_emitting_a_program() {
    let (staged, _fixture) = staged(rejected_split());
    let small = staged.run_without_graphs(&["--emit-on-rejection"]);
    assert_eq!(
        small.result.status.code(),
        Some(3),
        "{}",
        small.result.stderr
    );
    let small_marker = fs::read_to_string(small.output_root().join("GATE_REJECTED")).unwrap();
    assert!(
        !small_marker.contains("dependency graph:"),
        "{small_marker}"
    );
    assert!(
        !small
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
    let export = staged.root().join("rejected-export");
    let result = staged.run(&[
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--emit-on-rejection",
    ]);
    assert_eq!(
        result.result.status.code(),
        Some(3),
        "{}",
        result.result.stderr
    );
    assert!(!export.join("app").exists());
    let marker = fs::read_to_string(export.join("GATE_REJECTED")).unwrap();
    assert!(
        marker.contains("reports-only") && marker.contains("NOT runnable"),
        "{marker}"
    );
    assert!(
        export
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
}

#[test]
fn reports_only_requires_analysis_and_keeps_final_program_validation() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const present = 1;\nconsole.log(present);\n",
        vec![logical_module("missing", &[Member::new("missing")])],
    ));
    let export = staged.root().join("invalid-export");
    let contradictory = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--out-root",
        path(&staged.root().join("different-root")),
    ]);
    assert_eq!(
        contradictory.status.code(),
        Some(1),
        "{}",
        contradictory.stderr
    );
    assert!(
        contradictory
            .stderr
            .contains("must name the same output root"),
        "{}",
        contradictory.stderr
    );
    let dry = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--dry-run",
    ]);
    assert_eq!(dry.status.code(), Some(2), "{}", dry.stderr);
    let no_analysis = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--reports-only",
        path(&export),
    ]);
    assert_eq!(no_analysis.status.code(), Some(2), "{}", no_analysis.stderr);
    let result = staged.run(&[
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
    ]);
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("missing"),
        "{}",
        result.result.stderr
    );
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
}

#[test]
fn bindings_assign_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { second(); return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: second}}}]\n",
        )],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let result = staged.assign(Some("second:a_earlier"), Some(&staged.artifact), true);
    assert_ok(&result.result, "bindings assign --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "{assessment:#}"
    );
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "candidate extraction into the earlier ESM module must invert source-unit order"
    );

    // Apply the identical move through the ordinary graph-backed edit path,
    // then compare the assessment with run --analysis over that candidate YAML.
    let applied = staged.assign(Some("second:a_earlier"), Some(&staged.artifact), false);
    assert_ok(&applied.result, "bindings assign --analysis");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        applied["effect_relocation_assessment"]["chunks"]["main"],
        *assessment
    );
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn destructuring_placement_preview_matches_staged_emission() {
    let source = "const { left, right } = { left: 10, right: 20 };\n\
console.log(left, right);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "group.yaml",
            "members: [{selector: {binding: {name: left}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(&result.result, "destructuring bindings assign preview");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["placement"]["projection_violations"],
        json!([]),
        "the shared request-planning pull must produce one finalized destination"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "destructuring grouping must be covered in the finalized preview"
    );
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "10 20\n",
        "",
    );
}

#[test]
fn nested_var_placement_preview_matches_staged_emission() {
    let source = "try { var impl = \"primary\"; } catch (e) { var impl = \"fallback\"; }\n\
console.log(impl);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "impl.yaml",
            "members: [{selector: {binding: {name: impl}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(&result.result, "nested-var bindings assign preview");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["placement"]["projection_violations"],
        json!([]),
        "nested-var planning should yield one finalized whole-statement destination"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "nested-var owners and whole-statement placement must be covered"
    );
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "primary\n",
        "",
    );
}

#[test]
fn bindings_assign_assessment_keeps_uncovered_havoc_conservative() {
    let source = "const written = (globalThis.marker = 1);\n\
const read = globalThis.marker;\n\
console.log(written, read);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "reader.yaml",
            "members: [{selector: {binding: {name: read}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(
        &result.result,
        "bindings assign --dry-run --analysis with no moves",
    );
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "conflict",
        "{assessment:#}"
    );
    assert_eq!(assessment["decision"]["havoc"], true, "{assessment:#}");
    assert_eq!(
        assessment["decision"]["uncovered_havoc"], true,
        "{assessment:#}"
    );
    assert!(assessment["counts"]["havoc_pairs"].as_u64().unwrap() > 0);
    assert_candidate_assessment_matches_run(application, &staged, false, true);
}

#[test]
fn modules_apply_assessment_matches_the_selected_residual_candidate() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { second(); return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let request = residual_apply_request(&staged, "second", "a_earlier", "second");
    assert_eq!(
        request["source_unit_ids"],
        json!([source_unit_for_binding(&staged, "second")])
    );
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();

    let before = module_snapshot(&staged.modules);
    let result = staged.apply(&batch, Some(&staged.artifact), true, true, false);
    assert_ok(&result.result, "modules apply --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "claiming the later residual in an earlier module must change the actual emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.apply(&batch, Some(&staged.artifact), false, true, false);
    assert_ok(&applied.result, "modules apply --analysis");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        applied["effect_relocation_assessment"]["chunks"]["main"], *assessment,
        "the written candidate must use the same canonical assessment as its preview"
    );
    assert!(applied.get("effect_assessment_unavailable").is_none());
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_apply_assessment_accepts_a_closed_no_swap_candidate() {
    let staged = staged_tree(
        &[(
            "main",
            "function only() { return \"only\"; }\nconsole.log(only());\n",
        )],
        &[("empty.yaml", "members: []\n")],
    );
    let request = residual_apply_request(&staged, "only", "new_module", "only");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();

    let result = staged.apply(&batch, Some(&staged.artifact), true, true, false);
    assert_ok(
        &result.result,
        "modules apply --dry-run --analysis with no swap",
    );
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["decision"]["scope"], "no_reordering",
        "{assessment:#}"
    );
    assert_eq!(assessment["swapped_pairs"], 0, "{assessment:#}");
    assert_eq!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "{assessment:#}"
    );
}

#[test]
fn modules_apply_assessment_refuses_stale_and_partial_inputs_before_writes() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let request = residual_apply_request(&staged, "second", "a_earlier", "second");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let before = module_snapshot(&staged.modules);

    let foreign = staged_tree(&[("main", "function other() { return \"other\"; }\n")], &[]);
    let stale = staged.apply(&batch, Some(&foreign.artifact), true, true, false);
    assert_eq!(
        stale.result.status.code(),
        Some(1),
        "{}",
        stale.result.stderr
    );
    assert!(
        stale.result.stderr.contains("projection") || stale.result.stderr.contains("analysis"),
        "expected staged projection refusal:\n{}",
        stale.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let partial = staged.apply(&batch, Some(&staged.artifact), true, false, false);
    assert_eq!(
        partial.result.status.code(),
        Some(1),
        "{}",
        partial.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let not_dry = staged.apply(&batch, Some(&foreign.artifact), false, true, false);
    assert_eq!(
        not_dry.result.status.code(),
        Some(1),
        "{}",
        not_dry.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_apply_scoped_assessment_matches_staged_emission() {
    let scoped = staged_tree_with_module_roots(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[("chunks/main", "main")],
        &[(
            "chunks/main/z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let scoped_request =
        residual_apply_request(&scoped, "second", "chunks/main/a_earlier", "second");
    let scoped_batch = scoped.root().join("apply.json");
    fs::write(
        &scoped_batch,
        serde_json::to_vec(&json!([scoped_request])).unwrap(),
    )
    .unwrap();
    let scoped_before = module_snapshot(&scoped.modules);
    let scoped_preview = scoped.apply(&scoped_batch, None, true, false, false);
    assert_ok(
        &scoped_preview.result,
        "ordinary scoped modules apply --dry-run",
    );
    let assessed = scoped.apply(&scoped_batch, Some(&scoped.artifact), true, true, false);
    assert_ok(
        &assessed.result,
        "scoped modules apply --dry-run --analysis",
    );
    let assessed: Value = serde_json::from_str(&assessed.result.stdout).unwrap();
    let application = &assessed["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(assessment["status"], "accepted_within_checked_scope");
    assert_eq!(module_snapshot(&scoped.modules), scoped_before);
    let applied = scoped.apply(&scoped_batch, Some(&scoped.artifact), false, true, false);
    assert_ok(&applied.result, "scoped modules apply --analysis");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        applied["effect_relocation_assessment"]["chunks"]["main"],
        *assessment
    );
    let candidate = assert_candidate_assessment_matches_run(application, &scoped, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_apply_assessment_inputs_are_explicit_but_ambient_defaults_are_ignored() {
    let staged = staged_tree(
        &[("main", "function value() { return \"value\"; }\n")],
        &[("empty.yaml", "members: []\n")],
    );
    let request = residual_apply_request(&staged, "value", "new_module", "value");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let before = module_snapshot(&staged.modules);
    let root = staged.root();
    let out = root.join("ambient-out");

    let ambient_args = staged.apply_args(&batch, None, true, false, false, &out);
    let ambient = std::process::Command::new(debundler_path())
        .args(ambient_args.iter().map(String::as_str))
        .env("DEBUNDLE_TREE_SOURCE_ROOT", root)
        .env("DEBUNDLE_OUT", &out)
        .output()
        .expect("spawn debundler with ambient transform defaults");
    assert!(
        ambient.status.success(),
        "ambient transform defaults changed ordinary apply behavior:\n{}",
        String::from_utf8_lossy(&ambient.stderr)
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let mut explicit_args = staged.apply_args(&batch, None, true, false, false, &out);
    explicit_args.extend([
        "--tree-source-root".to_string(),
        path(root).to_string(),
        "--out-root".to_string(),
        path(&out).to_string(),
    ]);
    let explicit = std::process::Command::new(debundler_path())
        .args(explicit_args.iter().map(String::as_str))
        .env("DEBUNDLE_TREE_SOURCE_ROOT", root)
        .env("DEBUNDLE_OUT", &out)
        .output()
        .expect("spawn debundler with explicit auxiliary inputs matching environment");
    assert_eq!(explicit.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&explicit.stderr).contains("require --analysis"),
        "expected explicit-context refusal:\n{}",
        String::from_utf8_lossy(&explicit.stderr)
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn bindings_unassign_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
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
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.unassign("first", None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed bindings unassign dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.unassign("first", Some(&staged.artifact), true);
    assert_ok(&result.result, "bindings unassign --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "unassigning the first function into residual must change the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.unassign("first", Some(&staged.artifact), false);
    assert_ok(&applied.result, "verified bindings unassign");
    assert!(!staged.modules.join("a_earlier.yaml").exists());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn bindings_unassign_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = staged_tree(
        &[("main", "function first() { return \"first\"; }\n")],
        &[(
            "a_earlier.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let foreign = staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let result = staged.unassign("first", Some(&foreign.artifact), true);
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("projection")
            || result.result.stderr.contains("analysis")
            || result.result.stderr.contains("source"),
        "expected staged input mismatch:\n{}",
        result.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_merge_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             function third() { return \"third\"; }\n\
             console.log(first(), second(), third());\n",
        )],
        &[
            (
                "a_earlier.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "b_middle.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
            (
                "z_later.yaml",
                "members: [{selector: {binding: {name: third}}}]\n",
            ),
        ],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.merge(&staged.modules, "z_later", "a_earlier", None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed modules merge dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.merge(
        &staged.modules,
        "z_later",
        "a_earlier",
        Some(&staged.artifact),
        true,
    );
    assert_ok(&result.result, "modules merge --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert_eq!(assessment["swapped_pairs"], 1, "{assessment:#}");
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "merging the earliest unit into the last module must change the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.merge(
        &staged.modules,
        "z_later",
        "a_earlier",
        Some(&staged.artifact),
        false,
    );
    assert_ok(&applied.result, "verified modules merge");
    assert!(!staged.modules.join("a_earlier.yaml").exists());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second third\n",
        "",
    );
}

#[test]
fn modules_delete_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
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
    );
    fs::create_dir_all(staged.modules.join("alternate")).unwrap();
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.delete("a_earlier", true, None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed modules delete dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.delete("alternate/../a_earlier", true, Some(&staged.artifact), true);
    assert_ok(
        &result.result,
        "modules delete --force --dry-run --analysis",
    );
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "deleting the earliest module must move its binding to residual and invert emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.delete("a_earlier", true, Some(&staged.artifact), false);
    assert_ok(&applied.result, "verified modules delete");
    assert!(!staged.modules.join("a_earlier.yaml").exists());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_delete_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = staged_tree(
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
    );
    let foreign = staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let result = staged.delete("a_earlier", true, Some(&foreign.artifact), true);
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("projection") || result.result.stderr.contains("analysis"),
        "{}\nexpected staged projection/artifact mismatch",
        result.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_merge_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = staged_tree(
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
    );
    let foreign = staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let result = staged.merge(
        &staged.modules,
        "z_later",
        "a_earlier",
        Some(&foreign.artifact),
        true,
    );
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("projection") || result.result.stderr.contains("analysis"),
        "{}
expected staged projection/artifact mismatch",
        result.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_refine_assessment_matches_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { second(); return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: second}}}]\n",
        )],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "second"),
            "module": "a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);
    let result = staged.refine(&batch, Some(&staged.artifact), true, false);
    assert_ok(&result.result, "modules refine --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "moving the later source unit into an earlier module must invert the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.refine(&batch, Some(&staged.artifact), false, false);
    assert_ok(&applied.result, "verified modules refine");
    let applied_outcome: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        &applied_outcome["effect_relocation_assessment"]["chunks"]["main"],
        assessment
    );
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_refine_assessment_refuses_stale_or_partial_context_before_writes() {
    let staged = staged_tree(
        &[("main", "function first() { return \"first\"; }\n")],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let foreign = staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    );
    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);

    let stale = staged.refine(&batch, Some(&foreign.artifact), true, false);
    assert_eq!(
        stale.result.status.code(),
        Some(1),
        "{}",
        stale.result.stderr
    );
    assert!(
        stale.result.stderr.contains("projection")
            || stale.result.stderr.contains("analysis")
            || stale.result.stderr.contains("source"),
        "expected staged input mismatch:\n{}",
        stale.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let partial = staged.refine(&batch, None, true, true);
    assert_eq!(
        partial.result.status.code(),
        Some(1),
        "{}",
        partial.result.stderr
    );
    assert!(
        partial.result.stderr.contains("require --analysis"),
        "{}",
        partial.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let env_defaults = {
        let output = Command::new(debundler_path())
            .args([
                "modules",
                "refine",
                "--batch",
                path(&batch),
                "--modules",
                path(&staged.modules),
                "--graph",
                path(&staged.graph),
                "--source-root",
                path(&staged.root().join("snapshot")),
                "--tree-config",
                path(&staged.config),
                "--dry-run",
                "--format",
                "json",
            ])
            .env("DEBUNDLE_TREE_SOURCE_ROOT", staged.root())
            .env("DEBUNDLE_OUT", staged.root().join("ambient-output"))
            .output()
            .expect("spawn debundler with transform environment defaults");
        CommandResult {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            status: output.status,
        }
    };
    assert_ok(
        &env_defaults,
        "ordinary modules refine with transform environment defaults",
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let explicit_auxiliary_matching_environment = {
        let source_root = staged.root().to_path_buf();
        let out_root = staged.root().join("ambient-output");
        let output = Command::new(debundler_path())
            .args([
                "modules",
                "refine",
                "--batch",
                path(&batch),
                "--modules",
                path(&staged.modules),
                "--graph",
                path(&staged.graph),
                "--source-root",
                path(&source_root.join("snapshot")),
                "--tree-config",
                path(&staged.config),
                "--tree-source-root",
                path(&source_root),
                "--out-root",
                path(&out_root),
                "--dry-run",
                "--format",
                "json",
            ])
            .env("DEBUNDLE_TREE_SOURCE_ROOT", &source_root)
            .env("DEBUNDLE_OUT", &out_root)
            .output()
            .expect("spawn debundler with explicit transform inputs matching environment defaults");
        CommandResult {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            status: output.status,
        }
    };
    assert_eq!(
        explicit_auxiliary_matching_environment.status.code(),
        Some(1),
        "explicit transform inputs must still require --analysis when equal to environment defaults\n{}",
        explicit_auxiliary_matching_environment.stderr
    );
    assert!(
        explicit_auxiliary_matching_environment
            .stderr
            .contains("require --analysis"),
        "expected explicit-context refusal:\n{}",
        explicit_auxiliary_matching_environment.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_refine_scoped_assessment_matches_staged_emission() {
    let staged = staged_tree_with_module_roots(
        &[(
            "main",
            "function first() { return \"first\"; }\nconsole.log(first());\n",
        )],
        &[("chunks/main", "main")],
        &[(
            "chunks/main/z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "chunks/main/a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);
    let result = staged.refine(&batch, Some(&staged.artifact), true, false);
    assert_ok(&result.result, "scoped modules refine --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(assessment["status"], "accepted_within_checked_scope");
    assert_eq!(module_snapshot(&staged.modules), before);
    let applied = staged.refine(&batch, Some(&staged.artifact), false, false);
    assert_ok(&applied.result, "scoped modules refine --analysis");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        applied["effect_relocation_assessment"]["chunks"]["main"],
        *assessment
    );
    let candidate = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first\n",
        "",
    );
}

fn application_tree_fixture() -> TreeStaged {
    staged_tree_with_module_roots(
        &[
            (
                "main",
                "import { suffix } from './helper.js';\n\
                 function first() { return 'first'; }\n\
                 function second() { return 'second'; }\n\
                 console.log(first(), second(), suffix);\n",
            ),
            ("helper", "export const suffix = 'peer';\n"),
        ],
        &[
            ("chunks/left", "main"),
            ("chunks/right", "main"),
            ("chunks/peer", "helper"),
        ],
        &[
            (
                "chunks/left/original.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "chunks/right/other.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
            (
                "chunks/peer/peer.yaml",
                "members: [{selector: {binding: {name: suffix}}}]\n",
            ),
        ],
    )
}

#[test]
fn application_candidate_preserves_other_roots_and_materialized_chunk_runtime() {
    let staged = application_tree_fixture();
    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "chunks/right/moved",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);
    let preview = staged.refine(&batch, Some(&staged.artifact), true, false);
    assert_ok(&preview.result, "complete application preview");
    assert_eq!(module_snapshot(&staged.modules), before);
    let preview: Value = serde_json::from_str(&preview.result.stdout).unwrap();
    let assessment = &preview["effect_relocation_assessment"];
    assert_eq!(assessment["status"], "accepted_within_checked_scope");
    assert_eq!(
        assessment["chunks"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["helper", "main"]
    );
    let applied = staged.refine(&batch, Some(&staged.artifact), false, false);
    assert_ok(&applied.result, "complete application apply");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(applied["effect_relocation_assessment"], *assessment);
    let after = module_snapshot(&staged.modules);
    assert_eq!(
        after[&staged.modules.join("chunks/peer/peer.yaml")],
        before[&staged.modules.join("chunks/peer/peer.yaml")]
    );
    assert_eq!(
        after[&staged.modules.join("chunks/right/other.yaml")],
        before[&staged.modules.join("chunks/right/other.yaml")]
    );
    let emitted = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(&staged.source, "first second peer\n", "");
    assert_node_output(
        &emitted.output_root().join("app/main/entry.js"),
        "first second peer\n",
        "",
    );
}

#[test]
fn application_candidate_applies_an_exact_root_without_dropping_sibling_roots() {
    let staged = application_tree_fixture();
    let edit_root = staged.modules.join("chunks/right");
    let before = module_snapshot(&staged.modules);
    let preview = staged.merge(&edit_root, "moved", "other", Some(&staged.artifact), true);
    assert_ok(&preview.result, "application subtree merge preview");
    assert_eq!(module_snapshot(&staged.modules), before);
    let preview: Value = serde_json::from_str(&preview.result.stdout).unwrap();
    let application = &preview["effect_relocation_assessment"];
    assert_eq!(application["status"], "accepted_within_checked_scope");
    assert_eq!(application["chunks"].as_object().unwrap().len(), 2);
    let applied = staged.merge(&edit_root, "moved", "other", Some(&staged.artifact), false);
    assert_ok(&applied.result, "application subtree merge apply");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(applied["effect_relocation_assessment"], *application);
    let after = module_snapshot(&staged.modules);
    for path in ["chunks/left/original.yaml", "chunks/peer/peer.yaml"] {
        let path = staged.modules.join(path);
        assert_eq!(after[&path], before[&path]);
    }
    let emitted = assert_candidate_assessment_matches_run(application, &staged, true, false);
    assert_node_output(&staged.source, "first second peer\n", "");
    assert_node_output(
        &emitted.output_root().join("app/main/entry.js"),
        "first second peer\n",
        "",
    );
}

#[test]
fn application_candidate_refuses_aliases_in_an_untouched_root_before_writes() {
    let staged = staged_tree_with_module_roots(
        &[
            (
                "main",
                "function first() { return 'first'; }\nconsole.log(first());\n",
            ),
            ("helper", "export function first() { return 'peer'; }\n"),
        ],
        &[("chunks/left", "main"), ("chunks/peer", "helper")],
        &[
            (
                "chunks/left/original.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "chunks/peer/original.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
        ],
    );
    let peer = staged.modules.join("chunks/peer");
    fs::remove_dir_all(&peer).unwrap();
    std::os::unix::fs::symlink("left", &peer).unwrap();
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "normal read of aliased authored roots");
    let graph = baseline
        .output_root()
        .join("reports/tree/main/dependency_graph.json.gz");
    assert!(graph.is_file());
    let edit_root = staged.modules.join("chunks/left");
    let output = staged.root().join("candidate-out");
    let before = module_snapshot(&staged.modules);
    for dry_run in [true, false] {
        let mut args = vec![
            "modules",
            "merge",
            "--modules",
            path(&edit_root),
            "--graph",
            path(&graph),
            "--source-root",
            path(staged.source.parent().unwrap()),
            "--target",
            "moved",
            "original",
            "--format",
            "json",
            "--tree-config",
            path(&staged.config),
            "--tree-modules",
            path(&staged.modules),
            "--tree-vendor-marks",
            path(&staged.vendor_marks),
            "--tree-source-root",
            path(staged.root()),
            "--out-root",
            path(&output),
            "--analysis",
            path(&staged.artifact),
        ];
        if dry_run {
            args.push("--dry-run");
        }
        let result = command(&args);
        assert_eq!(result.status.code(), Some(1), "{}", result.stderr);
        assert!(
            result.stderr.contains("symlink entries"),
            "{}",
            result.stderr
        );
        assert_eq!(module_snapshot(&staged.modules), before);
        assert_eq!(fs::read_link(&peer).unwrap(), Path::new("left"));
    }
}

#[test]
fn application_candidate_does_not_discard_a_refused_sibling_chunk() {
    let staged = staged_tree_with_module_roots(
        &[
            (
                "main",
                "function first() { return 'first'; }\nconsole.log(first());\n",
            ),
            (
                "helper",
                "const written = (globalThis.marker = 1);\nconst read = globalThis.marker;\nconsole.log(written, read);\n",
            ),
        ],
        &[("chunks/main", "main"), ("chunks/peer", "helper")],
        &[
            (
                "chunks/main/original.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "chunks/peer/a_reader.yaml",
                "members: [{selector: {binding: {name: read}}}]\n",
            ),
        ],
    );
    assert!(
        !staged.run.result.status.success(),
        "the authored sibling split is refused"
    );
    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "chunks/main/moved",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);
    let preview = staged.refine(&batch, Some(&staged.artifact), true, false);
    assert_ok(&preview.result, "application preview with refused sibling");
    let preview: Value = serde_json::from_str(&preview.result.stdout).unwrap();
    let application = &preview["effect_relocation_assessment"];
    assert_ne!(application["status"], "accepted_within_checked_scope");
    assert_eq!(
        application["chunks"]["main"]["status"],
        "accepted_within_checked_scope"
    );
    assert_ne!(
        application["chunks"]["helper"]["status"],
        "accepted_within_checked_scope"
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    let refused = staged.refine(&batch, Some(&staged.artifact), false, false);
    assert_eq!(
        refused.result.status.code(),
        Some(1),
        "{}",
        refused.result.stderr
    );
    let refused: Value = serde_json::from_str(&refused.result.stdout).unwrap();
    assert_eq!(refused["rejection"], *application);
    assert_eq!(module_snapshot(&staged.modules), before);
    assert_node_output(&staged.root().join("snapshot/helper.js"), "1 1\n", "");
}

#[test]
fn application_candidate_requires_final_program_validation_after_chunk_acceptance() {
    let staged = staged_tree_with_module_roots(
        &[
            (
                "main",
                "import { present } from './helper.js';\nfunction first() { return 'first'; }\nconsole.log(first(), present);\n",
            ),
            ("helper", "export const present = 7;\n"),
        ],
        &[("chunks/main", "main"), ("chunks/peer", "helper")],
        &[
            (
                "chunks/main/original.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "chunks/peer/missing.yaml",
                "members: [{selector: {binding: {name: missing}}}]\n",
            ),
        ],
    );
    assert!(
        !staged.run.result.status.success(),
        "normal reconstruction refuses an unresolved claim"
    );
    for chunk in ["main", "helper"] {
        let report: Value = serde_json::from_slice(
            &fs::read(
                staged
                    .run
                    .report_root
                    .join(format!("{chunk}/candidate_relocation_assessment.json")),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(report["status"], "accepted_within_checked_scope");
    }
    let batch = staged.root().join("refine.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "chunks/main/moved",
        }]))
        .unwrap(),
    )
    .unwrap();
    let before = module_snapshot(&staged.modules);
    for dry_run in [true, false] {
        let result = staged.refine(&batch, Some(&staged.artifact), dry_run, false);
        assert_eq!(
            result.result.status.code(),
            Some(1),
            "{}",
            result.result.stderr
        );
        assert!(
            result.result.stderr.contains("missing"),
            "{}",
            result.result.stderr
        );
        assert_eq!(module_snapshot(&staged.modules), before);
    }
    assert_node_output(&staged.source, "first 7\n", "");
}

#[test]
fn application_candidate_covers_all_configured_roots_and_materialized_chunks() {
    let cases = [
        (
            staged_tree_with_module_roots(
                &[("main", "function first() {}\nfunction second() {}\n")],
                &[("chunks/first", "main"), ("chunks/second", "main")],
                &[
                    (
                        "chunks/first/original.yaml",
                        "members: [{selector: {binding: {name: first}}}]\n",
                    ),
                    (
                        "chunks/second/other.yaml",
                        "members: [{selector: {binding: {name: second}}}]\n",
                    ),
                ],
            ),
            "chunks/first/moved",
            vec!["main"],
        ),
        (
            staged_tree_with_module_roots(
                &[
                    ("main", "function first() {}\n"),
                    ("helper", "export const shared = 7;\n"),
                ],
                &[("chunks/main", "main"), ("chunks/helper", "helper")],
                &[
                    (
                        "chunks/main/original.yaml",
                        "members: [{selector: {binding: {name: first}}}]\n",
                    ),
                    (
                        "chunks/helper/other.yaml",
                        "members: [{selector: {binding: {name: shared}}}]\n",
                    ),
                ],
            ),
            "chunks/main/moved",
            vec!["helper", "main"],
        ),
    ];
    for (staged, destination, expected) in cases {
        let batch = staged.root().join("refine.json");
        fs::write(
            &batch,
            serde_json::to_vec(&json!([{
                "unit_id": source_unit_for_binding(&staged, "first"),
                "module": destination,
            }]))
            .unwrap(),
        )
        .unwrap();
        let before = module_snapshot(&staged.modules);
        for dry_run in [true, false] {
            let result = staged.refine(&batch, Some(&staged.artifact), dry_run, false);
            assert_ok(&result.result, "complete application configured scope");
            let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
            let application = &outcome["effect_relocation_assessment"];
            assert_eq!(application["status"], "accepted_within_checked_scope");
            assert_eq!(
                application["chunks"]
                    .as_object()
                    .unwrap()
                    .keys()
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                expected
            );
            if dry_run {
                assert_eq!(module_snapshot(&staged.modules), before);
            } else {
                assert_candidate_assessment_matches_run(application, &staged, true, false);
            }
        }
    }
}

#[test]
fn bindings_assign_requested_assessment_refuses_stale_inputs_before_yaml_writes() {
    let staged = staged_tree(
        &[("main", "const value = 1;\nconsole.log(value);\n")],
        &[(
            "value.yaml",
            "members: [{selector: {binding: {name: value}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let original_artifact = fs::read(&staged.artifact).unwrap();

    // Engine identity is checked by the production staged-artifact loader.
    let (mut header, body) =
        staged_analysis::analysis_artifact::read_header(&original_artifact).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded_header = rmp_serde::to_vec_named(&header).unwrap();
    let mut wrong_engine = Vec::new();
    wrong_engine.extend_from_slice(&u32::try_from(encoded_header.len()).unwrap().to_le_bytes());
    wrong_engine.extend_from_slice(&encoded_header);
    wrong_engine.extend_from_slice(body);
    fs::write(&staged.artifact, wrong_engine).unwrap();
    let refused = staged.assign(Some("value:target"), Some(&staged.artifact), true);
    assert!(
        !refused.result.status.success(),
        "wrong engine was accepted"
    );
    assert!(
        refused.result.stderr.contains("engine"),
        "{}",
        refused.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    fs::write(&staged.artifact, &original_artifact).unwrap();

    // The verified graph/source context rejects a changed source before the
    // prospective YAML edit can be committed.
    let original_source = fs::read(&staged.source).unwrap();
    fs::write(&staged.source, "const value = 2;\nconsole.log(value);\n").unwrap();
    let refused = staged.assign(Some("value:target"), Some(&staged.artifact), true);
    assert!(
        !refused.result.status.success(),
        "changed source was accepted"
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    fs::write(&staged.source, original_source).unwrap();

    // A different projection with the same source and engine is rejected by
    // the same production `AnalysisMode::Load` path.
    let root = staged.root().to_path_buf();
    let alternate_list = root.join("js-files-alt.txt");
    fs::write(&alternate_list, "main.js\n").unwrap();
    let alternate_config = root.join("alternate_spec_config.yaml");
    let config_text = fs::read_to_string(&staged.config).unwrap();
    assert!(
        config_text.contains("js_list_path: js-files.txt"),
        "{config_text}"
    );
    fs::write(
        &alternate_config,
        config_text.replace(
            "js_list_path: js-files.txt",
            "js_list_path: js-files-alt.txt",
        ),
    )
    .unwrap();
    let alternate_projection = root.join("alternate.projection");
    let alternate_artifact = root.join("alternate.dba");
    let alternate_out = root.join("alternate-out");
    let project_args = [
        "spec",
        "project-analysis",
        "--tree-config",
        path(&alternate_config),
        "--tree-modules",
        path(&staged.modules),
        "--tree-vendor-marks",
        path(&staged.vendor_marks),
        "--tree-source-root",
        path(&root),
        "--out-root",
        path(&alternate_out),
        "--out",
        path(&alternate_projection),
    ];
    assert_ok(&command(&project_args), "alternate project-analysis");
    let analyze_args = [
        "--projection",
        path(&alternate_projection),
        "--tree-source-root",
        path(&root),
        "--out",
        path(&alternate_artifact),
    ];
    assert_ok(
        &command_with(&analyzer_path(), &analyze_args),
        "alternate analyze",
    );
    let refused = staged.assign(Some("value:target"), Some(&alternate_artifact), true);
    assert!(
        !refused.result.status.success(),
        "different projection was accepted"
    );
    assert!(
        refused.result.stderr.contains("projection"),
        "{}",
        refused.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

fn assert_candidate_has_placement_violation(
    result: &CommandResult,
    expected_violation: &str,
    label: &str,
) {
    assert_ok(result, label);
    let outcome: Value = serde_json::from_str(&result.stdout).unwrap();
    let application = &outcome["effect_relocation_assessment"];
    let assessment = &application["chunks"]["main"];
    assert_eq!(
        assessment["status"], "blocked_by_placement",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["status"], "blocked",
        "{assessment:#}"
    );
    assert!(
        assessment["placement"]["projection_violations"]
            .as_array()
            .expect("typed placement violations")
            .iter()
            .any(|violation| violation["kind"] == expected_violation),
        "expected {expected_violation} in {assessment:#}"
    );
}

fn assert_normal_run_refuses_with_projection_violation(
    run: &TreeRun,
    expected_debug_variant: &str,
    label: &str,
) {
    assert!(
        !run.result.status.success(),
        "{label} unexpectedly emitted runnable output"
    );
    assert!(
        run.result
            .stderr
            .contains("placement projection is unmappable"),
        "{label} did not use the common projection refusal:\n{}",
        run.result.stderr
    );
    assert!(
        run.result.stderr.contains(expected_debug_variant),
        "{label} did not report {expected_debug_variant}:\n{}",
        run.result.stderr
    );
    assert!(
        !run.out_root.join("app").exists(),
        "{label} wrote application output after placement refusal"
    );
}

#[test]
fn staged_assessment_types_conflicting_destructuring_claims() {
    const SOURCE: &str = "const source = { a: 1, b: 2 };\n\
const { a, b } = source;\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[
                (
                    "a_left.yaml",
                    "members: [{selector: {binding: {name: source}}}, {selector: {binding: {name: a}}}]\n",
                ),
                (
                    "b_right.yaml",
                    "members: [{selector: {binding: {name: b}}}]\n",
                ),
            ],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: source}}}, {selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.assign(Some("b:zz_right"), Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "destructuring_destination_split",
        "bindings assign --dry-run --analysis for split destructuring",
    );
    let before = module_snapshot(&staged.modules);
    let write = staged.assign(Some("b:zz_right"), Some(&staged.artifact), false);
    assert_eq!(
        write.result.status.code(),
        Some(1),
        "{}",
        write.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "DestructuringDestinationSplit",
        "split destructuring",
    );
}

#[test]
fn staged_assessment_types_partially_owned_nested_var_statement() {
    const SOURCE: &str = "if (true) { var a = 1; var b = 2; }\n\
console.log(a, b);\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[(
                "z_same.yaml",
                "members: [{selector: {binding: {name: a}}}]\n",
            )],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.unassign("b", Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "nested_var_missing_owner",
        "bindings unassign --dry-run --analysis for partial nested var",
    );
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "NestedVarMissingOwner",
        "partially owned nested var",
    );
}

#[test]
fn staged_assessment_types_split_nested_var_bindings() {
    const SOURCE: &str = "if (true) { var a = 1; var b = 2; }\n\
console.log(a, b);\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[
                (
                    "a_left.yaml",
                    "members: [{selector: {binding: {name: a}}}]\n",
                ),
                (
                    "b_right.yaml",
                    "members: [{selector: {binding: {name: b}}}]\n",
                ),
            ],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.assign(Some("b:zz_right"), Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "nested_var_destination_split",
        "bindings assign --dry-run --analysis for split nested-var bindings",
    );
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "NestedVarDestinationSplit",
        "split nested-var bindings",
    );
}

fn set_source_only(staged: &Staged, binding: &str, chunk_rename: bool) {
    let mut spec: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&staged.spec).unwrap()).unwrap();
    let annotation = serde_yaml::from_str("initializer_binding_effects: source_only").unwrap();
    if chunk_rename {
        spec["chunk_renames"]["static/app"] = serde_yaml::from_str(&format!(
            "members:\n  - selector:\n      binding:\n        name: {binding}\nannotations: {{}}\n"
        ))
        .unwrap();
        spec["chunk_renames"]["static/app"]["annotations"][binding] = annotation;
    } else {
        spec["logical_modules"]["static/app"]["all"]["annotations"][binding] = annotation;
    }
    write_yaml_file(&staged.spec, &spec);
}

#[test]
fn initializer_source_only_reuses_raw_analysis_and_changes_materialized_effects() {
    struct Case {
        source: &'static str,
        binding: &'static str,
        members: &'static [&'static str],
        anonymous_statements: &'static [&'static str],
        static_footprint: &'static [(&'static str, &'static str)],
        output: &'static str,
        expects_rebind: bool,
        array_integrity_premise: bool,
    }
    let cases = [
        Case {
            source: "const seed = 41;\n\
function factory() { globalThis.host?.(() => { const value = later; later = 3; return value; }); return seed; }\n\
var result = (seed, factory(), factory());\n\
var later = 0;\n\
console.log(result, later);\n",
            binding: "result",
            members: &["seed", "factory", "result", "later"],
            anonymous_statements: &[],
            static_footprint: &[("seed", "eager_use"), ("factory", "hoisted_use")],
            output: "41 0\n",
            expects_rebind: true,
            array_integrity_premise: false,
        },
        Case {
            source: "function readLater() { return later; }\n\
globalThis.saved = readLater;\n\
var table = (() => {\n\
  const values = [];\n\
  values.push((0).toString());\n\
  return values;\n\
})();\n\
var later = \"ready\";\n\
console.log(table[0], later, globalThis.saved === readLater);\n\
export { table, readLater };\n",
            binding: "table",
            members: &["readLater", "table", "later"],
            anonymous_statements: &[
                "globalThis.saved = readLater;",
                "console.log(table[0], later, globalThis.saved === readLater);",
            ],
            static_footprint: &[],
            output: "0 ready true\n",
            expects_rebind: false,
            array_integrity_premise: true,
        },
    ];
    for case in cases {
        for chunk_rename in [false, true] {
            let members = case
                .members
                .iter()
                .map(|name| Member::new(name))
                .collect::<Vec<_>>();
            let mut opts = FixtureOpts::new(
                case.source,
                vec![logical_module_with_anon(
                    "all",
                    &members,
                    case.anonymous_statements,
                )],
            );
            if case.array_integrity_premise {
                opts = opts.with_program_premises(ProgramPremises {
                    intrinsics_never_written: Some(IntrinsicsNeverWritten {
                        families: IntrinsicFamilies::Listed(BTreeSet::from([
                            IntrinsicFamily::Array,
                        ])),
                        note: Some("Neutral source-only receiver provenance fixture".to_owned()),
                    }),
                    ..ProgramPremises::default()
                });
            }
            let (staged, _fixture) = staged(opts);
            let before = staged.run(&[]);
            assert_ok(&before.result, "unannotated materialization");
            let raw_graph: Value = read_gzip_json(
                &before
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let result = graph_node_for_binding(&raw_graph, case.binding);
            let later = graph_node_for_binding(&raw_graph, "later");
            assert!(
                raw_graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == result["id"]
                        && edge["target"] == later["id"]
                        && edge["edge_kind"] == "eager_use"
                        && edge["binding"] == "later"
                }),
                "fallback must contribute a dependency before the overlay: {raw_graph:#}"
            );
            assert!(
                raw_graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == later["id"]
                        && edge["target"] == result["id"]
                        && edge["edge_kind"] == "sequenced"
                }),
                "an assumed observation before initialization must retain source order: {raw_graph:#}"
            );
            let artifact_before = fs::read(&staged.artifact).unwrap();
            set_source_only(&staged, case.binding, chunk_rename);
            let edited = staged.root().join("source-only.projection");
            assert_ok(&staged.project(&edited), "project source-only annotation");
            assert!(
                fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
                "a lexical effect overlay must preserve raw analysis identity"
            );
            assert_ok(&staged.analyze(), "reanalyze with unchanged raw projection");
            assert!(
                artifact_before == fs::read(&staged.artifact).unwrap(),
                "source-only annotations must leave raw source facts and receipts unchanged"
            );
            let direct = staged.run(&[]);
            let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
            assert_ok(&direct.result, "direct source-only materialization");
            assert_ok(&loaded.result, "loaded source-only materialization");
            assert_same_invocation_trees(&direct, &loaded);
            assert_node_output(
                &loaded.output_root().join("app/static/app/entry.js"),
                case.output,
                "",
            );
            let graph: Value = read_gzip_json(
                &loaded
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let result = graph_node_for_binding(&graph, case.binding);
            let execution = &result["synchronous_execution"];
            assert_eq!(
                execution["binding_effect_authority"]["authority"], "author_source_only",
                "{execution:#}"
            );
            let effect_use = &execution["initializer_binding_effects"];
            assert_eq!(effect_use["outcome"], "applied", "{execution:#}");
            for field in ["narrowed_reads", "narrowed_uninitialized_reads"] {
                assert!(effect_use[field].as_u64().unwrap() > 0, "{effect_use:#}");
            }
            if case.expects_rebind {
                assert!(
                    effect_use["narrowed_rebinds"].as_u64().unwrap() > 0,
                    "{effect_use:#}"
                );
            } else {
                assert_eq!(effect_use["narrowed_rebinds"], 0, "{effect_use:#}");
            }
            for &(binding, edge_kind) in case.static_footprint {
                let target = graph_node_for_binding(&graph, binding);
                assert!(
                    graph["edges"].as_array().unwrap().iter().any(|edge| {
                        edge["source"] == result["id"]
                            && edge["target"] == target["id"]
                            && edge["edge_kind"] == edge_kind
                            && edge["binding"] == binding
                    }),
                    "static footprint dependency must survive: {graph:#}"
                );
            }
            assert!(
                !graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == result["id"]
                        && edge["edge_kind"] == "eager_use"
                        && edge["binding"] == "later"
                }),
                "source-only authority must suppress fallback roots: {graph:#}"
            );
            let later = graph_node_for_binding(&graph, "later");
            assert!(
                !graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == later["id"]
                        && edge["target"] == result["id"]
                        && edge["edge_kind"] == "sequenced"
                }),
                "narrowed assumed observations must leave the ordering graph: {graph:#}"
            );
        }
    }
}

#[test]
fn initializer_source_only_still_rejects_observed_reads_and_rebinds_with_saved_facts() {
    let mut cases = ["return seed;", "seed = 9; return 7;"]
        .into_iter()
        .map(|body| {
            (
                format!(
                    "let seed = 41;\nfunction factory() {{ {body} }}\nvar result = factory();\nconsole.log(result);\n"
                ),
                "result",
                vec!["seed", "factory", "result"],
            )
        })
        .collect::<Vec<_>>();
    cases.push((
        "function readLater() { return later; }\nvar table = readLater();\nvar later = \"ready\";\nconsole.log(table);\nexport { table, readLater };\n".to_owned(),
        "table",
        vec!["readLater", "table", "later"],
    ));
    for (source, binding, member_names) in cases {
        let members = member_names
            .iter()
            .map(|name| Member::new(name))
            .collect::<Vec<_>>();
        let (staged, _fixture) = staged(FixtureOpts::new(
            &source,
            vec![logical_module("all", &members)],
        ));
        set_source_only(&staged, binding, false);
        for args in [vec![], vec!["--analysis", path(&staged.artifact)]] {
            let invocation = staged.run(&args);
            assert!(
                !invocation.result.status.success(),
                "observed lexical effects must contradict the premise"
            );
            assert!(
                invocation
                    .result
                    .stderr
                    .contains("contradicts observed lexical effects"),
                "{}",
                invocation.result.stderr
            );
        }
    }
}

#[test]
fn initializer_source_only_preserves_simple_declarator_admission() {
    for source in [
        "var result;\n",
        "function result() { return 7; }\n",
        "const {result} = {result: 7};\n",
        "var result = 7; var result = 8;\n",
    ] {
        for chunk_rename in [false, true] {
            let (staged, _fixture) = staged(FixtureOpts::new(
                source,
                vec![logical_module("all", &[Member::new("result")])],
            ));
            set_source_only(&staged, "result", chunk_rename);
            let projection = staged.root().join("inadmissible.projection");
            let result = staged.project(&projection);
            assert!(
                !result.status.success(),
                "unsupported target must fail before raw analysis"
            );
            assert!(
                result.stderr.contains("initializer_binding_effects"),
                "{}",
                result.stderr
            );
            for args in [vec![], vec!["--analysis", path(&staged.artifact)]] {
                let result = staged.run(&args);
                assert!(
                    !result.result.status.success(),
                    "unsupported target must fail materialization"
                );
                assert!(
                    result.result.stderr.contains("initializer_binding_effects"),
                    "{}",
                    result.result.stderr
                );
            }
        }
    }
}

#[test]
fn initializer_source_only_uses_resolved_binding_and_keeps_comma_siblings_raw() {
    for bare_redeclaration in ["", "var result;\n"] {
        let source = format!(
            "const seed = 41; function factory() {{ return seed; }}\nvar sibling = factory(), result = 7;\n{bare_redeclaration}console.log(sibling, result);\n"
        );
        let (staged, _fixture) = staged(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("factory"),
                    Member::new("sibling"),
                    Member::renamed("readable", "result"),
                ],
            )],
        ));
        set_source_only(&staged, "readable", false);
        let edited = staged.root().join("renamed.projection");
        assert_ok(&staged.project(&edited), "project resolved annotation");
        assert_eq!(
            fs::read(&staged.projection).unwrap(),
            fs::read(edited).unwrap()
        );
        let direct = staged.run(&[]);
        let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
        assert_ok(&direct.result, "direct comma-list annotation");
        assert_ok(&loaded.result, "loaded comma-list annotation");
        assert_same_invocation_trees(&direct, &loaded);
        assert_node_output(
            &loaded.output_root().join("app/static/app/entry.js"),
            "41 7\n",
            "",
        );
        let graph: Value = read_gzip_json(
            &loaded
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let selected = graph_node_for_binding(&graph, "result");
        assert_eq!(
            selected["synchronous_execution"]["initializer_binding_effects"]["binding"],
            "result"
        );
        assert_eq!(
            selected["synchronous_execution"]["binding_effect_authority"]["authority"],
            "author_source_only"
        );
        let sibling = graph_node_for_binding(&graph, "sibling");
        assert!(sibling["synchronous_execution"]["initializer_binding_effects"].is_null());
        let seed = graph_node_for_binding(&graph, "seed");
        assert!(
            graph["edges"].as_array().unwrap().iter().any(|edge| {
                edge["source"] == sibling["id"]
                    && edge["target"] == seed["id"]
                    && edge["edge_kind"] == "eager_use"
                    && edge["binding"] == "seed"
            }),
            "the sibling's observed promoted read must remain: {graph:#}"
        );
    }
}

#[test]
fn inferred_and_selected_set_spread_bindings_keep_opaque_crossing_rejected() {
    assert_set_spread_saved_replay(
        "console.log('before');\nvar seed = new Set(['a', 'b']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);\nconsole.log('after', copied.size);\nexport {};\n",
        "before\nafter 3\n",
        true,
    );
}

#[test]
fn set_spread_operation_receipts_survive_saved_relocation() {
    assert_set_spread_saved_replay(
        "0;\nvar seed = new Set(['a', 'b']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);\nconsole.log('after', copied.size);\nexport {};\n",
        "after 3\n",
        false,
    );
}

fn assert_set_spread_saved_replay(
    source: &str,
    expected_stdout: &str,
    rejects_opaque_neighbor: bool,
) {
    const DECLARATION: &str = "var seed = new Set(['a', 'b']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);";
    for selected in [false, true] {
        let mut opts = FixtureOpts::new(
            source,
            vec![logical_module(
                "sets",
                &[
                    Member::new("seed"),
                    Member::new("sibling"),
                    Member::new("copied"),
                ],
            )],
        );
        if selected {
            let inputs = [
                ("seed", NativeOperationKind::SetFromPrimitiveArray),
                ("sibling", NativeOperationKind::SetFromPrimitiveArray),
                ("copied", NativeOperationKind::SetFromSetSpread),
            ]
            .into_iter()
            .map(|(binding, operation)| NativeOperationInputContract {
                selector: SourceUnitSelector::SourceMatch(
                    serde_yaml::from_str(&format!(
                        "match: {DECLARATION:?}\nbindings: [{binding}]\n"
                    ))
                    .expect("native construction selector"),
                ),
                operation,
                audited_source_sha256: Sha256::digest(source.as_bytes())
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect(),
                note: None,
            })
            .collect();
            opts = opts.with_native_operation_inputs("static/app", inputs);
        }
        let (staged, _fixture) = staged(opts);
        assert_node_output(
            &staged.root().join("snapshot/static/app.js"),
            expected_stdout,
            "",
        );
        let direct_args = if rejects_opaque_neighbor {
            vec!["--emit-on-rejection", "--effect-conflicts-jsonl"]
        } else {
            Vec::new()
        };
        let direct = staged.run(&direct_args);
        if !direct.result.status.success() {
            save_relocation_reports(
                &direct.output_root().join("reports/tree"),
                "static/app",
                if selected {
                    "set-spread-direct-selected"
                } else {
                    "set-spread-direct-inferred"
                },
            );
        }
        if rejects_opaque_neighbor {
            assert_potential_suppression_chunk_relocation(
                &direct,
                "static/app",
                "blocked_by_effect",
            );
        } else {
            assert_ok(&direct.result, "direct Set spread run");
        }
        let mut loaded_args = direct_args;
        loaded_args.extend(["--analysis", path(&staged.artifact)]);
        let loaded = staged.run(&loaded_args);
        if !loaded.result.status.success() {
            save_relocation_reports(
                &loaded.output_root().join("reports/tree"),
                "static/app",
                if selected {
                    "set-spread-saved-selected"
                } else {
                    "set-spread-saved-inferred"
                },
            );
        }
        if rejects_opaque_neighbor {
            assert_potential_suppression_chunk_relocation(
                &loaded,
                "static/app",
                "blocked_by_effect",
            );
        } else {
            assert_ok(&loaded.result, "Set spread run from saved analysis");
        }
        assert_same_invocation_trees(&direct, &loaded);
        for invocation in [&direct, &loaded] {
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                expected_stdout,
                "",
            );
            let graph: Value = read_gzip_json(
                &invocation
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let copied_execution =
                &graph_node_for_binding(&graph, "copied")["synchronous_execution"];
            assert_eq!(copied_execution["complete"], true, "{copied_execution:#}");
            let receipt = &copied_execution["binding_effect_authority"];
            assert_eq!(
                receipt["authority"], "operation_receipt",
                "{copied_execution:#}"
            );
            let calls = receipt["receipt"]["calls"].as_array().unwrap();
            assert_eq!(calls.len(), 1, "{copied_execution:#}");
            assert_eq!(
                calls[0]["target"]["kind"], "native_construction",
                "{calls:#?}"
            );
            assert_eq!(calls[0]["target"]["intrinsic"], "set", "{calls:#?}");

            if selected {
                let refs = json!([
                    {"defining_chunk": "static/app", "input_index": 0},
                    {"defining_chunk": "static/app", "input_index": 1},
                    {"defining_chunk": "static/app", "input_index": 2}
                ]);
                assert_eq!(
                    receipt["receipt"]["read_dependencies"]["native_operation_inputs"], refs,
                    "the inferred Set-spread proof depends on all selected construction sites: {receipt:#}"
                );
                assert_eq!(
                    receipt["receipt"]["rebind_dependencies"]["native_operation_inputs"], refs,
                    "{receipt:#}"
                );
                assert_eq!(
                    calls[0]["dependencies"]["native_operation_inputs"], refs,
                    "{calls:#?}"
                );
                for (binding, input_index, operation) in [
                    ("seed", 0, "set_from_primitive_array"),
                    ("sibling", 1, "set_from_primitive_array"),
                    ("copied", 2, "set_from_set_spread"),
                ] {
                    let execution =
                        &graph_node_for_binding(&graph, binding)["synchronous_execution"];
                    let uses = execution["native_operation_input_uses"]
                        .as_array()
                        .expect("selected Set operation input uses");
                    assert!(
                        uses.iter().any(|use_site| {
                            use_site["evidence"]["reference"]["defining_chunk"] == "static/app"
                                && use_site["evidence"]["reference"]["input_index"] == input_index
                                && use_site["evidence"]["operation"] == operation
                                && use_site["evidence"]["selected_binding"] == binding
                        }),
                        "{binding} lost its exact selected native input: {execution:#}"
                    );
                }
            } else {
                assert_eq!(
                    receipt["receipt"]["read_dependencies"],
                    json!({}),
                    "{receipt:#}"
                );
                assert_eq!(
                    receipt["receipt"]["rebind_dependencies"],
                    json!({}),
                    "{receipt:#}"
                );
                assert_eq!(calls[0]["dependencies"], json!({}), "{calls:#?}");
                for binding in ["seed", "sibling", "copied"] {
                    let execution =
                        &graph_node_for_binding(&graph, binding)["synchronous_execution"];
                    assert!(
                        execution["inference_uses"]
                            .as_array()
                            .expect("inferred Set operation uses")
                            .iter()
                            .any(|proof| proof["conclusion"] == "primitive_set_iteration"),
                        "{binding} lost primitive Set iteration inference: {execution:#}"
                    );
                }
            }

            let report = candidate_relocation_report(invocation, "static/app");
            if rejects_opaque_neighbor {
                assert_eq!(report["status"], "blocked_by_effect", "{report:#}");
                assert_eq!(report["decision"]["uncovered_havoc"], true, "{report:#}");
                assert_eq!(
                    report["emitted_order"],
                    json!([1, 2, 3, 0, 4, 5]),
                    "{report:#}"
                );
                assert_eq!(report["swapped_pairs"], 3, "{report:#}");
                assert_eq!(report["counts"]["havoc_pairs"], 3, "{report:#}");
                assert_eq!(report["counts"]["uncovered_pairs"], 3, "{report:#}");
                let rows = read_jsonl(
                    &invocation
                        .output_root()
                        .join("reports/tree/static/app/effect_conflicts.jsonl"),
                );
                assert_eq!(rows.len(), 3, "{rows:#?}");
                for (binding, unit) in [("seed", 1), ("sibling", 2), ("copied", 3)] {
                    let pair = rows
                        .iter()
                        .find(|row| {
                            row["runs_first"]["unit"] == unit && row["runs_later"]["unit"] == 0
                        })
                        .unwrap_or_else(|| {
                            panic!("{binding} no longer crosses opaque console unit 0: {rows:#?}")
                        });
                    assert_eq!(pair["runs_first"]["module"], "sets", "{pair:#}");
                    assert_eq!(
                        pair["runs_later"]["module"], "anon_residual_sentinel",
                        "{pair:#}"
                    );
                    assert_eq!(pair["havoc"], true, "{pair:#}");
                    assert_eq!(pair["disposition"], "uncovered", "{pair:#}");
                    assert_eq!(
                        pair["first_opacity"]["unknown_coverage"],
                        json!([]),
                        "{pair:#}"
                    );
                    assert_eq!(
                        pair["later_opacity"]["incomplete_execution"], true,
                        "{pair:#}"
                    );
                    assert_eq!(pair["later_opacity"]["opaque_heap"], true, "{pair:#}");
                    for domain in ["global_properties", "general_heap", "host_effects"] {
                        assert!(
                            pair["later_opacity"]["unknown_coverage"]
                                .as_array()
                                .unwrap()
                                .contains(&json!(domain)),
                            "{pair:#}"
                        );
                    }
                    assert_eq!(
                        pair["later_opacity"]["owner_coverage_refusal"]["reason"],
                        "unsupported_owner_kind",
                        "{pair:#}"
                    );
                    let losses = pair["later_opacity"]["execution_losses"]
                        .as_array()
                        .unwrap();
                    assert!(
                        losses.iter().any(|loss| {
                            loss["reason"]["code"] == "unsupported_operation"
                                && loss["reason"]["operation"] == "member_read"
                        }),
                        "{pair:#}"
                    );
                    assert!(
                        losses
                            .iter()
                            .any(|loss| { loss["reason"]["code"] == "unknown_call_target" }),
                        "{pair:#}"
                    );
                    assert!(
                        losses.iter().all(|loss| {
                            loss["statement_ordinal"] == 0
                                && loss["source_location"]["start_line"] == 1
                        }),
                        "{pair:#}"
                    );
                }
            } else {
                assert_eq!(
                    report["status"], "accepted_within_checked_scope",
                    "{report:#}"
                );
                assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
                assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
            }
        }
    }
}

#[test]
fn module_declarative_store_effects_survive_saved_analysis() {
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "function make() { return () => 3; }\nconst read = make();\nlet marker = 0;\nmarker = 1;\nconst result = read();\nmarker = 2;\nconsole.log(marker, result);\n",
            vec![
                logical_module_with_anon("markers", &[Member::new("marker")], &["marker = 1;", "marker = 2;"]),
                logical_module("result", &[Member::new("make"), Member::new("read"), Member::new("result")]),
                logical_module_with_anon("observe", &[], &["console.log(marker, result);"]),
            ],
        ).with_dataflow_aware_s_chain(),
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct module-store run");
    let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&loaded.result, "module-store run from saved analysis");
    assert_same_invocation_trees(&direct, &loaded);
    for invocation in [&direct, &loaded] {
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "2 3\n",
            "",
        );
    }
}
