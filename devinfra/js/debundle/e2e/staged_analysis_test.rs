//! `debundle spec project-analysis`, `debundle analyze` and `debundle run
//! --analysis` reproduce `debundle run` exactly, and an artifact stays valid
//! across exactly the spec edits analysis does not read.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use debundle_e2e_support::*;
use serde_json::Value;

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
        command(&[
            "analyze",
            "--projection",
            path(&self.projection),
            "--tree-source-root",
            path(root),
            "--out",
            path(&self.artifact),
        ])
    }

    fn run(&self, extra: &[&str]) -> CommandResult {
        let mut args = vec!["run", "--graphs", "--spec", path(&self.spec)];
        args.extend_from_slice(extra);
        command(&args)
    }

    /// Move the trees a run wrote aside, so the next run writes fresh ones.
    fn set_aside(&self, label: &str) -> PathBuf {
        let aside = self.root().join(label);
        fs::create_dir_all(&aside).unwrap();
        for name in ["out", "harness"] {
            let tree = self.root().join(name);
            if tree.exists() {
                fs::rename(&tree, aside.join(name)).unwrap();
            }
        }
        fs::create_dir_all(self.root().join("out")).unwrap();
        aside
    }
}

fn path(path: &Path) -> &str {
    path.to_str().expect("UTF-8 test path")
}

fn command(args: &[&str]) -> CommandResult {
    let output = run_debundle(args);
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
                Ok(read_json::<Value>(&entry))
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

fn assert_same_trees(direct: &Path, staged: &Path) {
    let (direct, staged) = (tree(direct), tree(staged));
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

fn assert_staged_run_matches(opts: FixtureOpts<'_>, run_args: &[&str]) {
    let (staged, _fixture) = staged(opts);
    let direct = staged.run(run_args);
    let direct_trees = staged.set_aside("direct");
    assert!(
        tree(&direct_trees.join("out")).keys().any(|path| path
            .file_name()
            .is_some_and(|name| name == "effect_translation_validation.json")),
        "direct run must produce the facts-backed effect report before staged comparison"
    );
    if run_args.contains(&"--effect-conflicts-jsonl") {
        assert!(
            tree(&direct_trees.join("out")).keys().any(|path| path
                .file_name()
                .is_some_and(|name| name == "effect_conflicts.jsonl")),
            "explicit stream must exist before staged comparison"
        );
    }
    let artifact = path(&staged.artifact).to_owned();
    let mut staged_args = run_args.to_vec();
    staged_args.extend(["--analysis", artifact.as_str()]);
    let from_artifact = staged.run(&staged_args);
    assert_eq!(direct.status.code(), from_artifact.status.code());
    assert_same_trees(&direct_trees.join("out"), &staged.root().join("out"));
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
fn gate_rejected_split_matches_a_direct_run() {
    assert_staged_run_matches(rejected_split(), &["--emit-on-rejection"]);
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
        &staged.run(&["--analysis", &artifact]),
        "run --analysis after the edit",
    );
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
    assert!(!refused.status.success(), "stale artifact was accepted");
    assert!(
        refused
            .stderr
            .contains("chunk_export_purity.static/vendorlib"),
        "refusal does not name the differing field:\n{}",
        refused.stderr
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
    assert_ok(&staged.run(export_args), "unfiltered export");
    let all = staged.set_aside("all-conflicts");
    let mut flags = export_args.to_vec();
    flags.extend([filter_flag, "static/app"]);
    assert_ok(&staged.run(&flags), "filtered export");
    let filtered = staged.set_aside("filtered-conflicts");
    let without_streams = |root: &Path| {
        tree(root)
            .into_iter()
            .filter(|(path, _)| path.file_name().is_none_or(|name| name != filename))
            .collect::<BTreeMap<_, _>>()
    };
    // tree() removes only command provenance. Every other byte, including
    // all graphs, summary counts, native census and emitted JS, must match.
    assert_eq!(
        without_streams(&all.join("out")),
        without_streams(&filtered.join("out"))
    );
    let main_stream = PathBuf::from(format!("reports/tree/static/app/{filename}"));
    let peer_stream = PathBuf::from(format!("reports/tree/static/peer/{filename}"));
    assert_eq!(
        fs::read(all.join("out").join(&main_stream)).unwrap(),
        fs::read(filtered.join("out").join(&main_stream)).unwrap()
    );
    assert!(all.join("out").join(&peer_stream).exists());
    assert!(!filtered.join("out").join(&peer_stream).exists());
    let artifact = path(&staged.artifact).to_owned();
    let mut loaded_flags = flags.to_vec();
    loaded_flags.extend(["--analysis", &artifact]);
    assert_ok(
        &staged.run(&loaded_flags),
        "filtered export from full staged analysis",
    );
    assert_same_trees(&filtered.join("out"), &staged.root().join("out"));
    staged.set_aside("filtered-loaded");
    let mut all_selected = export_args.to_vec();
    all_selected.extend([filter_flag, "static/app", filter_flag, "static/peer"]);
    assert_ok(
        &staged.run(&all_selected),
        "repeated filter selects both chunks",
    );
    assert_same_trees(&all.join("out"), &staged.root().join("out"));
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
        suppressed_graphs.status.code(),
        Some(1),
        "{}",
        suppressed_graphs.stderr
    );
    assert!(
        suppressed_graphs.stderr.contains("--graphs requested"),
        "{}",
        suppressed_graphs.stderr
    );
    assert_ok(
        &command(&["run", "--spec", path(&staged.spec)]),
        "small default run",
    );
    assert_node_output(
        &staged.root().join("out/app/static/app/entry.js"),
        "2\n",
        "",
    );
    let small = staged.set_aside("small-default");
    let small_files = tree(&small.join("out"));
    assert!(!small_files.keys().any(|p| {
        p.file_name()
            .is_some_and(|n| n == "dependency_graph.json.gz" || n == "program_graph.json.gz")
    }));
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]),
        "full graphs from shared analysis",
    );
    let full = tree(&staged.root().join("out"));
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
        &staged.run(&[
            "--analysis",
            path(&staged.artifact),
            "--reports-only",
            path(&export),
        ]),
        "reports-only full export",
    );
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
    let exported = tree(&export.join("reports/tree"));
    let program_reports = tree(&staged.root().join("out/reports/tree"));
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
    let small = command(&["run", "--spec", path(&staged.spec), "--emit-on-rejection"]);
    assert_eq!(small.status.code(), Some(3), "{}", small.stderr);
    let small_marker = fs::read_to_string(staged.root().join("out/GATE_REJECTED")).unwrap();
    assert!(
        !small_marker.contains("dependency graph:"),
        "{small_marker}"
    );
    assert!(
        !staged
            .root()
            .join("out/reports/tree/static/app/dependency_graph.json.gz")
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
    assert_eq!(result.status.code(), Some(3), "{}", result.stderr);
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
    assert_eq!(result.status.code(), Some(1), "{}", result.stderr);
    assert!(result.stderr.contains("missing"), "{}", result.stderr);
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
}
