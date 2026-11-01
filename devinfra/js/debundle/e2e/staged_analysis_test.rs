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
        let mut args = vec!["run", "--spec", path(&self.spec)];
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
            let bytes = match serde_json::from_slice::<Value>(&bytes) {
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
}

#[test]
fn gate_rejected_split_matches_a_direct_run() {
    assert_staged_run_matches(rejected_split(), &["--emit-on-rejection"]);
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
