//! Black-box harness for the debundler binary.
//!
//! Equivalent to the former `support.mjs`: drives the `debundle_rust` CLI
//! through a JSONC spec and asserts on the emitted file tree by reading
//! files and re-running them under `node`.

use runfiles::{Runfiles, rlocation};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use tempfile::TempDir;

const DEBUNDLER_RLOCATION: &str = "_main/devinfra/js/debundle/debundle";
const NODE_RLOCATION: &str = "nodejs_linux_amd64/bin/node";

static MODULE_EXPORT_PROBE_COUNTER: AtomicUsize = AtomicUsize::new(0);
static GENERATED_MODULE_SCRIPT_COUNTER: AtomicUsize = AtomicUsize::new(0);

/// One member of a `define_logical_module` request.
///
/// `name` is the exported name in the materialized module; `binding` is the
/// original top-level binding to extract. When `binding` is `None`, the
/// exported name and the original binding are the same.
pub struct Member {
    pub name: &'static str,
    pub binding: Option<&'static str>,
}

impl Member {
    /// Extract a binding under its original name.
    pub fn new(name: &'static str) -> Self {
        Self {
            name,
            binding: None,
        }
    }

    /// Extract `binding` and re-export it as `name`.
    pub fn renamed(name: &'static str, binding: &'static str) -> Self {
        Self {
            name,
            binding: Some(binding),
        }
    }
}

pub fn logical_module(path: &str, members: &[Member]) -> Value {
    let id = format!("logical__{}", path.replace('/', "_"));
    let member_values: Vec<Value> = members
        .iter()
        .map(|m| {
            let binding = m.binding.unwrap_or(m.name);
            json!({
                "id": format!("member__{}", m.name),
                "name": m.name,
                "selector": { "binding": { "name": binding } },
            })
        })
        .collect();
    json!({
        "id": id,
        "operation": "define_logical_module",
        "selector": { "chunkId": "static/app" },
        "target": { "path": path },
        "members": member_values,
    })
}

pub struct FixtureOpts<'a> {
    pub source: &'a str,
    pub operations: Vec<Value>,
    pub chunk_id: &'a str,
    pub include_residual: bool,
    pub extra_files: &'a [(&'a str, &'a str)],
}

impl<'a> FixtureOpts<'a> {
    pub fn new(source: &'a str, operations: Vec<Value>) -> Self {
        Self {
            source,
            operations,
            chunk_id: "static/app",
            include_residual: true,
            extra_files: &[],
        }
    }
}

pub struct Fixture {
    pub chunk_id: String,
    pub entry_path: PathBuf,
    pub out_root: PathBuf,
    #[allow(dead_code)]
    pub snapshot_root: PathBuf,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

pub fn run_logical_modules_e2e_fixture(opts: FixtureOpts<'_>) -> Fixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.out_root.join("transform_spec.jsonc");
    let spec = build_spec(
        opts.chunk_id,
        opts.include_residual,
        &setup.js_list_path,
        &opts.operations,
        &setup.out_root,
        &setup.snapshot_root,
    );
    write_json_file(&spec_path, &spec);

    let result = spawn_transform(&spec_path);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let entry_path = setup
        .out_root
        .join(opts.chunk_id.split('/').collect::<PathBuf>())
        .join("entry.js");
    Fixture {
        chunk_id: opts.chunk_id.to_string(),
        entry_path,
        out_root: setup.out_root,
        snapshot_root: setup.snapshot_root,
        _root: setup.root,
    }
}

pub fn expect_logical_modules_e2e_rejection(
    opts: FixtureOpts<'_>,
    error_substring_alternatives: &[&str],
) {
    let setup = setup_fixture(&opts);
    let spec_path = setup.out_root.join("transform_spec.jsonc");
    let spec = build_spec(
        opts.chunk_id,
        opts.include_residual,
        &setup.js_list_path,
        &opts.operations,
        &setup.out_root,
        &setup.snapshot_root,
    );
    write_json_file(&spec_path, &spec);

    let result = spawn_transform(&spec_path);
    assert!(
        !result.status.success(),
        "expected spec to be rejected\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    let stderr_lower = result.stderr.to_lowercase();
    assert!(
        error_substring_alternatives
            .iter()
            .any(|s| stderr_lower.contains(&s.to_lowercase())),
        "stderr did not contain any of {:?}\nstderr:\n{}",
        error_substring_alternatives,
        result.stderr,
    );
}

pub fn assert_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.entry_path, expected_stdout, "");
}

pub fn list_module_exports(out_root: &Path, module_path: &str) -> Vec<String> {
    let counter = MODULE_EXPORT_PROBE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let probe_path = out_root.join(format!("__probe_module_exports_{counter}.mjs"));
    let probe = format!(
        "const mod = await import({});\nprocess.stdout.write(JSON.stringify(Object.keys(mod)));\n",
        json!(format!("./{module_path}")),
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
    let exported: std::collections::BTreeSet<String> = list_module_exports(out_root, module_path)
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

pub fn assert_generated_module_script(out_root: &Path, source: &str, expected_stdout: &str) {
    let counter = GENERATED_MODULE_SCRIPT_COUNTER.fetch_add(1, Ordering::Relaxed);
    let assertion_path = out_root.join(format!("assert_generated_module_{counter}.mjs"));
    fs::write(&assertion_path, source).unwrap();
    assert_node_output(&assertion_path, expected_stdout, "");
}

pub fn assert_generated_module_after_entry_script(
    out_root: &Path,
    source: &str,
    expected_stdout: &str,
) {
    // Silences the entry's own console.log (the entry already executes its
    // top-level effect) before running the caller-supplied probe script.
    let wrapped = format!(
        "const __log = console.log;\n\
         console.log = () => {{}};\n\
         await import(\"./static/app/entry.js\");\n\
         console.log = __log;\n\
         {source}",
    );
    assert_generated_module_script(out_root, &wrapped, expected_stdout);
}

pub struct NodeOutput {
    pub stdout: String,
    pub stderr: String,
    pub status: std::process::ExitStatus,
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

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

struct FixtureSetup {
    root: TempDir,
    out_root: PathBuf,
    snapshot_root: PathBuf,
    js_list_path: PathBuf,
}

fn setup_fixture(opts: &FixtureOpts<'_>) -> FixtureSetup {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let extracted_root = root.path().join("extracted");
    let out_root = root.path().join("out");
    let snapshot_root = root.path().join("snapshot");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();

    // Mark the snapshot tree as ESM so node loads emitted .js files as modules.
    write_text_file(
        &snapshot_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&json!({"type": "module"})).unwrap()
        ),
    );

    let entry_file = format!("{}.js", opts.chunk_id);
    write_text_file(&snapshot_root.join(&entry_file), opts.source);
    for (rel_path, content) in opts.extra_files {
        write_text_file(&snapshot_root.join(rel_path), content);
    }

    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &format!("{entry_file}\n"));

    FixtureSetup {
        root,
        out_root,
        snapshot_root,
        js_list_path,
    }
}

fn build_spec(
    chunk_id: &str,
    include_residual: bool,
    js_list_path: &Path,
    operations: &[Value],
    out_root: &Path,
    snapshot_root: &Path,
) -> Value {
    let mut all_operations: Vec<Value> = operations.to_vec();
    if include_residual {
        all_operations.push(json!({
            "id": "logical__residual_unhandled",
            "operation": "define_residual_module",
            "selector": { "chunkId": chunk_id },
            "target": { "path": "residual/unhandled" },
        }));
    }
    json!({
        "kind": "js.ast_transform_spec",
        "operations": all_operations,
        "pipeline": [
            {
                "id": "load",
                "operation": "load_js_chunks",
                "args": { "inputRoot": snapshot_root, "jsListPath": js_list_path },
            },
            { "id": "parse", "operation": "compute_js_asts" },
            { "id": "normalize", "operation": "normalize_js_chunks", "args": { "jobs": 1 } },
            {
                "id": "logical",
                "operation": "materialize_logical_modules",
                "args": { "chunkIds": [chunk_id], "pruneOtherChunks": false },
            },
            { "id": "write", "operation": "write_js_tree", "args": { "force": true, "outDir": out_root } },
        ],
    })
}

/// Slugified test path for the current `#[test]` thread, used as the
/// tempdir prefix so failed runs are easy to pick out of `/tmp` without
/// each test having to repeat its own name.
fn current_test_prefix() -> String {
    let thread = std::thread::current();
    let name = thread.name().unwrap_or("unknown");
    let slug: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = slug.trim_matches('-');
    let mut compact = String::with_capacity(trimmed.len());
    let mut prev_dash = false;
    for c in trimmed.chars() {
        if c == '-' {
            if !prev_dash {
                compact.push(c);
            }
            prev_dash = true;
        } else {
            compact.push(c);
            prev_dash = false;
        }
    }
    format!("debundle-e2e-{compact}-")
}

fn write_text_file(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn write_json_file(path: &Path, value: &Value) {
    fs::write(
        path,
        format!("{}\n", serde_json::to_string_pretty(value).unwrap()),
    )
    .unwrap();
}

fn debundler_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, DEBUNDLER_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve debundler runfile: {DEBUNDLER_RLOCATION}"))
}

fn node_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, NODE_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve node runfile: {NODE_RLOCATION}"))
}

struct CommandResult {
    stdout: String,
    stderr: String,
    status: std::process::ExitStatus,
}

fn spawn_transform(spec_path: &Path) -> CommandResult {
    let bin = debundler_path();
    let output = Command::new(&bin)
        .arg("--spec")
        .arg(spec_path)
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

fn run_node_script(path: &Path) -> CommandResult {
    let node = node_path();
    let output = Command::new(&node)
        .arg(path)
        .output()
        .unwrap_or_else(|e| panic!("spawn node {}: {e}", node.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}
