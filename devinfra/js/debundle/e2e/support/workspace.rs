//! Owned tree inputs shared by ordinary, graph and staged CLI workflows.

use super::{CommandResult, command_result, current_test_prefix, debundler_path, write_text_file};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use tempfile::TempDir;

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

impl TreeRun {
    pub fn root(&self) -> &Path {
        self._root.path()
    }
}

/// The `--tree-{config,modules,vendor-marks,source-root}` arguments for the
/// tree-fixture layout [`run_tree_fixture`] writes under `root`.
pub fn tree_spec_args(root: &Path) -> [OsString; 8] {
    [
        "--tree-config".into(),
        root.join("spec_config.yaml").into(),
        "--tree-modules".into(),
        root.join("modules").into(),
        "--tree-vendor-marks".into(),
        root.join("vendor_marks.yaml").into(),
        "--tree-source-root".into(),
        root.into(),
    ]
}

/// Writes `fixture` and runs `debundle run` on it in tree form with
/// `extra_args`, writing the JS tree.
/// A tree fixture for tests whose queries require full graph reports.
pub fn run_tree_graph_fixture(fixture: &TreeFixture<'_>, extra_args: &[&str]) -> TreeRun {
    let args = std::iter::once("--graphs")
        .chain(extra_args.iter().copied())
        .collect::<Vec<_>>();
    run_tree_fixture(fixture, &args)
}

pub fn run_tree_fixture(fixture: &TreeFixture<'_>, extra_args: &[&str]) -> TreeRun {
    write_tree_fixture(fixture).run(extra_args)
}

/// A materialized tree input, owned until its last command finishes.
/// Preparing it does not execute the CLI or create an output tree.
pub struct TreeWorkspace {
    root: TempDir,
}

impl TreeWorkspace {
    pub fn root(&self) -> &Path {
        self.root.path()
    }

    /// Execute the current on-disk spec and transfer its lifetime to the result.
    pub fn run(self, extra_args: &[&str]) -> TreeRun {
        let out_root = self.root().join("out");
        let bin = debundler_path();
        let output = Command::new(&bin)
            .arg("run")
            .args(tree_spec_args(self.root()))
            .arg("--out-root")
            .arg(&out_root)
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
        TreeRun {
            report_root: out_root.join("reports").join("tree"),
            out_root,
            result: command_result(output),
            _root: self.root,
        }
    }
}

pub fn write_tree_fixture(fixture: &TreeFixture<'_>) -> TreeWorkspace {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let snapshot = root.path().join("snapshot");
    let modules = root.path().join("modules");
    fs::create_dir_all(&modules).expect("create modules root");
    let mut js_list = String::new();
    let mut unassigned_mode = String::new();
    for (chunk, source) in fixture.chunks {
        write_text_file(&snapshot.join(format!("{chunk}.js")), source);
        js_list.push_str(&format!("{chunk}.js\n"));
        unassigned_mode.push_str(&format!("  {chunk}: {{ kind: inline_in_entry }}\n"));
    }
    write_text_file(&root.path().join("js-files.txt"), &js_list);
    let module_roots = if fixture.module_roots.is_empty() {
        String::new()
    } else {
        format!(
            "module_roots:\n{}",
            fixture
                .module_roots
                .iter()
                .map(|(tree, chunk)| format!("  {tree}: {chunk}\n"))
                .collect::<String>()
        )
    };
    for (tree, _) in fixture.module_roots {
        fs::create_dir_all(modules.join(tree)).expect("create chunk module root");
    }
    for (path, body) in fixture.modules {
        write_text_file(&modules.join(path), body);
    }
    let config = root.path().join("spec_config.yaml");
    write_text_file(
        &config,
        &format!(
            "main_chunk_id: {}\n{module_roots}inputs:\n  root: snapshot\n  \
             js_list_path: js-files.txt\nwrite_js_tree: true\nunassigned_mode:\n{unassigned_mode}",
            fixture.chunks[0].0,
        ),
    );
    let vendor_marks = root.path().join("vendor_marks.yaml");
    write_text_file(&vendor_marks, "vendor_marks: []\n");
    TreeWorkspace { root }
}
