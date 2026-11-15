//! Node and Bun execution checks for emitted Debundle output.

use super::case_commands;
use super::{
    APP_DIR, CommandResult, Fixture, PreparedFixture, command_result, current_test_prefix, process,
    spawn_transform_with_args,
};
use artifact::paths::path_from_module_path;
use runfiles::{Runfiles, rlocation};
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

const NODE_RLOCATION: &str = "nodejs_linux_amd64/bin/node";

static MODULE_EXPORT_PROBE_COUNTER: AtomicUsize = AtomicUsize::new(0);
static GENERATED_MODULE_SCRIPT_COUNTER: AtomicUsize = AtomicUsize::new(0);

pub fn assert_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.entry_path, expected_stdout, "");
}

/// Compare the original chunk and its emitted entry under the same Node runtime.
pub fn assert_preserved_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.source_path, expected_stdout, "");
    assert_entry_output(fixture, expected_stdout);
}

/// Assert that the original and emitted entry both fail with the same runtime
/// error class and stdout. This covers top-level initialization failures, whose
/// modules cannot be imported inside a catch block by the generated-module
/// probe helper.
pub fn assert_preserved_entry_error(fixture: &Fixture, expected_error: &str) {
    let original = run_node_script(&fixture.source_path);
    let emitted = run_node_script(&fixture.entry_path);
    for (path, result) in [
        (&fixture.source_path, &original),
        (&fixture.entry_path, &emitted),
    ] {
        assert!(
            !result.status.success(),
            "node {} unexpectedly succeeded\nstdout:\n{}\nstderr:\n{}",
            path.display(),
            result.stdout,
            result.stderr,
        );
        assert!(
            result.stderr.contains(expected_error),
            "node {} stderr did not contain {expected_error:?}:\n{}",
            path.display(),
            result.stderr,
        );
    }
    assert_eq!(original.status.code(), emitted.status.code());
    assert_eq!(original.stdout, emitted.stdout);
}

/// Compare both files under the pinned Bun release supplied in this test's
/// runfiles. Node execution alone cannot validate a Bun-specific host premise.
pub fn assert_preserved_entry_output_with_bun(fixture: &Fixture, expected_stdout: &str) {
    let bun = bun_1_3_10_path();
    for path in [&fixture.source_path, &fixture.entry_path] {
        let output = case_commands::fixture_output(Command::new(&bun).arg(path), path)
            .unwrap_or_else(|err| panic!("{err}"));
        assert!(
            output.status.success(),
            "Bun {} exited {:?}: {}",
            path.display(),
            output.status.code(),
            String::from_utf8_lossy(&output.stderr),
        );
        assert_eq!(String::from_utf8_lossy(&output.stdout), expected_stdout);
        assert!(
            output.stderr.is_empty(),
            "Bun stderr: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

/// Exercise Bun's standalone embedding path for one runtime-premise probe.
/// This tests the public release binary, not any third-party embedded artifact.
pub fn assert_compiled_bun_entry_output(fixture: &Fixture, expected_stdout: &str) {
    let bun = bun_1_3_10_path();
    let dir = fixture._root.child();
    let executable = dir.path().join("probe");
    let compile = case_commands::fixture_output(
        Command::new(&bun)
            .arg("build")
            .arg("--compile")
            .arg(&fixture.source_path)
            .arg("--outfile")
            .arg(&executable),
        &fixture.source_path,
    )
    .unwrap_or_else(|err| panic!("{err}"));
    assert!(
        compile.status.success(),
        "Bun compile failed: {}",
        String::from_utf8_lossy(&compile.stderr)
    );
    let output =
        case_commands::fixture_output(&mut Command::new(&executable), &fixture.source_path)
            .unwrap_or_else(|err| panic!("{err}"));
    assert!(
        output.status.success(),
        "compiled Bun exited {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&output.stdout), expected_stdout);
    assert!(
        output.stderr.is_empty(),
        "compiled Bun stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn bun_1_3_10_path() -> PathBuf {
    let relative = std::env::var("DEBUNDLE_TEST_BUN").expect("DEBUNDLE_TEST_BUN runfile label");
    let runfiles = Runfiles::create().expect("create runfiles");
    let bun = rlocation!(runfiles, relative.as_str())
        .unwrap_or_else(|| panic!("resolve Bun runfile: {relative}"));
    let version =
        process::output(Command::new(&bun).arg("--version")).unwrap_or_else(|err| panic!("{err}"));
    assert!(version.status.success(), "Bun version check failed");
    assert_eq!(String::from_utf8_lossy(&version.stdout).trim(), "1.3.10");
    bun
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
        let output =
            case_commands::fixture_output(Command::new(&node).arg("--check").arg(&file), &file)
                .unwrap_or_else(|err| panic!("{err}"));
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

pub fn assert_node_error(path: &Path, expected_error: &str) {
    let result = run_node_script(path);
    assert!(
        !result.status.success(),
        "node {} unexpectedly succeeded\nstdout:\n{}\nstderr:\n{}",
        path.display(),
        result.stdout,
        result.stderr,
    );
    assert!(
        result.stderr.contains(expected_error),
        "node {} stderr did not contain {expected_error:?}:\n{}",
        path.display(),
        result.stderr,
    );
}

fn node_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, NODE_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve node runfile: {NODE_RLOCATION}"))
}

pub(super) fn run_node_script(path: &Path) -> CommandResult {
    let node = node_path();
    command_result(
        case_commands::fixture_output(Command::new(&node).arg(path), path)
            .unwrap_or_else(|e| panic!("spawn node {}: {e}", node.display())),
    )
}

/// With `DEBUNDLE_TV_AUDIT` (the L4c discrepancy audit, beside the CLI's
/// `debundle_tv_audit/` records), write whether the source and the emitted
/// entry behave the same under Node, with the effect verdict of the run that
/// emitted it, to `$TEST_UNDECLARED_OUTPUTS_DIR/debundle_node_parity/`.
pub(super) fn audit_node_parity(kind: &str, source: &Path, entry: &Path, effect_report: &Path) {
    let Some(dir) = std::env::var_os("DEBUNDLE_TV_AUDIT")
        .and_then(|_| std::env::var_os("TEST_UNDECLARED_OUTPUTS_DIR"))
    else {
        return;
    };
    static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
    let dir = PathBuf::from(dir).join("debundle_node_parity");
    let run = |path: &Path| {
        let result = run_node_script(path);
        serde_json::json!({
            "code": result.status.code(),
            "stdout": result.stdout,
            "error": result.stderr.lines().find(|line| line.contains("Error")),
        })
    };
    let effect = fs::read_to_string(effect_report).ok().map(|text| {
        let report: Value = serde_json::from_str(&text).expect("effect report JSON");
        serde_json::json!({
            "legacy_gate_accepts": report["legacy_gate_accepts"],
            "status": report["decision"]["status"],
            "swapped_pairs": report["swapped_pairs"],
            "emitted_order": report["emitted_order"],
        })
    });
    let record = serde_json::json!({
        "test": current_test_prefix(),
        "kind": kind,
        "source": run(source),
        "emitted": entry.exists().then(|| run(entry)),
        "effect": effect,
    });
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join(format!(
            "{}-{}.json",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        )),
        record.to_string(),
    )
    .unwrap();
}

/// `audit_node_parity` for a refused fixture: rerun it with
/// `--emit-on-rejection` into a sibling output root, restoring the refused
/// run's reports for the caller's assertions.
pub(super) fn audit_rejected_node_parity(setup: &PreparedFixture, args: &[&str], chunk_id: &str) {
    if std::env::var_os("DEBUNDLE_TV_AUDIT").is_none() || args.contains(&"--emit-on-rejection") {
        return;
    }
    let refused = setup.out_root.with_extension("refused");
    let emitted = setup.out_root.with_extension("emitted");
    fs::rename(&setup.out_root, &refused).unwrap();
    let rerun =
        spawn_transform_with_args(&setup.spec_path, &[args, &["--emit-on-rejection"]].concat());
    if setup.out_root.exists() {
        fs::rename(&setup.out_root, &emitted).unwrap();
    }
    fs::rename(&refused, &setup.out_root).unwrap();
    // Only a gate-only refusal (exit 3) lowers a runnable-shaped tree.
    if rerun.status.code() == Some(3) {
        audit_node_parity(
            "rejected",
            &setup.snapshot_root.join(format!("{chunk_id}.js")),
            &emitted
                .join(APP_DIR)
                .join(path_from_module_path(chunk_id))
                .join("entry.js"),
            &emitted
                .join(setup.report_root.strip_prefix(&setup.out_root).unwrap())
                .join(chunk_id)
                .join("effect_translation_validation.json"),
        );
    }
}
