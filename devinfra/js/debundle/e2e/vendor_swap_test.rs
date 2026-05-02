//! Vendor-swap end-to-end coverage. Builds a tiny snapshot chunk that
//! re-exports a synthetic upstream package, runs the swap pipeline, and
//! asserts the generated wrapper.

use debundle_e2e_support::{CommandResult, run_debundler, write_json_file, write_text_file};
use serde_json::{Value, json};
use std::fs;
use std::path::Path;
use tempfile::TempDir;

#[test]
fn named_from_module_default_handles_export_named_as_default_re_export() {
    // Cytoscape v3.30.4 ships `export { cytoscape as default };` (a named
    // re-export of a local) instead of a bare `export default cytoscape;`.
    // The wrapper must accept the re-export form.
    let upstream_source = r#"const lib = { ping() { return "pong"; } };
const aux = "side";
export { lib as default, aux };
"#;

    let fixture = run_named_from_module_default_fixture(NamedFromModuleDefaultOpts {
        chunk_source: "export { x as default };\nconst x = 0;\n",
        upstream_source,
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );

    let wrapper_source = fs::read_to_string(&fixture.wrapper_path).expect("wrapper exists");
    assert!(
        wrapper_source.contains("ping"),
        "wrapper should retain the upstream local that becomes `default`:\n{wrapper_source}",
    );
    assert!(
        wrapper_source.contains("export default"),
        "wrapper should expose a `export default ...`:\n{wrapper_source}",
    );
    assert!(
        !wrapper_source.contains("as default"),
        "wrapper should not re-emit the original upstream `export {{ ... as default }}` once the wrapper has its own default export:\n{wrapper_source}",
    );
}

struct NamedFromModuleDefaultOpts<'a> {
    chunk_source: &'a str,
    upstream_source: &'a str,
}

struct VendorSwapFixture {
    result: CommandResult,
    wrapper_path: std::path::PathBuf,
    _root: TempDir,
}

fn run_named_from_module_default_fixture(
    opts: NamedFromModuleDefaultOpts<'_>,
) -> VendorSwapFixture {
    const PACKAGE_NAME: &str = "lib";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "dist/index.mjs";
    const CHUNK_PATH: &str = "static/lib-X.js";

    let root =
        TempDir::with_prefix("vendor-swap-named-from-module-default-").expect("create tempdir");
    let extracted_root = root.path().join("extracted");
    let snapshot_root = root.path().join("snapshot");
    let out_root = root.path().join("out");
    let wrapper_root = root.path().join("vendors");
    let manifest_path = wrapper_root.join("manifest.json");
    let package_root = root.path().join("upstream");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&wrapper_root).unwrap();
    fs::create_dir_all(&package_root).unwrap();
    fs::create_dir_all(snapshot_root.join(Path::new(CHUNK_PATH).parent().unwrap())).unwrap();
    fs::create_dir_all(package_root.join("dist")).unwrap();

    write_text_file(&snapshot_root.join(CHUNK_PATH), opts.chunk_source);
    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &format!("{CHUNK_PATH}\n"));

    write_text_file(
        &package_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&json!({
                "name": PACKAGE_NAME,
                "version": PACKAGE_VERSION,
            }))
            .unwrap(),
        ),
    );
    write_text_file(&package_root.join(SUBPATH), opts.upstream_source);

    let spec_path = root.path().join("transform_spec.jsonc");
    let spec = build_named_from_module_default_spec(BuildSpecArgs {
        chunk_path: CHUNK_PATH,
        js_list_path: &js_list_path,
        snapshot_root: &snapshot_root,
        out_root: &out_root,
        manifest_path: &manifest_path,
        wrapper_root: &wrapper_root,
        package_name: PACKAGE_NAME,
        package_version: PACKAGE_VERSION,
        subpath: SUBPATH,
    });
    write_json_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);

    // Wrapper layout: <output_wrapper_dir>/<chunk_id>/<entry_file>. chunk_id
    // is the chunk path with the trailing `.js` stripped; the normalized
    // chunk's entry file is always `entry.js`.
    let chunk_id = CHUNK_PATH.strip_suffix(".js").unwrap_or(CHUNK_PATH);
    let wrapper_path = wrapper_root.join(chunk_id).join("entry.js");
    VendorSwapFixture {
        result,
        wrapper_path,
        _root: root,
    }
}

struct BuildSpecArgs<'a> {
    chunk_path: &'a str,
    js_list_path: &'a Path,
    snapshot_root: &'a Path,
    out_root: &'a Path,
    manifest_path: &'a Path,
    wrapper_root: &'a Path,
    package_name: &'a str,
    package_version: &'a str,
    subpath: &'a str,
}

fn build_named_from_module_default_spec(args: BuildSpecArgs<'_>) -> Value {
    json!({
        "kind": "js.ast_transform_spec",
        "operations": [{
            "id": "mark_vendor_lib",
            "operation": "mark_vendor",
            "level": "swap",
            "chunkPath": args.chunk_path,
            "identity": format!("{}/{}", args.package_name, args.subpath),
            "upstreamFamily": "Lib",
            "package": args.package_name,
            "version": args.package_version,
            "subpath": args.subpath,
            "wrapperShape": "named-from-module-default",
            "confidence": "confirmed",
            "evidence": [{
                "path": args.chunk_path,
                "line": 1,
                "text": "export { x as default }",
            }],
        }],
        "pipeline": [
            {
                "id": "load",
                "operation": "load_js_chunks",
                "args": { "inputRoot": args.snapshot_root, "jsListPath": args.js_list_path },
            },
            { "id": "parse", "operation": "compute_js_asts" },
            { "id": "normalize", "operation": "normalize_js_chunks" },
            { "id": "annotate_vendor", "operation": "apply_vendor_annotations" },
            { "id": "rename_vendor", "operation": "rename_vendor_exports" },
            {
                "id": "swap_vendor",
                "operation": "swap_vendor_chunks",
                "args": {
                    "outputManifestPath": args.manifest_path,
                    "outputWrapperDir": args.wrapper_root,
                    "write": true,
                },
            },
            {
                "id": "write",
                "operation": "write_js_tree",
                "args": { "force": true, "outDir": args.out_root },
            },
        ],
    })
}
