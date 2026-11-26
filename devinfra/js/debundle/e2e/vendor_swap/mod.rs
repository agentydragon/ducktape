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
use std::path::{Path, PathBuf};

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
    assert_rejected_case("debundler", result, diagnostics);
}

/// [`assert_rejected`] for one case of a table: `case` prefixes the failure messages.
#[track_caller]
fn assert_rejected_case(case: &str, result: &CommandResult, diagnostics: &[&str]) {
    assert!(
        !result.status.success(),
        "{case}: expected a rejection, got exit status {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr
    );
    for diagnostic in diagnostics {
        assert!(
            result.stderr.contains(diagnostic),
            "{case}: stderr lacks {diagnostic:?}\nstderr:\n{}",
            result.stderr
        );
    }
}

struct VendorSwapFixture {
    result: CommandResult,
    chunk_path: String,
    wrapper_path: PathBuf,
    manifest_path: PathBuf,
    _root: CaseWorkspace,
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

    fn write_chunk(&self, chunk_path: &str, source: &str) {
        write_text_file(&self.snapshot_root.join(chunk_path), source);
    }

    fn write_chunks(&self, chunks: &[(&str, &str)]) {
        let mut entries = String::new();
        for (chunk_path, source) in chunks {
            self.write_chunk(chunk_path, source);
            entries.push_str(chunk_path);
            entries.push('\n');
        }
        self.write_js_list(&entries);
    }

    fn write_js_list(&self, entries: &str) {
        write_text_file(&self.js_list_path, entries);
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

    /// Write `source` to `<root>/external-bundles/<filename>` and
    /// return the absolute bundle path. Used by bundled_partial_swap
    /// fixtures whose spec carries a `bundle: { path: ... }` block.
    fn write_bundle(&self, filename: &str, source: &str) -> PathBuf {
        let bundle_root = self.root.path().join("external-bundles");
        let bundle_path = bundle_root.join(filename);
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

/// Set up a workspace for a bundled_partial_swap fixture, writing the
/// chunks (`snapshot_root/<path> = source`), the js-list, and the
/// bundle file. The returned tuple `(ws, bundle_path)` is everything
/// downstream wiring needs to compose the spec.
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

/// Build the common `inputs` / `swap_vendor_chunks` / `write_js_tree`
/// blocks for a bundled_partial_swap spec, given an outer `vendor:`
/// block authored by the caller. `extra` is merged onto the top-level
/// spec object — used by the materialization-rewrite test which adds
/// `logical_modules` / `unassigned_mode` / `materialize_logical_modules`.
fn build_bundled_partial_swap_spec(
    ws: &VendorTestWorkspace,
    vendor: Value,
    extra: Option<Value>,
) -> Value {
    let mut spec = ws.transform_spec(vendor);
    if let Some(Value::Object(extras)) = extra {
        let object = spec.as_object_mut().expect("spec is JSON object");
        for (k, v) in extras {
            object.insert(k, v);
        }
    }
    spec
}

struct FullSwapFixtureArgs<'a> {
    chunk_source: &'a str,
    /// `None` runs the plain (wrapper-less) swap path.
    wrapper_shape: Option<WrapperShape>,
    upstream_source: &'a str,
    /// Upstream named exports asserted as package-default aliases
    /// (`SwapMark::default_export_aliases`). Empty for most fixtures.
    default_export_aliases: &'a [&'a str],
}

fn run_full_swap_fixture(args: FullSwapFixtureArgs<'_>) -> VendorSwapFixture {
    const PACKAGE_NAME: &str = "lib";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "dist/index.mjs";
    const CHUNK_PATH: &str = "static/lib-X.js";

    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[(CHUNK_PATH, args.chunk_source)]);
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        args.upstream_source,
    );

    let vendor_mark = VendorMark {
        identity: format!("{PACKAGE_NAME}/{SUBPATH}"),
        level: VendorLevel::Swap(SwapMark {
            package: PACKAGE_NAME.into(),
            version: PACKAGE_VERSION.into(),
            subpath: SUBPATH.into(),
            wrapper_shape: args.wrapper_shape,
            default_export_aliases: args
                .default_export_aliases
                .iter()
                .map(|s| (*s).into())
                .collect(),
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

fn run_island_registration_fixture(chunk_source: &str) -> PartialSwapFixture {
    run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source,
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    })
}

struct PartialSwapFixtureArgs<'a> {
    chunk_source: &'a str,
    caller_source: &'a str,
    upstream_source: &'a str,
    /// (chunk_export, package_name, upstream_export) tuples. The package
    /// `zod` is wired by the fixture below.
    symbols: Vec<(&'a str, &'a str, &'a str)>,
    upstream_version: &'a str,
}

struct PartialSwapFixture {
    result: CommandResult,
    caller_emitted_path: PathBuf,
    megachunk_emitted_path: PathBuf,
    _root: CaseWorkspace,
}

fn run_partial_swap_with_mark(
    ws: VendorTestWorkspace,
    vendor_spec: impl serde::Serialize,
    packages: &[(&str, &Path)],
) -> PartialSwapFixture {
    const MEGACHUNK_PATH: &str = "static/megachunk.js";

    let result = ws.run(
        json!({
            MEGACHUNK_PATH: vendor_spec,
        }),
        packages,
    );

    let caller_emitted_path = ws.out_root.join("app/static/app").join("entry.js");
    let megachunk_emitted_path = ws.out_root.join("app/static/megachunk").join("entry.js");

    PartialSwapFixture {
        result,
        caller_emitted_path,
        megachunk_emitted_path,
        _root: ws.root,
    }
}

fn run_partial_swap_fixture(args: PartialSwapFixtureArgs<'_>) -> PartialSwapFixture {
    const PACKAGE_NAME: &str = "zod";
    const SUBPATH: &str = "lib/index.mjs";

    // Keep the installed version fixed: mismatch cases deliberately request
    // a different version in the spec.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        args.chunk_source,
        args.caller_source,
        PACKAGE_NAME,
        "3.23.8",
        SUBPATH,
        args.upstream_source,
    );
    let symbols: Vec<_> = args
        .symbols
        .iter()
        .map(|(export, package, upstream)| {
            (
                *export,
                swap_symbol(package, PartialSwapKind::Member, Some(upstream), None),
            )
        })
        .collect();
    let vendor = partial_swap_vendor(
        "megachunk partial swap fixture",
        &[(
            PACKAGE_NAME,
            partial_package(args.upstream_version, SUBPATH, Some("z")),
        )],
        &symbols,
    );
    run_partial_swap_with_mark(ws, vendor, &[(PACKAGE_NAME, &package_root)])
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

// ─── analysis of swapped implementations ────────────────────────────────
//
// A swap promises the replacement behaves like the bundled implementation
// (SPEC.md § Vendor swap equivalence), so facts derived from the bundled
// body justify splits around calls that run the replacement.

fn setup_partial_swap_consumer_fixture(
    chunk_source: &str,
    caller_source: &str,
    package_name: &str,
    version: &str,
    subpath: &str,
    upstream_source: &str,
) -> (VendorTestWorkspace, PathBuf) {
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[(MEGACHUNK_PATH, chunk_source), (CALLER_PATH, caller_source)]);
    let package_root = ws.write_upstream_package(
        &format!("upstream/{package_name}"),
        package_name,
        version,
        subpath,
        upstream_source,
    );
    (ws, package_root)
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

fn partial_package(version: &str, subpath: &str, namespace: Option<&str>) -> PartialSwapPackage {
    PartialSwapPackage {
        version: version.into(),
        subpath: subpath.into(),
        namespace: namespace.map(str::to_owned),
    }
}

fn emitted_app_root(fixture: &PartialSwapFixture) -> PathBuf {
    fixture
        .caller_emitted_path
        .ancestors()
        .nth(3)
        .expect("app root above static/app/entry.js")
        .to_path_buf()
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
