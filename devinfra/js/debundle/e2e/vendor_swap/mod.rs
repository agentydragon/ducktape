//! Vendor-swap end-to-end coverage. Builds a tiny snapshot chunk that
//! re-exports a synthetic upstream package, runs the swap pipeline, and
//! asserts the generated wrapper.

mod bundled_partial_swap;
mod export_names;
mod full_swap;
mod island_registration;
mod partial_swap;
mod partial_swap_kinds;
mod reentry;

use debundle_e2e_support::{
    CaseWorkspace, CommandResult, assert_node_output, assert_source_file, read_json,
    run_debundler_with_args, write_text_file, write_yaml_file,
};
use serde_json::{Value, json};
use spec::vendor_marks::{
    BundledPartialSwapBundle, BundledPartialSwapMark, BundledPartialSwapPackage, PartialSwapMark,
    PartialSwapPackage, PartialSwapSymbol, SwapMark, VendorLevel, VendorMark,
};
use spec::{PartialSwapKind, WrapperShape};
use std::fs;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::path::{Path, PathBuf};

#[track_caller]
fn assert_success(result: &CommandResult) {
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr
    );
}

/// Assert that the debundler refused the input and that stderr contains every diagnostic.
#[track_caller]
fn assert_rejected(result: &CommandResult, diagnostics: &[&str]) {
    assert!(
        !result.status.success(),
        "expected a rejection, got exit status {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr
    );
    for diagnostic in diagnostics {
        assert!(
            result.stderr.contains(diagnostic),
            "stderr lacks {diagnostic:?}\nstderr:\n{}",
            result.stderr
        );
    }
}

/// Run one case of a table, naming it when an assertion inside fails.
fn in_case(case: &str, check: impl FnOnce()) {
    if let Err(panic) = catch_unwind(AssertUnwindSafe(check)) {
        eprintln!("case {case:?} failed");
        resume_unwind(panic);
    }
}

/// Run `script` as an ES module placed in `dir`.
fn assert_probe_output(dir: &Path, script: &str, expected: &str) {
    let probe = dir.join("__probe.mjs");
    write_text_file(&probe, script);
    assert_node_output(&probe, expected, "");
}

/// Provide a minimal ESM `node_modules/<name>` in the emitted app root so
/// node can resolve the bare package specifiers the swap rewrites emit.
fn install_node_module(app_root: &Path, name: &str, version: &str, source: &str) {
    write_text_file(
        &app_root.join(format!("node_modules/{name}/package.json")),
        &format!(
            "{{ \"name\": \"{name}\", \"version\": \"{version}\", \"type\": \"module\", \"main\": \"index.js\" }}\n"
        ),
    );
    write_text_file(
        &app_root.join(format!("node_modules/{name}/index.js")),
        source,
    );
}

struct VendorTestWorkspace {
    root: CaseWorkspace,
    snapshot_root: PathBuf,
    out_root: PathBuf,
    wrapper_root: PathBuf,
    manifest_path: PathBuf,
    js_list_path: PathBuf,
}

impl VendorTestWorkspace {
    fn new() -> Self {
        let root = CaseWorkspace::default();
        let workspace_root = root.path().join("workspace");
        let snapshot_root = workspace_root.join("snapshot");
        let extracted_root = workspace_root.join("extracted");
        let out_root = workspace_root.join("out");
        let wrapper_root = out_root.join("app").join("vendors").join("generated");
        let manifest_path = out_root.join("reports").join("vendor_swaps.json");
        let js_list_path = extracted_root.join("js-files.txt");
        Self {
            root,
            snapshot_root,
            out_root,
            wrapper_root,
            manifest_path,
            js_list_path,
        }
    }

    fn transform_spec(&self, vendor: impl serde::Serialize) -> Value {
        json!({
            "vendor": vendor,
            "inputs": { "input_root": self.snapshot_root, "js_list_path": self.js_list_path },
            "swap_vendor_chunks": { "output_manifest_path": self.manifest_path, "output_wrapper_dir": self.wrapper_root, "write": true },
            "write_js_tree": { "out_dir": self.out_root },
        })
    }

    /// [`Self::transform_spec`] with `extra`'s top-level keys merged in.
    fn transform_spec_with(&self, vendor: impl serde::Serialize, extra: Value) -> Value {
        let mut spec = self.transform_spec(vendor);
        let Value::Object(extras) = extra else {
            panic!("extra spec keys must be an object: {extra}");
        };
        spec.as_object_mut()
            .expect("spec is JSON object")
            .extend(extras);
        spec
    }

    fn write_chunks(&self, chunks: &[(&str, &str)]) {
        let mut entries = String::new();
        for (chunk_path, source) in chunks {
            write_text_file(&self.snapshot_root.join(chunk_path), source);
            entries.push_str(chunk_path);
            entries.push('\n');
        }
        write_text_file(&self.js_list_path, &entries);
    }

    fn write_upstream_package(
        &self,
        rel_path: &str,
        package_name: &str,
        version: &str,
        subpath: &str,
        source: &str,
    ) -> PathBuf {
        let package_root = self.root.path().join(rel_path);
        write_text_file(
            &package_root.join("package.json"),
            &format!(
                "{}\n",
                serde_json::to_string_pretty(&json!({
                    "name": package_name,
                    "version": version,
                }))
                .unwrap(),
            ),
        );
        write_text_file(&package_root.join(subpath), source);
        package_root
    }

    fn wrapper_path_for_chunk(&self, chunk_path: &str) -> PathBuf {
        let chunk_id = chunk_path.strip_suffix(".js").unwrap_or(chunk_path);
        self.wrapper_root.join(chunk_id).join("entry.js")
    }

    /// Write `source` to `<root>/external-bundles/<filename>` and return the
    /// absolute path a `bundled_partial_swap` mark's `bundle.path` names.
    fn write_bundle(&self, filename: &str, source: &str) -> PathBuf {
        let bundle_path = self.root.path().join("external-bundles").join(filename);
        write_text_file(&bundle_path, source);
        bundle_path
    }

    fn run(&self, vendor: impl serde::Serialize, packages: &[(&str, &Path)]) -> CommandResult {
        self.run_spec(&self.transform_spec(vendor), packages)
    }

    fn run_spec(&self, spec: &Value, packages: &[(&str, &Path)]) -> CommandResult {
        self.run_spec_with_args(spec, packages, &[])
    }

    fn run_spec_with_args(
        &self,
        spec: &Value,
        packages: &[(&str, &Path)],
        extra_args: &[&str],
    ) -> CommandResult {
        let spec_path = self.root.path().join("transform_spec.yaml");
        write_yaml_file(&spec_path, spec);
        run_debundler_with_args(&spec_path, packages, extra_args)
    }
}

/// Set up a workspace for a bundled_partial_swap fixture: the chunks, the
/// js-list, and the bundle file whose path the spec names.
fn setup_bundled_partial_swap(
    bundle_filename: &str,
    bundle_source: &str,
    chunks: &[(&str, &str)],
) -> (VendorTestWorkspace, PathBuf) {
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(chunks);
    let bundle_path = ws.write_bundle(bundle_filename, bundle_source);
    (ws, bundle_path)
}

fn member(name: &str) -> Value {
    json!({"name": name, "selector": {"binding": {"name": name}}})
}

/// Spec keys materializing `modules` of chunk `static/app` in the listed
/// import order; the rest of the chunk stays inline in its entry.
fn materialize_app_modules(modules: &[(&str, &[&str])], materialize: Value) -> Value {
    let logical: serde_json::Map<String, Value> = modules
        .iter()
        .map(|(module, members)| {
            let members: Vec<Value> = members.iter().map(|name| member(name)).collect();
            ((*module).to_owned(), json!({ "members": members }))
        })
        .collect();
    let order: Vec<&str> = modules.iter().map(|(module, _)| *module).collect();
    json!({
        "logical_modules": { "static/app": logical },
        "import_order": { "static/app": order },
        "unassigned_mode": { "static/app": { "kind": "inline_in_entry" } },
        "materialize_logical_modules": materialize,
    })
}

fn partial_package(version: &str, subpath: &str, namespace: Option<&str>) -> PartialSwapPackage {
    PartialSwapPackage {
        version: version.into(),
        subpath: subpath.into(),
        namespace: namespace.map(str::to_owned),
    }
}

fn bundled_package(
    version: &str,
    subpath: &str,
    bundle_export: &str,
    namespace: Option<&str>,
) -> BundledPartialSwapPackage {
    BundledPartialSwapPackage {
        version: version.into(),
        subpath: subpath.into(),
        bundle_export: bundle_export.into(),
        namespace: namespace.map(str::to_owned),
    }
}

fn swap_symbol(
    package: &str,
    kind: PartialSwapKind,
    upstream_export: Option<&str>,
    local: Option<&str>,
) -> PartialSwapSymbol {
    PartialSwapSymbol {
        package: package.into(),
        kind,
        upstream_export: upstream_export.map(str::to_owned),
        local: local.map(str::to_owned),
    }
}

fn partial_swap_vendor(
    identity: &str,
    packages: &[(&str, PartialSwapPackage)],
    symbols: &[(&str, PartialSwapSymbol)],
) -> VendorMark {
    VendorMark {
        identity: identity.into(),
        level: VendorLevel::PartialSwap(PartialSwapMark {
            packages: packages
                .iter()
                .map(|(name, value)| ((*name).into(), value.clone()))
                .collect(),
            symbols: symbols
                .iter()
                .map(|(name, value)| ((*name).into(), value.clone()))
                .collect(),
        }),
    }
}

fn bundled_vendor(
    identity: &str,
    bundle: &Path,
    packages: &[(&str, BundledPartialSwapPackage)],
    symbols: &[(&str, PartialSwapSymbol)],
) -> VendorMark {
    VendorMark {
        identity: identity.into(),
        level: VendorLevel::BundledPartialSwap(BundledPartialSwapMark {
            bundle: BundledPartialSwapBundle {
                path: bundle.into(),
            },
            packages: packages
                .iter()
                .map(|(name, value)| ((*name).into(), value.clone()))
                .collect(),
            symbols: symbols
                .iter()
                .map(|(name, value)| ((*name).into(), value.clone()))
                .collect(),
        }),
    }
}

// ─── full swap ──────────────────────────────────────────────────────────

struct VendorSwapFixture {
    result: CommandResult,
    chunk_path: String,
    wrapper_path: PathBuf,
    manifest_path: PathBuf,
    _root: CaseWorkspace,
}

/// Fully swap chunk `static/lib-X.js` for package `lib`.
/// `default_export_aliases` are upstream named exports asserted as
/// package-default aliases; `wrapper_shape: None` is the wrapper-less path.
fn full_swap(
    chunk_source: &str,
    wrapper_shape: Option<WrapperShape>,
    upstream_source: &str,
    default_export_aliases: &[&str],
) -> VendorSwapFixture {
    const PACKAGE_NAME: &str = "lib";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "dist/index.mjs";
    const CHUNK_PATH: &str = "static/lib-X.js";

    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[(CHUNK_PATH, chunk_source)]);
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        upstream_source,
    );

    let vendor_mark = VendorMark {
        identity: format!("{PACKAGE_NAME}/{SUBPATH}"),
        level: VendorLevel::Swap(SwapMark {
            package: PACKAGE_NAME.into(),
            version: PACKAGE_VERSION.into(),
            subpath: SUBPATH.into(),
            wrapper_shape,
            default_export_aliases: default_export_aliases.iter().map(|s| (*s).into()).collect(),
        }),
    };
    let result = ws.run(
        json!({
            CHUNK_PATH: vendor_mark,
        }),
        &[(PACKAGE_NAME, &package_root)],
    );
    VendorSwapFixture {
        result,
        chunk_path: CHUNK_PATH.to_string(),
        wrapper_path: ws.wrapper_path_for_chunk(CHUNK_PATH),
        manifest_path: ws.manifest_path.clone(),
        _root: ws.root,
    }
}

fn assert_wrapper_output(fixture: &VendorSwapFixture, script: &str, expected: &str) {
    assert_success(&fixture.result);
    let probe = fixture.wrapper_path.with_file_name("probe.mjs");
    write_text_file(&probe, script);
    assert_node_output(&probe, expected, "");
}

// ─── partial swap ───────────────────────────────────────────────────────
//
// `level: partial_swap` rewrites per-symbol imports out of a chunk that
// the spec wants to keep on disk (un-swapped symbols stay imported from
// the chunk). Each rewritten symbol gets its local references replaced
// with `<namespace>.<upstream_export>` and one
// `import * as <namespace> from "<package>"` is emitted per file.
//
// After the consumer side is rewritten, the strip pass drops the swapped
// names from the vendor chunk's own export surface and sweeps top-level
// bindings that are no longer reachable, refusing when that would lose an
// effect residual code observes.

const ZOD_TRUE: &str = "export const boolean = () => true;\n";
const ZOD_ONE: &str = "export const boolean = () => 1;\n";
/// Imports swapped `e6` and retained `keepMe` without calling them at load.
const ALIASED_CALLER: &str = "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n";
const SWAPPED_ONLY_CALLER: &str = "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n";
/// Prints `e6()` and `keepMe()` at load.
const LOGGING_CALLER: &str =
    "import { e6, keepMe } from '../megachunk/entry.js';\nconsole.log(e6(), keepMe());\n";

struct PartialSwapFixture {
    result: CommandResult,
    caller_emitted_path: PathBuf,
    megachunk_emitted_path: PathBuf,
    _root: CaseWorkspace,
}

impl PartialSwapFixture {
    /// Assert success and install `source` as the emitted app's
    /// `node_modules/<name>`; returns the emitted app root.
    #[track_caller]
    fn install_emitted(&self, name: &str, version: &str, source: &str) -> PathBuf {
        assert_success(&self.result);
        let app_root = self
            .caller_emitted_path
            .ancestors()
            .nth(3)
            .expect("app root above static/app/entry.js")
            .to_path_buf();
        install_node_module(&app_root, name, version, source);
        app_root
    }

    /// Run the emitted caller entry against `upstream` installed as `zod`.
    #[track_caller]
    fn assert_zod_caller_output(&self, upstream: &str, expected: &str) {
        self.install_emitted("zod", "3.23.8", upstream);
        assert_node_output(&self.caller_emitted_path, expected, "");
    }
}

/// Chunk `static/megachunk.js`, caller `static/app.js`, and the upstream
/// package installed under `upstream/<package_name>`.
fn setup_partial_swap_consumer_fixture(
    chunk_source: &str,
    caller_source: &str,
    package_name: &str,
    version: &str,
    subpath: &str,
    upstream_source: &str,
) -> (VendorTestWorkspace, PathBuf) {
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        ("static/megachunk.js", chunk_source),
        ("static/app.js", caller_source),
    ]);
    let package_root = ws.write_upstream_package(
        &format!("upstream/{package_name}"),
        package_name,
        version,
        subpath,
        upstream_source,
    );
    (ws, package_root)
}

fn run_partial_swap_with_mark(
    ws: VendorTestWorkspace,
    vendor_spec: impl serde::Serialize,
    packages: &[(&str, &Path)],
) -> PartialSwapFixture {
    let result = ws.run(json!({ "static/megachunk.js": vendor_spec }), packages);
    PartialSwapFixture {
        result,
        caller_emitted_path: ws.out_root.join("app/static/app/entry.js"),
        megachunk_emitted_path: ws.out_root.join("app/static/megachunk/entry.js"),
        _root: ws.root,
    }
}

/// Member-swap chunk export `symbol.0` to `z.<symbol.1>` of package `zod`.
/// The installed package is 3.23.8; `spec_version` is what the mark requests.
fn run_partial_swap_fixture(
    chunk_source: &str,
    caller_source: &str,
    upstream_source: &str,
    symbol: (&str, &str),
    spec_version: &str,
) -> PartialSwapFixture {
    const SUBPATH: &str = "lib/index.mjs";
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        chunk_source,
        caller_source,
        "zod",
        "3.23.8",
        SUBPATH,
        upstream_source,
    );
    let vendor = partial_swap_vendor(
        "megachunk partial swap fixture",
        &[("zod", partial_package(spec_version, SUBPATH, Some("z")))],
        &[(
            symbol.0,
            swap_symbol("zod", PartialSwapKind::Member, Some(symbol.1), None),
        )],
    );
    run_partial_swap_with_mark(ws, vendor, &[("zod", &package_root)])
}

/// The common partial swap: chunk export `e6` → `z.boolean`.
fn zod_swap(chunk_source: &str, caller_source: &str, upstream_source: &str) -> PartialSwapFixture {
    run_partial_swap_fixture(
        chunk_source,
        caller_source,
        upstream_source,
        ("e6", "boolean"),
        "3.23.8",
    )
}

struct PartialSwapKindFixtureArgs<'a> {
    kind: PartialSwapKind,
    package_name: &'a str,
    package_version: &'a str,
    subpath: &'a str,
    chunk_source: &'a str,
    caller_source: &'a str,
    upstream_source: &'a str,
    chunk_export: &'a str,
    /// Required for `kind: named` and `member`. None for `namespace` / `default`.
    upstream_export: Option<&'a str>,
}

fn run_partial_swap_kind_fixture(args: PartialSwapKindFixtureArgs<'_>) -> PartialSwapFixture {
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        args.chunk_source,
        args.caller_source,
        args.package_name,
        args.package_version,
        args.subpath,
        args.upstream_source,
    );
    let vendor = partial_swap_vendor(
        &format!("megachunk {:?} swap fixture", args.kind),
        &[(
            args.package_name,
            partial_package(
                args.package_version,
                args.subpath,
                (args.kind == PartialSwapKind::Member).then_some("Upstream"),
            ),
        )],
        &[(
            args.chunk_export,
            swap_symbol(args.package_name, args.kind, args.upstream_export, None),
        )],
    );

    run_partial_swap_with_mark(ws, vendor, &[(args.package_name, &package_root)])
}
