//! Black-box harness for the `debundle` binary.
//!
//! Drives the CLI through a JSONC spec and asserts on the emitted file
//! tree by reading files and re-running them under `node`.

use runfiles::{Runfiles, rlocation};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use swc_common::FileName;
use swc_common::sync::Lrc;
use swc_ecma_ast::{
    BindingIdent, BlockStmtOrExpr, Decl, ExportSpecifier, Expr, FnDecl, Function, ImportSpecifier,
    Module, ModuleDecl, ModuleExportName, ModuleItem, ObjectPatProp, Pat, Stmt, VarDeclKind,
    VarDeclarator,
};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};
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

    // Mirror `extra_files` into out_root after the transform runs, so
    // re-imports emitted by the materializer can resolve through
    // their relative paths under out_root. (write_js_tree wipes
    // out_root with `force=true` before emitting; mirroring earlier
    // would lose these files.)
    for (rel_path, content) in opts.extra_files {
        write_text_file(&setup.out_root.join(rel_path), content);
    }

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

/// Run the materializer over `opts` and assert it rejects the spec
/// with stderr containing at least one of `error_substring_alternatives`
/// (case-insensitive). Use this helper when the rejection's exact
/// wording isn't pinned — e.g. when several rejection paths converge
/// on the same outcome and the caller is fine with any of them.
///
/// For tests that need to assert *specific evidence* in the error
/// (e.g. "the cycle report names mod_a AND mod_b"), use
/// [`expect_logical_modules_e2e_rejection_containing_all`] instead.
pub fn expect_logical_modules_e2e_rejection(
    opts: FixtureOpts<'_>,
    error_substring_alternatives: &[&str],
) {
    let stderr = run_and_assert_rejected(&opts);
    let stderr_lower = stderr.to_lowercase();
    assert!(
        error_substring_alternatives
            .iter()
            .any(|s| stderr_lower.contains(&s.to_lowercase())),
        "stderr did not contain any of {error_substring_alternatives:?}\nstderr:\n{stderr}",
    );
}

/// Stricter sibling of [`expect_logical_modules_e2e_rejection`]: the
/// stderr must contain **every** substring in `required_substrings`,
/// not just one. Use when the test's contract is that the error
/// names specific evidence (every module in a cycle, every binding
/// in a collision, etc.); a generic-but-empty error wouldn't pass
/// the contract.
pub fn expect_logical_modules_e2e_rejection_containing_all(
    opts: FixtureOpts<'_>,
    required_substrings: &[&str],
) {
    let stderr = run_and_assert_rejected(&opts);
    let stderr_lower = stderr.to_lowercase();
    let missing: Vec<&str> = required_substrings
        .iter()
        .copied()
        .filter(|s| !stderr_lower.contains(&s.to_lowercase()))
        .collect();
    assert!(
        missing.is_empty(),
        "stderr missing required substrings {missing:?}\nstderr:\n{stderr}",
    );
}

fn run_and_assert_rejected(opts: &FixtureOpts<'_>) -> String {
    let setup = setup_fixture(opts);
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
    result.stderr
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
        "inputs": { "inputRoot": snapshot_root, "jsListPath": js_list_path },
        "operations": all_operations,
        "pipeline": [
            {
                "id": "logical",
                "operation": "materialize_logical_modules",
                "args": { "chunkIds": [chunk_id], "pruneOtherChunks": false, "targetDir": "modules" },
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

pub fn write_text_file(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

pub fn write_json_file(path: &Path, value: &Value) {
    fs::write(
        path,
        format!("{}\n", serde_json::to_string_pretty(value).unwrap()),
    )
    .unwrap();
}

pub fn debundler_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, DEBUNDLER_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve debundler runfile: {DEBUNDLER_RLOCATION}"))
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

fn spawn_transform(spec_path: &Path) -> CommandResult {
    run_debundler(spec_path, &[])
}

/// Run `debundle --spec <path> [--package-root <name>=<dir> ...]` and return its
/// captured stdio + exit status. Used by tests that exercise pipeline stages
/// outside the logical-modules harness in [`run_logical_modules_e2e_fixture`].
pub fn run_debundler(spec_path: &Path, package_roots: &[(&str, &Path)]) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command.arg("--spec").arg(spec_path);
    for (name, dir) in package_roots {
        command
            .arg("--package-root")
            .arg(format!("{name}={}", dir.display()));
    }
    let output = command
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

// --- AST-walking assertion helpers ---------------------------------------

/// Parse `source` as an ESM module and return the SWC AST. Tests use this
/// when the substring-on-emit checks aren't precise enough — e.g. when
/// they need to walk specifiers to disambiguate `aH$1 as aH` (correct)
/// from `aH$1 as aH$1` (corrupt).
pub fn parse_module(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("entry.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Typescript(TsSyntax {
            tsx: true,
            decorators: true,
            no_early_errors: true,
            ..Default::default()
        }),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer)
        .parse_module()
        .unwrap_or_else(|err| panic!("entry must parse, got {err:?}; source:\n{source}"))
}

/// Parse `source` and assert that every named import specifier binds a
/// distinct local symbol. Mirrors the duplicate-declaration check Node
/// would perform at module-load time.
pub fn assert_unique_import_locals(source: &str) {
    let module = parse_module(source);
    let mut seen = BTreeSet::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for specifier in &import.specifiers {
            let local = match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            };
            assert!(
                seen.insert(local.clone()),
                "duplicate import local `{local}` in:\n{source}",
            );
        }
    }
}

/// Parse `source` and assert exactly one `export { ... }` specifier has
/// `orig.sym == expected_orig`, with its `exported` either absent (when
/// `expected_exported_as` is `None`) or `Ident { sym: expected_exported_as }`.
/// Walks the parsed specifier tree so a corrupted `export { aH$1 as aH$1 }`
/// fails — a substring check on `aH$1 as aH` would accept both shapes.
pub fn assert_export_named_specifier(
    source: &str,
    expected_orig: &str,
    expected_exported_as: Option<&str>,
) {
    let module = parse_module(source);
    let matched: Vec<_> = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => Some(named),
            _ => None,
        })
        .flat_map(|named| named.specifiers.iter())
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named) => Some(named),
            _ => None,
        })
        .filter(|spec| {
            let ModuleExportName::Ident(ident) = &spec.orig else {
                return false;
            };
            ident.sym.as_ref() == expected_orig
        })
        .collect();
    assert_eq!(
        matched.len(),
        1,
        "expected exactly one `export {{ {expected_orig} ... }}` specifier; got {} in:\n{source}",
        matched.len(),
    );
    let actual = match &matched[0].exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => panic!("unexpected string export in:\n{source}"),
        None => None,
    };
    assert_eq!(
        actual.as_deref(),
        expected_exported_as,
        "export {{ {expected_orig} ... }} `as` clause mismatch in:\n{source}",
    );
}

/// Assert that no function-body scope in `source` declares `target_name`
/// more than once (counting destructured params and `let`/`const` decls;
/// `var` is excluded because it allows redeclaration in the same scope).
/// Mirrors Node's lexical-binding duplicate check.
pub fn assert_unique_lexical_decls_per_scope(source: &str, target_name: &str) {
    fn pat_binds(pat: &Pat, target: &str) -> bool {
        match pat {
            Pat::Ident(BindingIdent { id, .. }) => id.sym.as_ref() == target,
            Pat::Object(object) => object.props.iter().any(|prop| match prop {
                ObjectPatProp::KeyValue(kv) => pat_binds(&kv.value, target),
                ObjectPatProp::Assign(assign) => assign.key.id.sym.as_ref() == target,
                ObjectPatProp::Rest(rest) => pat_binds(&rest.arg, target),
            }),
            Pat::Array(array) => array
                .elems
                .iter()
                .flatten()
                .any(|elem| pat_binds(elem, target)),
            Pat::Assign(assign) => pat_binds(&assign.left, target),
            Pat::Rest(rest) => pat_binds(&rest.arg, target),
            _ => false,
        }
    }

    fn check_function(function: &Function, target: &str, source: &str) {
        let Some(body) = &function.body else {
            return;
        };
        let mut count = 0;
        for param in &function.params {
            if pat_binds(&param.pat, target) {
                count += 1;
            }
        }
        for stmt in &body.stmts {
            // `var` allows redeclaration in the same scope (and `function f(a){var a;}`
            // is legal); only `let`/`const`/`class`/`function` are subject to the
            // "Identifier 'X' has already been declared" lexical check.
            if let Stmt::Decl(Decl::Var(var)) = stmt
                && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
            {
                for declarator in &var.decls {
                    if pat_binds(&declarator.name, target) {
                        count += 1;
                    }
                }
            }
        }
        assert!(
            count <= 1,
            "scope binds `{target}` {count} times in:\n{source}",
        );
        for stmt in &body.stmts {
            descend_stmt(stmt, target, source);
        }
    }

    fn descend_stmt(stmt: &Stmt, target: &str, source: &str) {
        match stmt {
            Stmt::Decl(Decl::Fn(FnDecl { function, .. })) => {
                check_function(function, target, source)
            }
            Stmt::Decl(Decl::Var(var)) => {
                for VarDeclarator { init, .. } in &var.decls {
                    if let Some(init) = init {
                        descend_expr(init, target, source);
                    }
                }
            }
            Stmt::Block(block) => {
                for stmt in &block.stmts {
                    descend_stmt(stmt, target, source);
                }
            }
            _ => {}
        }
    }

    fn descend_expr(expr: &Expr, target: &str, source: &str) {
        match expr {
            Expr::Fn(fn_expr) => check_function(&fn_expr.function, target, source),
            Expr::Arrow(arrow) => {
                let mut count = 0;
                for param in &arrow.params {
                    if pat_binds(param, target) {
                        count += 1;
                    }
                }
                if let BlockStmtOrExpr::BlockStmt(block) = &*arrow.body {
                    for stmt in &block.stmts {
                        if let Stmt::Decl(Decl::Var(var)) = stmt
                            && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
                        {
                            for declarator in &var.decls {
                                if pat_binds(&declarator.name, target) {
                                    count += 1;
                                }
                            }
                        }
                    }
                }
                assert!(
                    count <= 1,
                    "arrow scope binds `{target}` {count} times in:\n{source}",
                );
            }
            _ => {}
        }
    }

    let module = parse_module(source);
    for item in &module.body {
        if let ModuleItem::Stmt(stmt) = item {
            descend_stmt(stmt, target_name, source);
        }
    }
}
