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

// ─── partial_swap ───────────────────────────────────────────────────────
//
// `level: partial_swap` rewrites per-symbol imports out of a chunk that
// the spec wants to keep on disk (un-swapped symbols stay imported from
// the chunk). Each rewritten symbol gets its local references replaced
// with `<namespace>.<upstream_export>` and one
// `import * as <namespace> from "<package>"` is emitted per file.

#[test]
fn partial_swap_basic_rewrites_to_namespace_member() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );

    let caller_emitted = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller_emitted.contains("import * as z from \"zod\""),
        "caller should namespace-import the package:\n{caller_emitted}",
    );
    assert!(
        caller_emitted.contains("z.boolean()"),
        "caller should call z.boolean() instead of zodBoolean():\n{caller_emitted}",
    );
    assert!(
        !caller_emitted.contains("zodBoolean"),
        "caller should not retain the original local-alias identifier:\n{caller_emitted}",
    );
    assert!(
        caller_emitted.contains("keepMe as kept"),
        "caller should retain non-swapped imports unchanged:\n{caller_emitted}",
    );
    assert!(
        caller_emitted.contains("kept()"),
        "caller should still reference the kept import by its local name:\n{caller_emitted}",
    );
}

#[test]
fn partial_swap_keeps_megachunk_on_disk() {
    // Partial swap leaves the chunk in place so its non-swapped exports
    // remain reachable. Contrast with `level: swap` which removes the
    // chunk entirely. The megachunk file (`<out_dir>/static/megachunk/entry.js`)
    // must still exist after the pipeline runs.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.megachunk_emitted_path.exists(),
        "megachunk should still be emitted: {:?}",
        fixture.megachunk_emitted_path,
    );

    let partial_manifest_path = fixture.partial_manifest_path();
    let manifest: Value = serde_json::from_str(
        &fs::read_to_string(&partial_manifest_path).expect("partial manifest"),
    )
    .expect("partial manifest parses");
    let symbol_resolution = manifest
        .get("resolutions")
        .and_then(|r| r.get(&fixture.megachunk_chunk_path))
        .and_then(|r| r.get("symbols"))
        .and_then(|r| r.get("e6"))
        .expect("manifest records e6 symbol resolution");
    assert_eq!(
        symbol_resolution
            .get("references_rewritten")
            .and_then(Value::as_u64),
        Some(1),
        "partial-swap manifest should count rewritten references:\n{manifest:#}",
    );
}

// ─── strip swapped exports ───────────────────────────────────────────────
//
// After `apply_partial_vendor_swaps` rewrites the consumer side, the
// `strip_swapped_vendor_exports` stage drops the swapped names from the
// vendor chunk's own export surface and sweeps any top-level bindings
// that are no longer reachable. These tests cover both halves of that
// pass against the synthetic megachunk fixture.

#[test]
fn partial_swap_strips_swapped_names_from_export_block() {
    // `export { e6, keepMe }` form: stripping `e6` should rewrite the
    // export block to expose only `keepMe`. Bare `export const e6 = …`
    // is exercised by the implementation-DCE tests below.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const e6Impl = () => true;\nconst keepMeImpl = () => 7;\nexport { e6Impl as e6, keepMeImpl as keepMe };\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let emitted = fs::read_to_string(&fixture.megachunk_emitted_path).expect("megachunk emitted");
    assert!(
        !emitted.contains("e6Impl as e6"),
        "stripped name `e6` should be gone from the export block:\n{emitted}",
    );
    assert!(
        emitted.contains("keepMeImpl as keepMe"),
        "non-swapped name `keepMe` should still be exported:\n{emitted}",
    );
}

#[test]
fn partial_swap_strips_implementation_when_unreferenced() {
    // `export const e6 = …` with no cross-refs: after stripping the
    // export prefix, `const e6 = …` becomes a chunk-local pure binding
    // that nothing reads, so the DCE pass deletes it entirely.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let emitted = fs::read_to_string(&fixture.megachunk_emitted_path).expect("megachunk emitted");
    assert!(
        !emitted.contains(" e6 "),
        "swapped binding `e6` should be DCE'd:\n{emitted}",
    );
    assert!(
        emitted.contains("keepMe"),
        "non-swapped binding `keepMe` should remain:\n{emitted}",
    );
}

#[test]
fn partial_swap_keeps_implementation_when_cross_referenced() {
    // `object` (swapped) references `ZodObject`; `ZodObject` is still
    // exported. The export-strip drops `object`'s export; DCE finds
    // `ZodObject` reachable through its own export and keeps it; the
    // dead `object` declaration goes away.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "class ZodObject {}\nconst object = () => new ZodObject();\nexport { object, ZodObject };\n",
        caller_source: "import { object as zodObject, ZodObject as Z } from \"../megachunk/entry.js\";\nexport function go() { return zodObject() instanceof Z; }\n",
        upstream_source: "export const object = () => ({});\n",
        symbols: vec![("object", "zod", "object")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let emitted = fs::read_to_string(&fixture.megachunk_emitted_path).expect("megachunk emitted");
    assert!(
        emitted.contains("class ZodObject"),
        "still-exported `ZodObject` definition should remain:\n{emitted}",
    );
    assert!(
        emitted.contains("ZodObject"),
        "still-exported `ZodObject` should be in the export surface:\n{emitted}",
    );
    assert!(
        !emitted.contains("const object"),
        "dead `object` body should be DCE'd:\n{emitted}",
    );
}

#[test]
fn partial_swap_keeps_side_effect_init() {
    // Top-level side effect (`Object.defineProperty(...)`) must stay
    // even when its only "reference" is a swapped binding.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const carrier = {};\nObject.defineProperty(carrier, \"_zod\", { value: {} });\nexport const e6 = () => true;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let emitted = fs::read_to_string(&fixture.megachunk_emitted_path).expect("megachunk emitted");
    assert!(
        emitted.contains("Object.defineProperty"),
        "side-effect statement should be retained:\n{emitted}",
    );
    assert!(
        !emitted.contains(" e6 "),
        "swapped `e6` definition should still be DCE'd:\n{emitted}",
    );
}

#[test]
fn partial_swap_rejects_version_mismatch() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        // Mismatch: spec wants 9.9.9 but the on-disk package.json below
        // pins to 3.23.8.
        upstream_version: "9.9.9",
    });

    assert!(
        !fixture.result.status.success(),
        "debundler should fail on partial-swap version mismatch",
    );
    assert!(
        fixture.result.stderr.contains("version mismatch"),
        "expected version-mismatch error in stderr:\n{}",
        fixture.result.stderr,
    );
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
    megachunk_chunk_path: String,
    caller_emitted_path: PathBuf,
    megachunk_emitted_path: PathBuf,
    manifest_path: PathBuf,
    _root: TempDir,
}

impl PartialSwapFixture {
    /// Partial-swap resolutions are written to a sibling JSON file in
    /// the same directory as the main vendor swap manifest.
    fn partial_manifest_path(&self) -> PathBuf {
        self.manifest_path
            .parent()
            .expect("manifest path has a parent")
            .join("vendor_partial_swap_manifest.json")
    }
}

fn run_partial_swap_fixture(args: PartialSwapFixtureArgs<'_>) -> PartialSwapFixture {
    const PACKAGE_NAME: &str = "zod";
    const SUBPATH: &str = "lib/index.mjs";
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";

    let root = TempDir::with_prefix("vendor-partial-swap-").expect("create tempdir");
    let workspace_root = root.path().join("workspace");
    let extracted_root = workspace_root.join("extracted");
    let snapshot_root = workspace_root.join("snapshot");
    let out_root = workspace_root.join("out");
    let wrapper_root = workspace_root.join("vendors").join("generated");
    let manifest_path = workspace_root.join("vendors").join("manifest.json");
    let package_root = root.path().join("upstream").join(PACKAGE_NAME);
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&wrapper_root).unwrap();
    fs::create_dir_all(&package_root).unwrap();
    fs::create_dir_all(snapshot_root.join("static")).unwrap();
    fs::create_dir_all(package_root.join("lib")).unwrap();

    write_text_file(&snapshot_root.join(MEGACHUNK_PATH), args.chunk_source);
    write_text_file(&snapshot_root.join(CALLER_PATH), args.caller_source);
    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &format!("{MEGACHUNK_PATH}\n{CALLER_PATH}\n"));

    // Pin the on-disk upstream to 3.23.8 regardless of what the spec
    // requests — the version-mismatch test relies on this so the spec
    // can declare a different version and trigger the strict check.
    write_text_file(
        &package_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&json!({
                "name": PACKAGE_NAME,
                "version": "3.23.8",
            }))
            .unwrap(),
        ),
    );
    write_text_file(&package_root.join(SUBPATH), args.upstream_source);

    let mut symbols_json = serde_json::Map::new();
    for (chunk_export, package, upstream_export) in &args.symbols {
        symbols_json.insert(
            (*chunk_export).to_string(),
            json!({ "package": package, "upstream_export": upstream_export }),
        );
    }

    let spec_path = root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            MEGACHUNK_PATH: {
                "level": "partial_swap",
                "identity": "megachunk partial swap fixture",
                "packages": {
                    PACKAGE_NAME: {
                        "namespace": "z",
                        "version": args.upstream_version,
                        "subpath": SUBPATH,
                    },
                },
                "symbols": Value::Object(symbols_json),
            },
        },
        "inputs": { "input_root": &snapshot_root, "js_list_path": &js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &manifest_path,
            "output_wrapper_dir": &wrapper_root,
            "write": true,
        },
        "write_js_tree": { "force": true, "out_dir": &out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);

    let caller_emitted_path = out_root.join("static/app").join("entry.js");
    let megachunk_emitted_path = out_root.join("static/megachunk").join("entry.js");

    PartialSwapFixture {
        result,
        megachunk_chunk_path: MEGACHUNK_PATH.to_string(),
        caller_emitted_path,
        megachunk_emitted_path,
        manifest_path,
        _root: root,
    }
}

#[test]
fn partial_swap_namespace_kind_replaces_whole_import() {
    // Caller has `import { a as React } from "../megachunk/entry.js"`
    // where the chunk export `a` is the package's whole namespace
    // object. References use member access (`React.useState(...)`),
    // which must stay intact post-swap. Only the import statement
    // should change to `import * as React from "react"`.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: "namespace",
        package_name: "react",
        package_version: "18.3.1",
        subpath: "index.js",
        chunk_source: "export const a = { useState: () => 1, useEffect: () => 2 };\nexport const keepMe = () => 7;\n",
        caller_source: "import { a as React, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return React.useState() + kept(); }\n",
        upstream_source: "export const useState = () => 1;\nexport const useEffect = () => 2;\n",
        chunk_export: "a",
        upstream_export: None,
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("import * as React from \"react\""),
        "caller should emit a namespace import for the package:\n{caller}",
    );
    assert!(
        caller.contains("React.useState()"),
        "caller's member-access references must stay intact:\n{caller}",
    );
    assert!(
        caller.contains("keepMe as kept"),
        "non-swapped specifiers stay in the residual import:\n{caller}",
    );
}

#[test]
fn partial_swap_default_kind_replaces_whole_import() {
    // Caller has `import { aQ as z } from "../megachunk/entry.js"`
    // where the chunk export `aQ` is the package's default export
    // (e.g. clsx). References call the local binding directly
    // (`z(...)`), which must stay intact. Only the import statement
    // should change to `import z from "clsx"`.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: "default",
        package_name: "clsx",
        package_version: "2.1.1",
        subpath: "dist/clsx.mjs",
        chunk_source: "export const aQ = (...args) => args.join(' ');\nexport const keepMe = () => 7;\n",
        caller_source: "import { aQ as z, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return z(\"a\", \"b\") + kept(); }\n",
        upstream_source: "export default (...args) => args.join(' ');\n",
        chunk_export: "aQ",
        upstream_export: None,
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("import z from \"clsx\""),
        "caller should emit a default import for the package:\n{caller}",
    );
    assert!(
        caller.contains("z(\"a\", \"b\")"),
        "caller's call-site references must stay intact:\n{caller}",
    );
    assert!(
        caller.contains("keepMe as kept"),
        "non-swapped specifiers stay in the residual import:\n{caller}",
    );
}

#[test]
fn partial_swap_named_kind_auto_renames_local_binding() {
    // Caller has `import { o as mobxObserver } from "../megachunk/entry.js"`
    // where the chunk export `o` is a single named export of the package
    // (e.g. `mobx-react-lite#observer`). The local alias `mobxObserver`
    // is whatever the chunker emitted — the partial-swap rewrites both
    // the import (no alias) AND every reference in the file to use the
    // upstream export name. So `mobxObserver(...)` becomes
    // `observer(...)`.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: "named",
        package_name: "mobx-react-lite",
        package_version: "4.0.7",
        subpath: "dist/index.js",
        chunk_source: "export const o = (Component) => Component;\nexport const keepMe = () => 7;\n",
        caller_source: "import { o as mobxObserver, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return mobxObserver(\"X\") + kept(); }\n",
        upstream_source: "export const observer = (Component) => Component;\n",
        chunk_export: "o",
        upstream_export: Some("observer"),
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("import { observer } from \"mobx-react-lite\""),
        "caller should emit a bare named import using the upstream export:\n{caller}",
    );
    assert!(
        caller.contains("observer(\"X\")"),
        "caller's call-site references should be renamed to the upstream export:\n{caller}",
    );
    assert!(
        !caller.contains("mobxObserver"),
        "caller should not retain the original local alias:\n{caller}",
    );
    assert!(
        caller.contains("keepMe as kept"),
        "non-swapped specifiers stay in the residual import:\n{caller}",
    );
}

#[test]
fn partial_swap_named_kind_no_rewrite_when_local_already_matches() {
    // When the caller-side local binding already matches the upstream
    // export name, the emitted import drops the `as` alias and no
    // identifier rewrite is needed.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: "named",
        package_name: "mobx-react-lite",
        package_version: "4.0.7",
        subpath: "dist/index.js",
        chunk_source: "export const o = (Component) => Component;\n",
        caller_source: "import { o as observer } from \"../megachunk/entry.js\";\nexport function go() { return observer(\"X\"); }\n",
        upstream_source: "export const observer = (Component) => Component;\n",
        chunk_export: "o",
        upstream_export: Some("observer"),
    });

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("import { observer } from \"mobx-react-lite\""),
        "no `as` alias when local matches upstream export:\n{caller}",
    );
    assert!(
        caller.contains("observer(\"X\")"),
        "call sites unchanged:\n{caller}",
    );
}

struct PartialSwapKindFixtureArgs<'a> {
    /// "namespace", "default", or "named"
    kind: &'a str,
    package_name: &'a str,
    package_version: &'a str,
    subpath: &'a str,
    chunk_source: &'a str,
    caller_source: &'a str,
    upstream_source: &'a str,
    chunk_export: &'a str,
    /// Required for `kind: named`. None for `namespace` / `default`.
    upstream_export: Option<&'a str>,
}

fn run_partial_swap_kind_fixture(args: PartialSwapKindFixtureArgs<'_>) -> PartialSwapFixture {
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";

    let root = TempDir::with_prefix("vendor-partial-swap-kind-").expect("create tempdir");
    let workspace_root = root.path().join("workspace");
    let extracted_root = workspace_root.join("extracted");
    let snapshot_root = workspace_root.join("snapshot");
    let out_root = workspace_root.join("out");
    let wrapper_root = workspace_root.join("vendors").join("generated");
    let manifest_path = workspace_root.join("vendors").join("manifest.json");
    let package_root = root.path().join("upstream").join(args.package_name);
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&wrapper_root).unwrap();
    fs::create_dir_all(&package_root).unwrap();
    fs::create_dir_all(snapshot_root.join("static")).unwrap();
    if let Some(parent) = Path::new(args.subpath).parent() {
        fs::create_dir_all(package_root.join(parent)).unwrap();
    }

    write_text_file(&snapshot_root.join(MEGACHUNK_PATH), args.chunk_source);
    write_text_file(&snapshot_root.join(CALLER_PATH), args.caller_source);
    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &format!("{MEGACHUNK_PATH}\n{CALLER_PATH}\n"));

    write_text_file(
        &package_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&json!({
                "name": args.package_name,
                "version": args.package_version,
            }))
            .unwrap(),
        ),
    );
    write_text_file(&package_root.join(args.subpath), args.upstream_source);

    let mut symbol_obj = serde_json::Map::new();
    symbol_obj.insert("package".to_string(), Value::from(args.package_name));
    symbol_obj.insert("kind".to_string(), Value::from(args.kind));
    if let Some(upstream_export) = args.upstream_export {
        symbol_obj.insert("upstream_export".to_string(), Value::from(upstream_export));
    }
    let spec_path = root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            MEGACHUNK_PATH: {
                "level": "partial_swap",
                "identity": format!("megachunk {} swap fixture", args.kind),
                "packages": {
                    args.package_name: {
                        "version": args.package_version,
                        "subpath": args.subpath,
                    },
                },
                "symbols": {
                    args.chunk_export: Value::Object(symbol_obj),
                },
            },
        },
        "inputs": { "input_root": &snapshot_root, "js_list_path": &js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &manifest_path,
            "output_wrapper_dir": &wrapper_root,
            "write": true,
        },
        "write_js_tree": { "force": true, "out_dir": &out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(args.package_name, &package_root)]);

    let caller_emitted_path = out_root.join("static/app").join("entry.js");
    let megachunk_emitted_path = out_root.join("static/megachunk").join("entry.js");

    PartialSwapFixture {
        result,
        megachunk_chunk_path: MEGACHUNK_PATH.to_string(),
        caller_emitted_path,
        megachunk_emitted_path,
        manifest_path,
        _root: root,
    }
}
