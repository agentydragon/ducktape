//! Vendor-swap end-to-end coverage. Builds a tiny snapshot chunk that
//! re-exports a synthetic upstream package, runs the swap pipeline, and
//! asserts the generated wrapper.

use debundle_e2e_support::{CommandResult, run_debundler, write_text_file, write_yaml_file};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
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

    let fixture = run_named_from_module_default_fixture(upstream_source);

    assert_wrapper_named_from_module_default(&fixture);
}

#[test]
fn named_from_module_default_handles_export_named_as_default_before_local_decl() {
    // ESM allows `export { lib as default };` to appear *before* the local
    // declaration. The generated wrapper's
    // `const __vendor_default__ = lib;` must therefore live after the
    // declaration to avoid a TDZ at module init.
    let upstream_source = r#"export { lib as default };
const lib = { ping() { return "pong"; } };
"#;

    let fixture = run_named_from_module_default_fixture(upstream_source);

    assert_wrapper_named_from_module_default(&fixture);
}

#[test]
fn named_from_module_default_handles_anonymous_default_function() {
    // `export default function () { ... }` collapses into
    // `const __vendor_default__ = function () { ... };`.
    let upstream_source = r#"export default function () { return "ping"; }
"#;

    let fixture = run_named_from_module_default_fixture(upstream_source);

    assert_wrapper_named_from_module_default(&fixture);
}

#[test]
fn named_from_module_default_handles_anonymous_default_class() {
    // `export default class { ... }` collapses into
    // `const __vendor_default__ = class { ... };`.
    let upstream_source = r#"export default class {
  ping() { return "ping"; }
}
"#;

    let fixture = run_named_from_module_default_fixture(upstream_source);

    assert_wrapper_named_from_module_default(&fixture);
}

fn assert_wrapper_named_from_module_default(fixture: &VendorSwapFixture) {
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

    // Cross-check the resolution manifest: `generated_wrapper_path` is recorded
    // relative to the manifest's own directory, so resolving it against
    // `dirname(manifest_path)` must produce the wrapper file's actual
    // on-disk path.
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(&fixture.manifest_path).expect("manifest exists"))
            .expect("manifest parses as JSON");
    let recorded = manifest
        .get("resolutions")
        .and_then(|r| r.get(&fixture.chunk_path))
        .and_then(|r| r.get("generated_wrapper_path"))
        .and_then(Value::as_str)
        .expect("manifest records generated_wrapper_path");
    let manifest_dir = fixture
        .manifest_path
        .parent()
        .expect("manifest path has a parent");
    let resolved = manifest_dir.join(recorded);
    assert_eq!(
        fs::canonicalize(&resolved).expect("resolved manifest path canonicalizes"),
        fs::canonicalize(&fixture.wrapper_path).expect("wrapper path canonicalizes"),
        "manifest's generated_wrapper_path ({recorded}) must resolve to the wrapper file ({:?})",
        fixture.wrapper_path,
    );
}

struct VendorSwapFixture {
    result: CommandResult,
    chunk_path: String,
    wrapper_path: PathBuf,
    manifest_path: PathBuf,
    _root: TempDir,
}

fn run_named_from_module_default_fixture(upstream_source: &str) -> VendorSwapFixture {
    const PACKAGE_NAME: &str = "lib";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "dist/index.mjs";
    const CHUNK_PATH: &str = "static/lib-X.js";

    let root =
        TempDir::with_prefix("vendor-swap-named-from-module-default-").expect("create tempdir");
    // Output paths the binary writes to are absolute (the new contract — no
    // workspace lookup in the binary). The vendor manifest lives at
    // `<root>/workspace/vendors/manifest.json`; wrappers go to its sibling
    // `generated/<chunk_id>/<entry>`, so manifest-relative emission produces
    // `generated/<chunk_id>/<entry>` strings.
    let workspace_root = root.path().join("workspace");
    let extracted_root = workspace_root.join("extracted");
    let snapshot_root = workspace_root.join("snapshot");
    let out_root = workspace_root.join("out");
    let wrapper_root = workspace_root.join("vendors").join("generated");
    let manifest_path = workspace_root.join("vendors").join("manifest.json");
    let package_root = root.path().join("upstream");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&wrapper_root).unwrap();
    fs::create_dir_all(&package_root).unwrap();
    fs::create_dir_all(snapshot_root.join(Path::new(CHUNK_PATH).parent().unwrap())).unwrap();
    fs::create_dir_all(package_root.join("dist")).unwrap();

    write_text_file(
        &snapshot_root.join(CHUNK_PATH),
        "export { x as default };\nconst x = 0;\n",
    );
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
    write_text_file(&package_root.join(SUBPATH), upstream_source);

    let spec_path = root.path().join("transform_spec.yaml");
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
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);

    // Wrapper layout: <output_wrapper_dir>/<chunk_id>/<entry_file>. chunk_id
    // is the chunk path with the trailing `.js` stripped; the normalized
    // chunk's entry file is always `entry.js`.
    let chunk_id = CHUNK_PATH.strip_suffix(".js").unwrap_or(CHUNK_PATH);
    let wrapper_path = wrapper_root.join(chunk_id).join("entry.js");
    VendorSwapFixture {
        result,
        chunk_path: CHUNK_PATH.to_string(),
        wrapper_path,
        manifest_path,
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
        "vendor": {
            args.chunk_path: {
                "level": "swap",
                "identity": format!("{}/{}", args.package_name, args.subpath),
                "package": args.package_name,
                "version": args.package_version,
                "subpath": args.subpath,
                "wrapper_shape": "named_from_module_default",
            },
        },
        "inputs": { "input_root": args.snapshot_root, "js_list_path": args.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": args.manifest_path,
            "output_wrapper_dir": args.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "force": true, "out_dir": args.out_root },
    })
}

#[test]
fn named_from_default_handles_object_literal_with_keyvalue_props() {
    // Canonical accepted shape: upstream's `export default` is an
    // object literal whose keys are `KeyValue` props with `Ident` (or
    // `Str`) names. The wrapper picks up each key and re-exports it as
    // a named export.
    let upstream_source = r#"export default {
  ping: () => "pong",
  pong: () => "ping",
};
"#;

    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source,
        chunk_source: "export { ping, pong } from \"lib\";\n",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );

    let wrapper_source = fs::read_to_string(&fixture.wrapper_path).expect("wrapper exists");
    // The wrapper hoists upstream's default into a `const _d = { ... }`
    // and re-emits each named export as `export const ping = _d.ping;`.
    assert!(
        wrapper_source.contains("export const ping = _d.ping"),
        "wrapper should emit a named-export pull for `ping`:\n{wrapper_source}",
    );
    assert!(
        wrapper_source.contains("export const pong = _d.pong"),
        "wrapper should emit a named-export pull for `pong`:\n{wrapper_source}",
    );
    assert!(
        wrapper_source.contains("export default _d"),
        "wrapper should preserve the default export:\n{wrapper_source}",
    );
}

#[test]
fn named_from_default_accepts_shorthand_props() {
    // Shorthand object-literal props (`{ ping, pong }` — local
    // binding names used directly as both key and value) produce
    // the same wrapper shape as `KeyValue` props: each shorthand
    // key reflects a data property on the default export whose
    // value is the local binding, so `export const K = _d.K;`
    // re-exports the right value. Real-world vendor `index.mjs`
    // files use shorthand commonly; accepting it removes an
    // otherwise-unmotivated authoring requirement.
    let upstream_source = r#"const ping = () => "pong";
const pong = () => "ping";
export default { ping, pong };
"#;

    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source,
        chunk_source: "export { ping, pong } from \"lib\";\n",
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
        wrapper_source.contains("export const ping = _d.ping"),
        "wrapper should emit a named-export pull for `ping`:\n{wrapper_source}",
    );
    assert!(
        wrapper_source.contains("export const pong = _d.pong"),
        "wrapper should emit a named-export pull for `pong`:\n{wrapper_source}",
    );
    assert!(
        wrapper_source.contains("export default _d"),
        "wrapper should preserve the default export:\n{wrapper_source}",
    );
}

#[test]
fn named_from_default_accepts_mixed_keyvalue_and_shorthand_props() {
    // Mixed shape — KeyValue + Shorthand in the same default
    // export. Both shapes contribute their keys to the wrapper's
    // named-export set.
    let upstream_source = r#"const ping = () => "pong";
export default { ping, "pong": () => "ping" };
"#;

    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source,
        chunk_source: "export { ping, pong } from \"lib\";\n",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let wrapper_source = fs::read_to_string(&fixture.wrapper_path).expect("wrapper exists");
    assert!(wrapper_source.contains("export const ping = _d.ping"));
    assert!(wrapper_source.contains("export const pong = _d.pong"));
}

#[test]
fn named_from_default_rejects_non_object_literal_default() {
    // `export default class { ... }` is an `ExportDefaultDecl`, not
    // an `ExportDefaultExpr`, so the search bails with "no export
    // default declaration" before the object-literal check.
    //
    // This is intentionally out-of-scope: the `named_from_default`
    // wrapper re-exports via `_d.K`, which on a class default would
    // resolve to STATIC properties only (instance methods sit on
    // `_d.prototype`). Function defaults are similar — `_d.K` reads
    // a property on the function object, which is the wrong access
    // path for anything semantically useful in the common case.
    // Vendor authors with `export default class` / `export default
    // function` shapes should use the `named_from_module_default`
    // wrapper instead (which handles anonymous default fn/class via
    // `DefaultDecl::{Fn,Class}` — see
    // `named_from_module_default_handles_anonymous_default_class`).
    let upstream_source = r#"export default class {
  ping() { return "pong"; }
}
"#;

    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source,
        chunk_source: "export { ping } from \"lib\";\n",
    });

    assert!(
        !fixture.result.status.success(),
        "debundler should fail on class default for named-from-default shape",
    );
    assert!(
        fixture
            .result
            .stderr
            .contains("named-from-default: upstream has no export default declaration"),
        "expected no-export-default error; got stderr:\n{}",
        fixture.result.stderr,
    );
}

struct NamedFromDefaultFixtureArgs<'a> {
    upstream_source: &'a str,
    chunk_source: &'a str,
}

fn run_named_from_default_fixture(args: NamedFromDefaultFixtureArgs<'_>) -> VendorSwapFixture {
    const PACKAGE_NAME: &str = "lib";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "dist/index.mjs";
    const CHUNK_PATH: &str = "static/lib-X.js";

    let root = TempDir::with_prefix("vendor-swap-named-from-default-").expect("create tempdir");
    let workspace_root = root.path().join("workspace");
    let extracted_root = workspace_root.join("extracted");
    let snapshot_root = workspace_root.join("snapshot");
    let out_root = workspace_root.join("out");
    let wrapper_root = workspace_root.join("vendors").join("generated");
    let manifest_path = workspace_root.join("vendors").join("manifest.json");
    let package_root = root.path().join("upstream");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&wrapper_root).unwrap();
    fs::create_dir_all(&package_root).unwrap();
    fs::create_dir_all(snapshot_root.join(Path::new(CHUNK_PATH).parent().unwrap())).unwrap();
    fs::create_dir_all(package_root.join("dist")).unwrap();

    write_text_file(&snapshot_root.join(CHUNK_PATH), args.chunk_source);
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
    write_text_file(&package_root.join(SUBPATH), args.upstream_source);

    let spec_path = root.path().join("transform_spec.yaml");
    let spec = build_named_from_default_spec(BuildSpecArgs {
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
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);

    let chunk_id = CHUNK_PATH.strip_suffix(".js").unwrap_or(CHUNK_PATH);
    let wrapper_path = wrapper_root.join(chunk_id).join("entry.js");
    VendorSwapFixture {
        result,
        chunk_path: CHUNK_PATH.to_string(),
        wrapper_path,
        manifest_path,
        _root: root,
    }
}

fn build_named_from_default_spec(args: BuildSpecArgs<'_>) -> Value {
    json!({
        "vendor": {
            args.chunk_path: {
                "level": "swap",
                "identity": format!("{}/{}", args.package_name, args.subpath),
                "package": args.package_name,
                "version": args.package_version,
                "subpath": args.subpath,
                "wrapper_shape": "named_from_default",
            },
        },
        "inputs": { "input_root": args.snapshot_root, "js_list_path": args.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": args.manifest_path,
            "output_wrapper_dir": args.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "force": true, "out_dir": args.out_root },
    })
}
