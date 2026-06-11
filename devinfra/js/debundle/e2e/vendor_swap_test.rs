//! Vendor-swap end-to-end coverage. Builds a tiny snapshot chunk that
//! re-exports a synthetic upstream package, runs the swap pipeline, and
//! asserts the generated wrapper.

use debundle_e2e_support::{
    CommandResult, assert_node_output, run_debundler, write_text_file, write_yaml_file,
};
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
        .get("full")
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

struct VendorTestWorkspace {
    root: TempDir,
    snapshot_root: PathBuf,
    _extracted_root: PathBuf,
    out_root: PathBuf,
    wrapper_root: PathBuf,
    manifest_path: PathBuf,
    js_list_path: PathBuf,
}

impl VendorTestWorkspace {
    fn new(prefix: &str) -> Self {
        let root = TempDir::with_prefix(prefix).expect("create tempdir");
        let workspace_root = root.path().join("workspace");
        let snapshot_root = workspace_root.join("snapshot");
        let extracted_root = workspace_root.join("extracted");
        let out_root = workspace_root.join("out");
        let wrapper_root = out_root.join("app").join("vendors").join("generated");
        let manifest_path = out_root.join("reports").join("vendor_swaps.json");
        let js_list_path = extracted_root.join("js-files.txt");
        fs::create_dir_all(&extracted_root).unwrap();
        fs::create_dir_all(&snapshot_root).unwrap();
        fs::create_dir_all(&out_root).unwrap();
        Self {
            root,
            snapshot_root,
            _extracted_root: extracted_root,
            out_root,
            wrapper_root,
            manifest_path,
            js_list_path,
        }
    }

    fn write_chunk(&self, chunk_path: &str, source: &str) {
        let full_path = self.snapshot_root.join(chunk_path);
        if let Some(parent) = full_path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        write_text_file(&full_path, source);
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
        fs::create_dir_all(&package_root).unwrap();
        if let Some(parent) = Path::new(subpath).parent() {
            fs::create_dir_all(package_root.join(parent)).unwrap();
        }
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
        fs::create_dir_all(&bundle_root).unwrap();
        let bundle_path = bundle_root.join(filename);
        write_text_file(&bundle_path, source);
        bundle_path
    }
}

/// Set up a workspace for a bundled_partial_swap fixture, writing the
/// chunks (`snapshot_root/<path> = source`), the js-list, and the
/// bundle file. The returned tuple `(ws, bundle_path)` is everything
/// downstream wiring needs to compose the spec.
fn setup_bundled_partial_swap(
    tempdir_prefix: &str,
    bundle_filename: &str,
    bundle_source: &str,
    chunks: &[(&str, &str)],
) -> (VendorTestWorkspace, PathBuf) {
    let ws = VendorTestWorkspace::new(tempdir_prefix);
    for (chunk_path, source) in chunks {
        ws.write_chunk(chunk_path, source);
    }
    let entries: String = chunks
        .iter()
        .map(|(p, _)| format!("{p}\n"))
        .collect::<Vec<_>>()
        .join("");
    ws.write_js_list(&entries);
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
    let mut spec = json!({
        "vendor": vendor,
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    if let Some(Value::Object(extras)) = extra {
        let object = spec.as_object_mut().expect("spec is JSON object");
        for (k, v) in extras {
            object.insert(k, v);
        }
    }
    spec
}

fn run_named_from_module_default_fixture(upstream_source: &str) -> VendorSwapFixture {
    run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-named-from-module-default-",
        chunk_source: "export { x as default };\nconst x = 0;\n",
        wrapper_shape: Some("named_from_module_default"),
        upstream_source,
        default_export_aliases: &[],
    })
}

struct FullSwapFixtureArgs<'a> {
    temp_prefix: &'a str,
    chunk_source: &'a str,
    /// `None` runs the plain (wrapper-less) swap path.
    wrapper_shape: Option<&'a str>,
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

    let ws = VendorTestWorkspace::new(args.temp_prefix);
    ws.write_chunk(CHUNK_PATH, args.chunk_source);
    ws.write_js_list(&format!("{CHUNK_PATH}\n"));
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        args.upstream_source,
    );

    let mut vendor_mark = json!({
        "level": "swap",
        "identity": format!("{PACKAGE_NAME}/{SUBPATH}"),
        "package": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "subpath": SUBPATH,
    });
    if let Some(wrapper_shape) = args.wrapper_shape {
        vendor_mark
            .as_object_mut()
            .expect("vendor mark is a JSON object")
            .insert("wrapper_shape".to_string(), json!(wrapper_shape));
    }
    if !args.default_export_aliases.is_empty() {
        vendor_mark
            .as_object_mut()
            .expect("vendor mark is a JSON object")
            .insert(
                "default_export_aliases".to_string(),
                json!(args.default_export_aliases),
            );
    }
    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            CHUNK_PATH: vendor_mark,
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    VendorSwapFixture {
        result,
        chunk_path: CHUNK_PATH.to_string(),
        wrapper_path: ws.wrapper_path_for_chunk(CHUNK_PATH),
        manifest_path: ws.manifest_path.clone(),
        _root: ws.root,
    }
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
    run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-named-from-default-",
        chunk_source: args.chunk_source,
        wrapper_shape: Some("named_from_default"),
        upstream_source: args.upstream_source,
        default_export_aliases: &[],
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
        .get("partial")
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
// After the consumer side is rewritten (lowering construction + the
// pass-through emission rewriter), the strip pass drops the swapped names from the
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
fn partial_swap_rejects_split_brain_residual_reachability() {
    // `object` (swapped) references `ZodObject`; `ZodObject` is still
    // exported. Keeping it would leave a residual copy of the old
    // package implementation reachable next to the replacement import.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "class ZodObject {}\nconst object = () => new ZodObject();\nexport { object, ZodObject };\n",
        caller_source: "import { object as zodObject, ZodObject as Z } from \"../megachunk/entry.js\";\nexport function go() { return zodObject() instanceof Z; }\n",
        upstream_source: "export const object = () => ({});\n",
        symbols: vec![("object", "zod", "object")],
        upstream_version: "3.23.8",
    });

    assert!(
        !fixture.result.status.success(),
        "debundler should reject split-brain partial swap",
    );
    assert!(
        fixture.result.stderr.contains("split-brain vendor swap"),
        "expected split-brain diagnostic in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn partial_swap_keeps_side_effect_init() {
    // Top-level hard side effects must stay
    // even when its only "reference" is a swapped binding.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "console.log(\"keep side effect\");\nexport const e6 = () => true;\n",
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
        emitted.contains("console.log"),
        "side-effect statement should be retained:\n{emitted}",
    );
    assert!(
        !emitted.contains(" e6 "),
        "swapped `e6` definition should still be DCE'd:\n{emitted}",
    );
}

#[test]
fn partial_swap_rejects_observable_side_effect_reading_swapped_binding() {
    // A `Hard`, globally-observable side effect (`globalThis.__zodBoolean =
    // e6`) that *reads* the swapped binding `e6`. The read makes the
    // statement swap-reachable, so the keep-pass declines to honor it for its
    // side effect and it would otherwise be silently dropped — even though
    // residual / external code can witness the global write via
    // `globalThis.__zodBoolean`. The strip pass must bail rather than ship a
    // bundle missing the effect.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nglobalThis.__zodBoolean = e6;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert!(
        !fixture.result.status.success(),
        "debundler should reject an observable side effect that reads a swapped binding\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture
            .result
            .stderr
            .contains("observable side-effect item")
            && fixture.result.stderr.contains("swap-reachable"),
        "expected observable-side-effect soundness diagnostic in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn partial_swap_drops_original_package_island_with_local_mutations() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "class ZodBoolean {}\nZodBoolean.displayName = \"ZodBoolean\";\nconst e6 = () => new ZodBoolean();\nexport { e6 };\n",
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
        !emitted.contains("ZodBoolean"),
        "old package class should be removed from the residual chunk:\n{emitted}",
    );
    assert!(
        !emitted.contains("displayName"),
        "local metadata write should be removed with the old package class:\n{emitted}",
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
    fn partial_manifest_path(&self) -> PathBuf {
        self.manifest_path.clone()
    }
}

fn run_partial_swap_raw(
    ws: VendorTestWorkspace,
    vendor_spec: serde_json::Map<String, Value>,
    packages: &[(&str, &Path)],
) -> PartialSwapFixture {
    const MEGACHUNK_PATH: &str = "static/megachunk.js";

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            MEGACHUNK_PATH: Value::Object(vendor_spec),
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, packages);

    let caller_emitted_path = ws.out_root.join("app/static/app").join("entry.js");
    let megachunk_emitted_path = ws.out_root.join("app/static/megachunk").join("entry.js");

    PartialSwapFixture {
        result,
        megachunk_chunk_path: MEGACHUNK_PATH.to_string(),
        caller_emitted_path,
        megachunk_emitted_path,
        manifest_path: ws.manifest_path.clone(),
        _root: ws.root,
    }
}

fn run_partial_swap_fixture(args: PartialSwapFixtureArgs<'_>) -> PartialSwapFixture {
    const PACKAGE_NAME: &str = "zod";
    const SUBPATH: &str = "lib/index.mjs";
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";

    let ws = VendorTestWorkspace::new("vendor-partial-swap-");
    ws.write_chunk(MEGACHUNK_PATH, args.chunk_source);
    ws.write_chunk(CALLER_PATH, args.caller_source);
    ws.write_js_list(&format!("{MEGACHUNK_PATH}\n{CALLER_PATH}\n"));
    // Pin the on-disk upstream to 3.23.8 regardless of what the spec
    // requests — the version-mismatch test relies on this so the spec
    // can declare a different version and trigger the strict check.
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        "3.23.8",
        SUBPATH,
        args.upstream_source,
    );

    let mut symbols_json = serde_json::Map::new();
    for (chunk_export, package, upstream_export) in &args.symbols {
        symbols_json.insert(
            (*chunk_export).to_string(),
            json!({ "package": package, "upstream_export": upstream_export }),
        );
    }

    let mut vendor = serde_json::Map::new();
    vendor.insert("level".into(), json!("partial_swap"));
    vendor.insert("identity".into(), json!("megachunk partial swap fixture"));
    vendor.insert(
        "packages".into(),
        json!({
            PACKAGE_NAME: {
                "namespace": "z",
                "version": args.upstream_version,
                "subpath": SUBPATH,
            },
        }),
    );
    vendor.insert("symbols".into(), Value::Object(symbols_json));

    run_partial_swap_raw(ws, vendor, &[(PACKAGE_NAME, &package_root)])
}

#[test]
fn partial_swap_skips_materialized_module_import_colliding_with_vendor_source_path() {
    // Regression for stale `ArtifactIndexes` reuse in the pipeline:
    // `materialize_logical_modules` creates `pkg/util.js` inside chunk
    // `static/app` and rewires the entry to `import { q } from
    // "./pkg/util.js"`. Resolving that import against indexes built
    // *before* materialization misses the new file in the
    // output-path index and falls back to source-path resolution:
    // `static/app.js` + `./pkg/util.js` → `static/pkg/util.js`,
    // which collides with the partially-swapped vendor chunk. The
    // partial-swap rewrite then misroutes the entry's import of its
    // own materialized module to the vendor package. With indexes
    // rebuilt post-materialize, the import resolves to the caller
    // chunk itself and is skipped.
    const VENDOR_PATH: &str = "static/pkg/util.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "obs";
    const PACKAGE_VERSION: &str = "1.0.0";
    const SUBPATH: &str = "index.js";

    let ws = VendorTestWorkspace::new("vendor-partial-swap-materialized-collision-");
    ws.write_chunk(
        VENDOR_PATH,
        "export const q = () => \"vendor\";\nexport const keep = () => 7;\n",
    );
    ws.write_chunk(
        APP_PATH,
        "const q = () => \"app\";\nexport function out() { return q(); }\n",
    );
    ws.write_js_list(&format!("{VENDOR_PATH}\n{APP_PATH}\n"));
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        "export const q = () => \"package\";\n",
    );

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            VENDOR_PATH: {
                "level": "partial_swap",
                "identity": "materialized sibling collision fixture",
                "packages": {
                    PACKAGE_NAME: {
                        "namespace": "z",
                        "version": PACKAGE_VERSION,
                        "subpath": SUBPATH,
                    },
                },
                "symbols": {
                    "q": { "package": PACKAGE_NAME, "upstream_export": "q" },
                },
            },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "logical_modules": {
            "static/app": {
                "pkg/util": {
                    "members": [
                        { "name": "q", "selector": { "binding": { "name": "q" } } },
                    ],
                },
            },
        },
        "unassigned_mode": {
            "static/app": { "kind": "inline_in_entry" },
        },
        "materialize_logical_modules": {
            "prune_other_chunks": false,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let entry_path = ws.out_root.join("app/static/app/entry.js");
    let entry = fs::read_to_string(&entry_path).expect("app entry emitted");
    assert!(
        entry.contains("pkg/util.js"),
        "entry must keep importing its own materialized module:\n{entry}",
    );
    assert!(
        !entry.contains(&format!("from \"{PACKAGE_NAME}\"")),
        "entry's materialized-module import must not be misrouted to the vendor package:\n{entry}",
    );

    let probe_path = ws.out_root.join("__run_materialized_collision.mjs");
    write_text_file(
        &probe_path,
        "const { out } = await import(\"./app/static/app/entry.js\");\nconsole.log(out());\n",
    );
    assert_node_output(&probe_path, "app\n", "");
}

#[test]
fn partial_swap_references_rewritten_parity_across_materialized_and_passthrough_consumers() {
    // `references_rewritten` parity pin (plans/vendor_into_emission.md §5,
    // open question 6): the manifest must count *emitted* consumer
    // references identically whether a reference lives in a pass-through
    // file (the caller chunk's residual entry, rewritten by the
    // post-materialize wave) or in a materialized module body (whose
    // vendor-targeted runtime re-import is planned during lowering's
    // import construction). One swapped member-kind symbol consumed once
    // from each file class pins the total at 2 — the same count the
    // all-wave path produced before the file-class split.
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "zod";
    const PACKAGE_VERSION: &str = "3.23.8";
    const SUBPATH: &str = "lib/index.mjs";

    let ws = VendorTestWorkspace::new("vendor-partial-swap-count-parity-");
    ws.write_chunk(
        MEGACHUNK_PATH,
        "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
    );
    ws.write_chunk(
        APP_PATH,
        "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\n\
         export const movedFlag = zodBoolean() && kept() === 7;\n\
         export function stay() { return zodBoolean(); }\n",
    );
    ws.write_js_list(&format!("{MEGACHUNK_PATH}\n{APP_PATH}\n"));
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        "export const boolean = () => true;\n",
    );

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            MEGACHUNK_PATH: {
                "level": "partial_swap",
                "identity": "references_rewritten parity fixture",
                "packages": {
                    PACKAGE_NAME: {
                        "namespace": "z",
                        "version": PACKAGE_VERSION,
                        "subpath": SUBPATH,
                    },
                },
                "symbols": {
                    "e6": { "package": PACKAGE_NAME, "upstream_export": "boolean" },
                },
            },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "logical_modules": {
            "static/app": {
                "flags": {
                    "members": [
                        { "name": "movedFlag", "selector": { "binding": { "name": "movedFlag" } } },
                    ],
                },
            },
        },
        "unassigned_mode": {
            "static/app": { "kind": "inline_in_entry" },
        },
        "materialize_logical_modules": {
            "prune_other_chunks": false,
            "target_dir": "modules",
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let module_path = ws.out_root.join("app/static/app/modules/flags.js");
    let module = fs::read_to_string(&module_path).expect("materialized module emitted");
    assert!(
        module.contains("import * as z from \"zod\"") && module.contains("z.boolean()"),
        "materialized module should consume the package, not the chunk:\n{module}",
    );
    assert!(
        !module.contains("zodBoolean"),
        "materialized module should not retain the swapped import alias:\n{module}",
    );
    assert!(
        module.contains("keepMe as kept"),
        "materialized module keeps the non-swapped chunk re-import:\n{module}",
    );
    let entry_path = ws.out_root.join("app/static/app/entry.js");
    let entry = fs::read_to_string(&entry_path).expect("app entry emitted");
    assert!(
        entry.contains("import * as z from \"zod\"") && entry.contains("z.boolean()"),
        "pass-through entry should consume the package, not the chunk:\n{entry}",
    );

    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(&ws.manifest_path).expect("partial manifest"))
            .expect("partial manifest parses");
    assert_eq!(
        manifest
            .get("partial")
            .and_then(|r| r.get(MEGACHUNK_PATH))
            .and_then(|r| r.get("symbols"))
            .and_then(|r| r.get("e6"))
            .and_then(|r| r.get("references_rewritten"))
            .and_then(Value::as_u64),
        Some(2),
        "one rewrite per file class (materialized module + residual entry):\n{manifest:#}",
    );

    let app_root = ws.out_root.join("app");
    install_node_module(
        &app_root,
        PACKAGE_NAME,
        PACKAGE_VERSION,
        "export const boolean = () => true;\n",
    );
    let probe_path = ws.out_root.join("__run_count_parity.mjs");
    write_text_file(
        &probe_path,
        "const { stay, movedFlag } = await import(\"./app/static/app/entry.js\");\n\
         console.log(`${stay()}:${movedFlag}`);\n",
    );
    assert_node_output(&probe_path, "true:true\n", "");
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

#[test]
fn bundled_partial_swap_replaces_react_cjs_family_with_singleton_esm_facade() {
    // React-family swaps need more than plain partial-swap. The browser
    // cannot load React's npm CJS entry directly as an importmapped ESM file,
    // and React + jsx-runtime must share one module singleton. This fixture is
    // the smallest contract for that shape: a namespace import for `react`, a
    // namespace import for `react/jsx-runtime`, and a shared internal cell
    // that proves both aliases came from one bundled package family rather
    // than from a residual in-blob copy plus raw CJS.
    // TODO: once the schema exists, keep this as an executable fixture. The
    // Node probe below is the minimal gate; a browser importmap/load test would
    // be the stronger proof for the live-proxy path.
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "react";
    const JSX_RUNTIME_NAME: &str = "react/jsx-runtime";
    const PACKAGE_VERSION: &str = "18.3.1";

    let (ws, bundle_path) = setup_bundled_partial_swap(
        "vendor-bundled-partial-swap-react-",
        "react-family.esbuilt.js",
        "const dispatcher = { current: \"package\" };\n\
         const React = { dispatcher, useState: () => dispatcher.current };\n\
         const jsxRuntime = { jsx: () => dispatcher.current };\n\
         export { React, jsxRuntime };\n",
        &[
            (
                MEGACHUNK_PATH,
                "const dispatcher = { current: \"vendor\" };\n\
                 const React = { dispatcher, useState: () => dispatcher.current };\n\
                 const jsxRuntime = { jsx: () => dispatcher.current };\n\
                 export { React as a, jsxRuntime as j };\n",
            ),
            (
                CALLER_PATH,
                "import { a as React, j as jsxRuntime } from \"../megachunk/entry.js\";\n\
                 console.log(`${React.useState()}:${jsxRuntime.jsx()}`);\n",
            ),
        ],
    );
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        "index.js",
        "const dispatcher = { current: \"package\" };\n\
         exports.dispatcher = dispatcher;\n\
         exports.useState = () => dispatcher.current;\n",
    );
    write_text_file(
        &package_root.join("jsx-runtime.js"),
        "const React = require(\"./index.js\");\n\
         exports.jsx = () => React.dispatcher.current;\n",
    );

    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({
            MEGACHUNK_PATH: {
                "level": "bundled_partial_swap",
                "identity": "React CJS family bundled partial swap fixture",
                "bundle": { "path": &bundle_path },
                "packages": {
                    PACKAGE_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "index.js",
                        "bundle_export": "React",
                    },
                    JSX_RUNTIME_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "jsx-runtime.js",
                        "bundle_export": "jsxRuntime",
                    },
                },
                "symbols": {
                    "a": { "package": PACKAGE_NAME, "kind": "namespace" },
                    "j": { "package": JSX_RUNTIME_NAME, "kind": "namespace" },
                },
            },
        }),
        None,
    );
    let spec_path = ws.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(
        &spec_path,
        &[
            (PACKAGE_NAME, &package_root),
            (JSX_RUNTIME_NAME, &package_root),
        ],
    );
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let caller_path = ws.out_root.join("app/static/app").join("entry.js");
    let caller = fs::read_to_string(&caller_path).expect("caller emitted");
    assert!(
        !caller.contains("from \"react\"") && !caller.contains("from \"react/jsx-runtime\""),
        "bundled partial swap must not leave browser-facing raw CJS package imports:\n{caller}",
    );
    assert!(
        caller.contains("vendors/generated/static/megachunk/react.js")
            && caller.contains("vendors/generated/static/megachunk/react_jsx-runtime.js"),
        "caller should import generated ESM facades:\n{caller}",
    );
    assert!(ws.wrapper_root.join("static/megachunk/bundle.js").exists());
    assert!(ws.wrapper_root.join("static/megachunk/react.js").exists());
    assert!(
        ws.wrapper_root
            .join("static/megachunk/react_jsx-runtime.js")
            .exists()
    );

    let probe_path = ws.out_root.join("__run_entry.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "package:package\n", "");
}

#[test]
fn bundled_partial_swap_runtime_cannot_mix_swapped_client_with_residual_singleton_user() {
    // Red test for React-like singleton package families. Swapping a renderer
    // facade while leaving a component in the residual vendor chunk can mix two
    // dispatcher singletons: the swapped renderer initializes the package copy,
    // but the residual component still reads the in-blob copy. The emitted app
    // must eventually run as `package`; today it throws `residual dispatcher is
    // null`, matching the browser-load failure seen with React hooks.
    // TODO: add a browser/importmap load probe alongside this Node check once
    // the e2e harness has a browser runner.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "singleton-kit";
    const CLIENT_NAME: &str = "singleton-kit/client";
    const PACKAGE_VERSION: &str = "1.0.0";

    let (ws, bundle_path) = setup_bundled_partial_swap(
        "vendor-bundled-partial-swap-singleton-",
        "singleton-kit.esbuilt.js",
        "const dispatcher = { current: null };\n\
         const Hooks = {\n\
           useCell() {\n\
             if (dispatcher.current === null) throw new TypeError(\"package dispatcher is null\");\n\
             return dispatcher.current.cell;\n\
           },\n\
         };\n\
         const Client = {\n\
           render(component) {\n\
             dispatcher.current = { cell: \"package\" };\n\
             try { return component(); } finally { dispatcher.current = null; }\n\
           },\n\
         };\n\
         export { Hooks, Client };\n",
        &[
            (
                VENDOR_PATH,
                "const dispatcher = { current: null };\n\
                 const Hooks = {\n\
                   useCell() {\n\
                     if (dispatcher.current === null) throw new TypeError(\"residual dispatcher is null\");\n\
                     return dispatcher.current.cell;\n\
                   },\n\
                 };\n\
                 function Component() { return Hooks.useCell(); }\n\
                 const Client = {\n\
                   render(component) {\n\
                     dispatcher.current = { cell: \"vendor\" };\n\
                     try { return component(); } finally { dispatcher.current = null; }\n\
                   },\n\
                 };\n\
                 export { Hooks as a, Client as h, Component as c };\n",
            ),
            (
                APP_PATH,
                "import { h as Client, c as Component } from \"../vendor/entry.js\";\n\
                 console.log(Client.render(Component));\n",
            ),
        ],
    );
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        "index.js",
        "exports.useCell = () => \"package\";\n",
    );
    write_text_file(
        &package_root.join("client.js"),
        "exports.render = component => component();\n",
    );

    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({
            VENDOR_PATH: {
                "level": "bundled_partial_swap",
                "identity": "singleton runtime mixed-copy fixture",
                "bundle": { "path": &bundle_path },
                "packages": {
                    PACKAGE_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "index.js",
                        "bundle_export": "Hooks",
                    },
                    CLIENT_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "client.js",
                        "bundle_export": "Client",
                    },
                },
                "symbols": {
                    "a": { "package": PACKAGE_NAME, "kind": "namespace" },
                    "h": { "package": CLIENT_NAME, "kind": "namespace" },
                },
            },
        }),
        None,
    );
    let spec_path = ws.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(
        &spec_path,
        &[(PACKAGE_NAME, &package_root), (CLIENT_NAME, &package_root)],
    );
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let probe_path = ws.out_root.join("__run_entry.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "package\n", "");
}

#[test]
fn bundled_partial_swap_rewrites_imports_created_by_logical_module_materialization() {
    // `materialize_logical_modules` can create new import statements after
    // the original artifact graph has been indexed. Vendor swapping still
    // needs to recognize those generated relative paths and route them to the
    // bundled facade instead of leaving the residual vendor chunk imported.
    // The import source intentionally resolves to `vendor/entry.js` after
    // normalization while the chunk table records the target as
    // `static/vendor`, matching selected-module helpers emitted under a shared
    // browser asset prefix.
    // TODO: add a browser/importmap load probe alongside this Node check once
    // the e2e harness has a browser runner.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "observer-kit/observer";
    const PACKAGE_VERSION: &str = "1.0.0";

    let (ws, bundle_path) = setup_bundled_partial_swap(
        "vendor-bundled-partial-swap-materialized-",
        "observer-kit.esbuilt.js",
        "const observe = value => `package:${value}`;\n\
         export { observe };\n",
        &[
            (
                VENDOR_PATH,
                "const observer = value => `vendor:${value}`;\n\
                 export { observer as o };\n",
            ),
            (
                APP_PATH,
                "import { o as observe } from \"../../vendor/entry.js\";\n\
                 const observed = observe(\"ok\");\n\
                 export { observed };\n",
            ),
        ],
    );
    let report_root = ws.out_root.join("reports").join("tree");
    let package_root = ws.write_upstream_package(
        "upstream/observer-kit",
        "observer-kit",
        PACKAGE_VERSION,
        "observer.js",
        "export default function observe(value) { return `package:${value}`; }\n",
    );

    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({
            VENDOR_PATH: {
                "level": "bundled_partial_swap",
                "identity": "materialized import bundled partial swap fixture",
                "bundle": { "path": &bundle_path },
                "packages": {
                    PACKAGE_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "observer.js",
                        "bundle_export": "observe",
                    },
                },
                "symbols": {
                    "o": { "package": PACKAGE_NAME, "kind": "default" },
                },
            },
        }),
        Some(json!({
            "logical_modules": {
                "static/app": {
                    "helpers/observer": {
                        "members": [
                            {
                                "name": "observed",
                                "selector": { "binding": { "name": "observed" } },
                            },
                        ],
                    },
                },
            },
            "unassigned_mode": {
                "static/app": { "kind": "inline_in_entry" },
            },
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": &report_root,
                "target_dir": "modules",
            },
        })),
    );
    let spec_path = ws.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let materialized_path = ws
        .out_root
        .join("app/static/app/modules/helpers/observer.js");
    let materialized = fs::read_to_string(&materialized_path).expect("materialized module emitted");
    assert!(
        !materialized.contains("vendor/entry.js"),
        "materialized module must not keep importing the residual vendor chunk:\n{materialized}",
    );
    assert!(
        materialized.contains("vendors/generated/static/vendor/observer-kit_observer.js"),
        "materialized module should import the generated bundled facade:\n{materialized}",
    );

    let probe_path = ws.out_root.join("__run_materialized.mjs");
    write_text_file(
        &probe_path,
        "const { observed } = await import(\"./app/static/app/modules/helpers/observer.js\");\n\
         console.log(observed);\n",
    );
    assert_node_output(&probe_path, "package:ok\n", "");
}

#[test]
fn bundled_partial_swap_rewrites_materialized_named_alias_references() {
    // Logical-module materialization synthesizes a fresh import for moved
    // source-chunk bindings. For bundled partial swaps, that generated import
    // is then rewritten to the package facade and all references using the
    // import binding's same hygiene context must follow it. A no-context import
    // alias would leave bare `toolkitMap` / `toolkitMake` references behind.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "toolkit-core";
    const PACKAGE_VERSION: &str = "1.0.0";

    let (ws, bundle_path) = setup_bundled_partial_swap(
        "vendor-bundled-partial-swap-materialized-named-",
        "toolkit-core.esbuilt.js",
        "const Toolkit = {\n\
           map(value) { return `package:${value}`; },\n\
           make(target) { target.mark = \"package\"; return target; },\n\
         };\n\
         export { Toolkit };\n",
        &[
            (
                VENDOR_PATH,
                "const map = value => `vendor:${value}`;\n\
                 const make = target => { target.mark = \"vendor\"; return target; };\n\
                 export { map as m, make as k };\n",
            ),
            (
                APP_PATH,
                "import { m as toolkitMap, k as toolkitMake } from \"../../vendor/entry.js\";\n\
                 const direct = toolkitMap(\"top\");\n\
                 const shadowed = ((toolkitMap) => toolkitMap(\"shadow\"))(value => `local:${value}`);\n\
                 const made = toolkitMake({ label: \"top\" });\n\
                 export { direct, shadowed, made };\n",
            ),
        ],
    );
    let report_root = ws.out_root.join("reports").join("tree");
    let package_root = ws.write_upstream_package(
        "upstream/toolkit-core",
        PACKAGE_NAME,
        PACKAGE_VERSION,
        "index.js",
        "export const map = value => `package:${value}`;\n\
         export const make = target => { target.mark = \"package\"; return target; };\n",
    );

    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({
            VENDOR_PATH: {
                "level": "bundled_partial_swap",
                "identity": "materialized named alias bundled partial swap fixture",
                "bundle": { "path": &bundle_path },
                "packages": {
                    PACKAGE_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "index.js",
                        "bundle_export": "Toolkit",
                        "namespace": "Toolkit",
                    },
                },
                "symbols": {
                    "m": {
                        "package": PACKAGE_NAME,
                        "kind": "named",
                        "upstream_export": "map",
                    },
                    "k": {
                        "package": PACKAGE_NAME,
                        "kind": "named",
                        "upstream_export": "make",
                    },
                },
            },
        }),
        Some(json!({
            "logical_modules": {
                "static/app": {
                    "helpers/toolkit": {
                        "members": [
                            {
                                "name": "direct",
                                "selector": { "binding": { "name": "direct" } },
                            },
                            {
                                "name": "shadowed",
                                "selector": { "binding": { "name": "shadowed" } },
                            },
                            {
                                "name": "made",
                                "selector": { "binding": { "name": "made" } },
                            },
                        ],
                    },
                },
            },
            "unassigned_mode": {
                "static/app": { "kind": "inline_in_entry" },
            },
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": &report_root,
                "target_dir": "modules",
            },
        })),
    );
    let spec_path = ws.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let materialized_path = ws
        .out_root
        .join("app/static/app/modules/helpers/toolkit.js");
    let materialized = fs::read_to_string(&materialized_path).expect("materialized module emitted");
    assert!(
        materialized.contains("vendors/generated/static/vendor/toolkit-core.js"),
        "materialized module should import the generated bundled facade:\n{materialized}",
    );
    assert!(
        materialized.contains("Toolkit.map(") && materialized.contains("Toolkit.make("),
        "materialized module should rewrite imported aliases to package-facade members:\n{materialized}",
    );
    assert!(
        !materialized.contains("toolkitMap(\"top\")") && !materialized.contains("toolkitMake"),
        "materialized module should not retain top-level generated import aliases:\n{materialized}",
    );
    assert!(
        materialized.contains("toolkitMap(\"shadow\")"),
        "shadowed local parameter should not be rewritten by the package-facade alias pass:\n{materialized}",
    );

    let probe_path = ws.out_root.join("__run_materialized_named.mjs");
    write_text_file(
        &probe_path,
        "const { direct, shadowed, made } = await import(\"./app/static/app/modules/helpers/toolkit.js\");\n\
         console.log(`${direct}|${shadowed}|${made.mark}`);\n",
    );
    assert_node_output(&probe_path, "package:top|local:shadow|package\n", "");
}

#[test]
fn bundled_partial_swap_rewrites_non_exported_local_helper_in_vendor_chunk() {
    // Zod's public surface can be swapped while a residual app schema in the
    // same vendor chunk still calls an internal zod helper that Vite did not
    // export. The `local` override is the spec-level escape hatch for that
    // case: rewrite every residual call to the generated facade and then let
    // the strip pass remove the original helper.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "zod";
    const PACKAGE_VERSION: &str = "4.1.12";

    let (ws, bundle_path) = setup_bundled_partial_swap(
        "vendor-bundled-partial-swap-local-helper-",
        "zod.esbuilt.js",
        "const Zod = { instanceof: Ctor => `package:${Ctor.name}` };\n\
         export { Zod };\n",
        &[
            (
                VENDOR_PATH,
                "function nY(Ctor) { return `vendor:${Ctor.name}`; }\n\
                 const schema = nY(URL);\n\
                 export { schema as keep };\n",
            ),
            (
                APP_PATH,
                "import { keep } from \"../vendor/entry.js\";\n\
                 console.log(keep);\n",
            ),
        ],
    );
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        "index.js",
        "const inst = Ctor => `package:${Ctor.name}`;\nexport { inst as instanceof };\n",
    );

    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({
            VENDOR_PATH: {
                "level": "bundled_partial_swap",
                "identity": "local helper bundled partial swap fixture",
                "bundle": { "path": &bundle_path },
                "packages": {
                    PACKAGE_NAME: {
                        "version": PACKAGE_VERSION,
                        "subpath": "index.js",
                        "bundle_export": "Zod",
                        "namespace": "Zod",
                    },
                },
                "symbols": {
                    "zodInstanceof": {
                        "package": PACKAGE_NAME,
                        "kind": "named",
                        "upstream_export": "instanceof",
                        "local": "nY",
                    },
                },
            },
        }),
        None,
    );
    let spec_path = ws.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &spec);

    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let vendor = fs::read_to_string(ws.out_root.join("app/static/vendor/entry.js"))
        .expect("vendor chunk emitted");
    assert!(
        !vendor.contains("function nY"),
        "original local helper should be removed from residual vendor chunk:\n{vendor}",
    );
    assert!(
        vendor.contains(".instanceof(URL)"),
        "residual schema should call the bundled facade:\n{vendor}",
    );

    let probe_path = ws.out_root.join("__run_local_helper.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "package:URL\n", "");
}

#[test]
fn partial_swap_does_not_rewrite_shadowing_inner_binding() {
    // Regression: the partial-swap identifier rewriter must be
    // hygiene-aware. The caller imports `e6 as zodFlag` (swapped to the
    // namespace member `z.boolean`), but a nested function declares a
    // *parameter* named `zodFlag` that shadows the import local and
    // reads through it. Keying the rewrite on the bare textual symbol
    // would miscompile the inner read into `z.boolean()`, returning the
    // upstream value instead of the argument the caller passed. Keying
    // on the resolver-assigned binding `Id` leaves the shadowed inner
    // binding untouched.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => \"UPSTREAM\";\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodFlag, keepMe as kept } from \"../megachunk/entry.js\";\n\
                        function pick(zodFlag) { return zodFlag(); }\n\
                        export const fromImport = zodFlag();\n\
                        export const fromParam = pick(() => \"PARAM\");\n\
                        export const keepUse = kept();\n",
        upstream_source: "export const boolean = () => \"UPSTREAM\";\n",
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

    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    // The top-level import-local use is rewritten to the facade member.
    assert!(
        caller.contains("z.boolean()"),
        "top-level import-local use should be rewritten to the facade member:\n{caller}",
    );
    // The shadowing parameter and its read must survive verbatim — the
    // inner `zodFlag()` is a call on the parameter, not the import local.
    assert!(
        caller.contains("function pick(zodFlag)") && caller.contains("return zodFlag()"),
        "shadowing parameter binding must not be rewritten to the facade member:\n{caller}",
    );

    // Runtime proof: `fromParam` must reflect the argument passed to
    // `pick`, not the swapped upstream value.
    let app_root = fixture
        .caller_emitted_path
        .ancestors()
        .nth(3)
        .expect("app root above static/app/entry.js")
        .to_path_buf();
    // The emitted caller imports the bare `zod` specifier; provide a
    // local `node_modules/zod` so node can resolve it at runtime.
    write_text_file(
        &app_root.join("node_modules/zod/package.json"),
        "{ \"name\": \"zod\", \"version\": \"3.23.8\", \"type\": \"module\", \"main\": \"index.js\" }\n",
    );
    write_text_file(
        &app_root.join("node_modules/zod/index.js"),
        "export const boolean = () => \"UPSTREAM\";\n",
    );
    let probe_path = app_root.join("__run_shadow_probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./static/app/entry.js\");\n\
         console.log(`${m.fromImport}:${m.fromParam}`);\n",
    );
    assert_node_output(&probe_path, "UPSTREAM:PARAM\n", "");
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

    let ws = VendorTestWorkspace::new("vendor-partial-swap-kind-");
    ws.write_chunk(MEGACHUNK_PATH, args.chunk_source);
    ws.write_chunk(CALLER_PATH, args.caller_source);
    ws.write_js_list(&format!("{MEGACHUNK_PATH}\n{CALLER_PATH}\n"));
    let package_root = ws.write_upstream_package(
        &format!("upstream/{}", args.package_name),
        args.package_name,
        args.package_version,
        args.subpath,
        args.upstream_source,
    );

    let mut symbol_obj = serde_json::Map::new();
    symbol_obj.insert("package".to_string(), Value::from(args.package_name));
    symbol_obj.insert("kind".to_string(), Value::from(args.kind));
    if let Some(upstream_export) = args.upstream_export {
        symbol_obj.insert("upstream_export".to_string(), Value::from(upstream_export));
    }

    let mut symbols = serde_json::Map::new();
    symbols.insert(args.chunk_export.to_string(), Value::Object(symbol_obj));

    let mut vendor = serde_json::Map::new();
    vendor.insert("level".into(), json!("partial_swap"));
    vendor.insert(
        "identity".into(),
        json!(format!("megachunk {} swap fixture", args.kind)),
    );
    vendor.insert(
        "packages".into(),
        json!({
            args.package_name: {
                "version": args.package_version,
                "subpath": args.subpath,
            },
        }),
    );
    vendor.insert("symbols".into(), Value::Object(symbols));

    run_partial_swap_raw(ws, vendor, &[(args.package_name, &package_root)])
}

// ─── partial-swap consumer soundness ────────────────────────────────────
//
// The per-symbol rewrite handles `ImportDecl` consumers. Two other consumer
// shapes can reference a partially-swapped chunk's export surface:
// `export { x } from "<chunk>"` re-exports and `import * as M from
// "<chunk>"` namespace imports. Re-exports of `named`/`default`/`namespace`
// kind symbols are rewritten against the upstream package; everything else
// (member-kind re-exports, namespace imports, `export *`) must make the
// pipeline bail — the strip pass removes those names from the chunk's
// export surface, so an unrewritten consumer either link-fails (re-export)
// or silently reads `undefined` (namespace member).

fn setup_partial_swap_consumer_fixture(
    prefix: &str,
    chunk_source: &str,
    caller_source: &str,
    package_name: &str,
    version: &str,
    subpath: &str,
    upstream_source: &str,
) -> (VendorTestWorkspace, PathBuf) {
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new(prefix);
    ws.write_chunk(MEGACHUNK_PATH, chunk_source);
    ws.write_chunk(CALLER_PATH, caller_source);
    ws.write_js_list(&format!("{MEGACHUNK_PATH}\n{CALLER_PATH}\n"));
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
    packages: Value,
    symbols: Value,
) -> serde_json::Map<String, Value> {
    let mut vendor = serde_json::Map::new();
    vendor.insert("level".into(), json!("partial_swap"));
    vendor.insert("identity".into(), json!(identity));
    vendor.insert("packages".into(), packages);
    vendor.insert("symbols".into(), symbols);
    vendor
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

#[test]
fn partial_swap_rewrites_named_kind_reexport_from_consumer() {
    // `export { e6 as zodBoolean } from "<chunk>"` of a kind=named swapped
    // symbol must be rewritten to re-export the upstream package, exactly
    // like ImportDecl consumers — otherwise the strip pass removes `e6`
    // from the chunk's export surface while the re-export still references
    // it, guaranteeing a module-link failure in the emitted tree.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-reexport-named-",
        "export const e6 = () => \"UPSTREAM\";\nexport const keepMe = () => 7;\n",
        "export { e6 as zodBoolean, keepMe } from \"../megachunk/entry.js\";\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const boolean = () => \"UPSTREAM\";\n",
    );
    let vendor = partial_swap_vendor(
        "named re-export consumer fixture",
        json!({ "zod": { "version": "3.23.8", "subpath": "lib/index.mjs" } }),
        json!({ "e6": { "package": "zod", "kind": "named", "upstream_export": "boolean" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("zod", &package_root)]);

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("export { boolean as zodBoolean } from \"zod\""),
        "re-export of swapped name should target the upstream package:\n{caller}",
    );
    assert!(
        caller.contains("keepMe") && caller.contains("../megachunk/entry.js"),
        "non-swapped re-export stays on the residual chunk:\n{caller}",
    );
    assert!(
        !caller.contains("e6"),
        "no re-export of the stripped chunk name may survive:\n{caller}",
    );

    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => \"UPSTREAM\";\n",
    );
    let probe_path = app_root.join("__run_reexport_named.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./static/app/entry.js\");\n\
         console.log(`${m.zodBoolean()}:${m.keepMe()}`);\n",
    );
    assert_node_output(&probe_path, "UPSTREAM:7\n", "");
}

#[test]
fn partial_swap_rewrites_default_kind_reexport_from_consumer() {
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-reexport-default-",
        "export const aQ = (...args) => args.join(\"+\");\nexport const keepMe = () => 7;\n",
        "export { aQ as z, keepMe } from \"../megachunk/entry.js\";\n",
        "clsx",
        "2.1.1",
        "dist/clsx.mjs",
        "export default (...args) => args.join(\"+\");\n",
    );
    let vendor = partial_swap_vendor(
        "default re-export consumer fixture",
        json!({ "clsx": { "version": "2.1.1", "subpath": "dist/clsx.mjs" } }),
        json!({ "aQ": { "package": "clsx", "kind": "default" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("clsx", &package_root)]);

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("export { default as z } from \"clsx\""),
        "re-export of a kind=default symbol should forward the package default:\n{caller}",
    );

    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "clsx",
        "2.1.1",
        "export default (...args) => args.join(\"+\");\n",
    );
    let probe_path = app_root.join("__run_reexport_default.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./static/app/entry.js\");\n\
         console.log(m.z(\"a\", \"b\"));\n",
    );
    assert_node_output(&probe_path, "a+b\n", "");
}

#[test]
fn partial_swap_rewrites_namespace_kind_reexport_from_consumer() {
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-reexport-namespace-",
        "export const a = { useState: () => 1 };\nexport const keepMe = () => 7;\n",
        "export { a as React, keepMe } from \"../megachunk/entry.js\";\n",
        "react",
        "18.3.1",
        "index.js",
        "export const useState = () => 1;\n",
    );
    let vendor = partial_swap_vendor(
        "namespace re-export consumer fixture",
        json!({ "react": { "version": "18.3.1", "subpath": "index.js" } }),
        json!({ "a": { "package": "react", "kind": "namespace" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("react", &package_root)]);

    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let caller = fs::read_to_string(&fixture.caller_emitted_path).expect("caller emitted");
    assert!(
        caller.contains("export * as React from \"react\""),
        "re-export of a kind=namespace symbol should forward the package namespace:\n{caller}",
    );

    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "react",
        "18.3.1",
        "export const useState = () => 1;\n",
    );
    let probe_path = app_root.join("__run_reexport_namespace.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./static/app/entry.js\");\n\
         console.log(m.React.useState());\n",
    );
    assert_node_output(&probe_path, "1\n", "");
}

#[test]
fn partial_swap_bails_on_namespace_import_of_partially_swapped_chunk() {
    // `import * as M from "<chunk>"` then `M.e6` — the strip pass removes
    // `e6` from the chunk's export surface, so the member read would
    // silently evaluate to `undefined` at runtime. The post-strip consumer
    // gate must reject the spec instead of emitting the broken tree.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-namespace-consumer-",
        "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        "import * as M from \"../megachunk/entry.js\";\nexport const r = M.e6();\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const boolean = () => true;\n",
    );
    let vendor = partial_swap_vendor(
        "namespace consumer fixture",
        json!({ "zod": { "version": "3.23.8", "subpath": "lib/index.mjs", "namespace": "z" } }),
        json!({ "e6": { "package": "zod", "upstream_export": "boolean" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("zod", &package_root)]);

    assert!(
        !fixture.result.status.success(),
        "debundler must reject a namespace import of a partially-swapped chunk\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("namespace")
            && fixture.result.stderr.contains("static/megachunk"),
        "expected namespace-consumer gate diagnostic in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn partial_swap_bails_on_member_kind_reexport_from_consumer() {
    // kind=member symbols rewrite references to `<namespace>.<export>`
    // member accesses — that shape has no live re-export equivalent, so a
    // re-export consumer of a member-kind symbol must hard-fail rather
    // than silently survive the strip with a dangling export name.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-member-reexport-",
        "export const e6 = () => true;\n",
        "export { e6 as zodBoolean } from \"../megachunk/entry.js\";\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const boolean = () => true;\n",
    );
    let vendor = partial_swap_vendor(
        "member re-export consumer fixture",
        json!({ "zod": { "version": "3.23.8", "subpath": "lib/index.mjs", "namespace": "z" } }),
        json!({ "e6": { "package": "zod", "upstream_export": "boolean" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("zod", &package_root)]);

    assert!(
        !fixture.result.status.success(),
        "debundler must reject a member-kind re-export consumer\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("e6") && fixture.result.stderr.contains("re-export"),
        "expected re-export gate diagnostic naming the swapped symbol:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn partial_swap_bails_on_export_star_from_partially_swapped_chunk() {
    // `export * from "<chunk>"` re-exports whatever survives the strip —
    // the swapped names silently vanish from the re-exporter's surface.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "vendor-partial-swap-export-star-",
        "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        "export * from \"../megachunk/entry.js\";\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const boolean = () => true;\n",
    );
    let vendor = partial_swap_vendor(
        "export-star consumer fixture",
        json!({ "zod": { "version": "3.23.8", "subpath": "lib/index.mjs", "namespace": "z" } }),
        json!({ "e6": { "package": "zod", "upstream_export": "boolean" } }),
    );
    let fixture = run_partial_swap_raw(ws, vendor, &[("zod", &package_root)]);

    assert!(
        !fixture.result.status.success(),
        "debundler must reject `export *` from a partially-swapped chunk\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("export *")
            && fixture.result.stderr.contains("static/megachunk"),
        "expected export-star gate diagnostic in stderr:\n{}",
        fixture.result.stderr,
    );
}

// ─── boundary_rename / suppress ─────────────────────────────────────────

#[test]
fn boundary_rename_rewrites_caller_imports_end_to_end() {
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new("vendor-boundary-rename-");
    ws.write_chunk(VENDOR_PATH, "const a = 1;\nexport { a as alpha };\n");
    ws.write_chunk(
        APP_PATH,
        "import { a } from \"../vendor/entry.js\";\nconsole.log(a);\n",
    );
    ws.write_js_list(&format!("{VENDOR_PATH}\n{APP_PATH}\n"));

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            VENDOR_PATH: { "level": "boundary_rename", "identity": "boundary rename fixture" },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);
    let result = run_debundler(&spec_path, &[]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let caller =
        fs::read_to_string(ws.out_root.join("app/static/app/entry.js")).expect("caller emitted");
    assert!(
        caller.contains("import { alpha as a }"),
        "caller import should be rewritten to the vendor's public export name:\n{caller}",
    );
    let probe_path = ws.out_root.join("__run_boundary_rename.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "1\n", "");
}

#[test]
fn boundary_rename_bails_when_mapping_key_is_a_real_export_of_another_local() {
    // The boundary mapping is keyed by vendor-LOCAL name (`export { a as
    // alpha }` → a→alpha). If the vendor ALSO genuinely exports the name
    // `a` bound to a *different* local (`export { z as a }`), a caller's
    // `import { a }` refers to local `z` — rewriting it to `import { alpha
    // as a }` would silently rebind it to local `a`'s value. The stage must
    // bail on the colliding shape.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new("vendor-boundary-rename-collision-");
    ws.write_chunk(
        VENDOR_PATH,
        "const a = \"local-a\";\nconst z = \"local-z\";\nexport { a as alpha, z as a };\n",
    );
    ws.write_chunk(
        APP_PATH,
        "import { a } from \"../vendor/entry.js\";\nconsole.log(a);\n",
    );
    ws.write_js_list(&format!("{VENDOR_PATH}\n{APP_PATH}\n"));

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            VENDOR_PATH: { "level": "boundary_rename", "identity": "boundary rename collision fixture" },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);
    let result = run_debundler(&spec_path, &[]);
    assert!(
        !result.status.success(),
        "debundler must reject a boundary mapping key that collides with a real export\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    assert!(
        result.stderr.contains("collides") && result.stderr.contains("`a`"),
        "expected boundary-rename collision diagnostic in stderr:\n{}",
        result.stderr,
    );
}

#[test]
fn suppress_vendor_chunk_passes_through_unchanged() {
    // `level: suppress` is annotation-only: no boundary rename, no swap,
    // no strip. The chunk and its callers must pass through untouched.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new("vendor-suppress-");
    ws.write_chunk(VENDOR_PATH, "const a = 1;\nexport { a as alpha };\n");
    ws.write_chunk(
        APP_PATH,
        "import { alpha } from \"../vendor/entry.js\";\nconsole.log(alpha);\n",
    );
    ws.write_js_list(&format!("{VENDOR_PATH}\n{APP_PATH}\n"));

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            VENDOR_PATH: { "level": "suppress", "identity": "suppress fixture" },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);
    let result = run_debundler(&spec_path, &[]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let vendor = fs::read_to_string(ws.out_root.join("app/static/vendor/entry.js"))
        .expect("vendor chunk emitted");
    assert!(
        vendor.contains("export { a as alpha }"),
        "suppressed vendor chunk's export surface must be unchanged:\n{vendor}",
    );
    let caller =
        fs::read_to_string(ws.out_root.join("app/static/app/entry.js")).expect("caller emitted");
    assert!(
        caller.contains("import { alpha } from \"../vendor/entry.js\""),
        "caller import of a suppressed chunk must be unchanged:\n{caller}",
    );
    let probe_path = ws.out_root.join("__run_suppress.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "1\n", "");
}

#[test]
fn suppress_vendor_chunk_skips_specifier_canonicalization() {
    // Golden pin for vendor_into_emission open question 3: suppress
    // means hands-off, so the pass-through emission rewriter skips
    // suppress-marked chunks entirely — even directives the
    // canonicalizer rewrites in every other chunk (`./helper-X.js` →
    // `../helper-X/entry.js`) keep their original spelling, making the
    // documented byte-compat contract true. (The old always-on stage 0
    // canonicalized suppress chunks too; this diff is intentional.)
    const VENDOR_PATH: &str = "static/vendor.js";
    const HELPER_PATH: &str = "static/helper-X.js";
    let ws = VendorTestWorkspace::new("vendor-suppress-canonicalize-");
    ws.write_chunk(
        VENDOR_PATH,
        "import { h } from \"./helper-X.js\";\nexport const a = h;\n",
    );
    ws.write_chunk(HELPER_PATH, "export const h = 1;\n");
    ws.write_js_list(&format!("{VENDOR_PATH}\n{HELPER_PATH}\n"));

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            VENDOR_PATH: { "level": "suppress", "identity": "suppress canonicalization fixture" },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);
    let result = run_debundler(&spec_path, &[]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let vendor = fs::read_to_string(ws.out_root.join("app/static/vendor/entry.js"))
        .expect("vendor chunk emitted");
    assert!(
        vendor.contains("\"./helper-X.js\""),
        "suppress chunk directives must keep their original spelling:\n{vendor}",
    );
    assert!(
        !vendor.contains("../helper-X/entry.js"),
        "suppress chunk directives must not be canonicalized:\n{vendor}",
    );
}

// ─── full swap: caller contract + export-shape validation ───────────────

#[test]
fn full_swap_with_caller_keeps_dangling_chunk_import_for_live_proxy() {
    // `level: swap` removes the vendor chunk from the artifact; callers'
    // imports of the removed chunk are intentionally left as-is in the
    // plain `write_js_tree` output. The emitted tree is proxy-dependent:
    // the live-proxy/import-map layer resolves the dangling specifier to
    // the swapped package at serve time. This test pins that contract.
    const CHUNK_PATH: &str = "static/lib-X.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "lib";
    let ws = VendorTestWorkspace::new("vendor-full-swap-caller-");
    ws.write_chunk(CHUNK_PATH, "export const ping = () => \"vendor\";\n");
    ws.write_chunk(
        APP_PATH,
        "import { ping } from \"../lib-X/entry.js\";\nconsole.log(ping());\n",
    );
    ws.write_js_list(&format!("{CHUNK_PATH}\n{APP_PATH}\n"));
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        "1.0.0",
        "dist/index.mjs",
        "export const ping = () => \"pkg\";\n",
    );

    let spec_path = ws.root.path().join("transform_spec.yaml");
    let spec = json!({
        "vendor": {
            CHUNK_PATH: {
                "level": "swap",
                "identity": "lib/dist/index.mjs",
                "package": PACKAGE_NAME,
                "version": "1.0.0",
                "subpath": "dist/index.mjs",
            },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "swap_vendor_chunks": {
            "output_manifest_path": &ws.manifest_path,
            "output_wrapper_dir": &ws.wrapper_root,
            "write": true,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    write_yaml_file(&spec_path, &spec);
    let result = run_debundler(&spec_path, &[(PACKAGE_NAME, &package_root)]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    assert!(
        !ws.out_root.join("app/static/lib-X").exists(),
        "fully-swapped chunk must be removed from the emitted tree",
    );
    let caller =
        fs::read_to_string(ws.out_root.join("app/static/app/entry.js")).expect("caller emitted");
    assert!(
        caller.contains("../lib-X/entry.js"),
        "caller's import of the removed chunk is intentionally left dangling \
         (resolved by the live-proxy/import-map at serve time):\n{caller}",
    );
}

#[test]
fn full_swap_without_wrapper_requires_upstream_default_for_default_export() {
    // `export default x;` (ExportDefaultExpr) must participate in the
    // no-wrapper export-shape check the same way `export { x as default }`
    // does: if the upstream package has no default export, callers that
    // import the chunk's default would break post-swap.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-full-swap-default-expr-",
        chunk_source: "const x = 0;\nexport default x;\n",
        wrapper_shape: None,
        upstream_source: "export const unrelated = 1;\n",
        default_export_aliases: &[],
    });
    assert!(
        !fixture.result.status.success(),
        "debundler must reject a default-exporting chunk swapped against an upstream without a default\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("export shape mismatch")
            && fixture.result.stderr.contains("default"),
        "expected export-shape mismatch naming `default` in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn full_swap_without_wrapper_accepts_named_default_alias_when_upstream_has_default() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-full-swap-named-default-",
        chunk_source: "const x = 0;\nexport { x as default };\n",
        wrapper_shape: None,
        upstream_source: "const d = 1;\nexport default d;\n",
        default_export_aliases: &[],
    });
    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
}

// ─── wrapper synthetic-local collisions ─────────────────────────────────

#[test]
fn named_from_default_wrapper_avoids_upstream_default_local_collision() {
    // The wrapper hoists upstream's default into a synthetic local
    // (historically `_d`). If the upstream module body already binds that
    // name, naive injection produces a duplicate-declaration SyntaxError.
    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source: "const _d = \"taken\";\nexport default { ping: () => _d };\n",
        chunk_source: "export { ping } from \"lib\";\n",
    });
    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let probe_path = fixture
        .wrapper_path
        .parent()
        .expect("wrapper has parent dir")
        .join("__probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./entry.js\");\nconsole.log(m.ping());\n",
    );
    assert_node_output(&probe_path, "taken\n", "");
}

#[test]
fn named_from_module_default_wrapper_avoids_upstream_default_local_collision() {
    let upstream_source = "const __vendor_default__ = \"taken\";\n\
                           export default function getter() { return __vendor_default__; }\n";
    let fixture = run_named_from_module_default_fixture(upstream_source);
    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let probe_path = fixture
        .wrapper_path
        .parent()
        .expect("wrapper has parent dir")
        .join("__probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./entry.js\");\nconsole.log(m.default());\n",
    );
    assert_node_output(&probe_path, "taken\n", "");
}

// ─── named_from_module_default named-export verification ────────────────

#[test]
fn named_from_module_default_rejects_unverified_named_exports() {
    // The wrapper emits `export const <name> = <default>;` for every
    // vendor named export. That equation only holds when the chunk itself
    // binds `<name>` to the same local as its default export. A genuine
    // independent export (`export { y as other }`) must make the swap
    // bail instead of silently re-exporting the upstream default under
    // an unrelated name.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-module-default-overclaim-",
        chunk_source: "const x = 0;\nconst y = 1;\nexport { x as default, y as other };\n",
        wrapper_shape: Some("named_from_module_default"),
        upstream_source: "export default function f() { return \"pkg\"; }\n",
        default_export_aliases: &[],
    });
    assert!(
        !fixture.result.status.success(),
        "debundler must reject named exports that are not verified aliases of the chunk default\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("named-from-module-default")
            && fixture.result.stderr.contains("other"),
        "expected over-claim diagnostic naming `other` in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn named_from_module_default_accepts_verified_default_aliases() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-module-default-alias-",
        chunk_source: "const x = () => \"val\";\nexport { x as default, x as alias };\n",
        wrapper_shape: Some("named_from_module_default"),
        upstream_source: "export default function f() { return \"val\"; }\n",
        default_export_aliases: &[],
    });
    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let probe_path = fixture
        .wrapper_path
        .parent()
        .expect("wrapper has parent dir")
        .join("__probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./entry.js\");\nconsole.log(m.alias === m.default);\n",
    );
    assert_node_output(&probe_path, "true\n", "");
}

#[test]
fn named_from_module_default_rejects_single_named_export_without_assertion() {
    // Tana's cytoscape chunk shape: the package default is re-exported under
    // a single minified name (`export { Ft as c }`) with no `default` export
    // of its own. The static check cannot prove `c` aliases the package
    // default from the chunk alone, so without an explicit assertion the
    // swap must bail rather than silently re-export the default under `c`.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-module-default-single-unasserted-",
        chunk_source: "const Ft = () => \"cy\";\nexport { Ft as c };\n",
        wrapper_shape: Some("named_from_module_default"),
        upstream_source: "export default function f() { return \"cy\"; }\n",
        default_export_aliases: &[],
    });
    assert!(
        !fixture.result.status.success(),
        "debundler must reject an unasserted single-named-export chunk\nstdout:\n{}\nstderr:\n{}",
        fixture.result.stdout,
        fixture.result.stderr,
    );
    assert!(
        fixture.result.stderr.contains("named-from-module-default")
            && fixture.result.stderr.contains("c"),
        "expected unverified-alias diagnostic naming `c` in stderr:\n{}",
        fixture.result.stderr,
    );
}

#[test]
fn named_from_module_default_admits_authored_default_alias() {
    // Same chunk shape as above, but the author asserts via
    // `default_export_aliases` that `c` is the package default. The swap
    // then succeeds and the wrapper re-exports the package default under `c`.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-module-default-single-asserted-",
        chunk_source: "const Ft = () => \"cy\";\nexport { Ft as c };\n",
        wrapper_shape: Some("named_from_module_default"),
        upstream_source: "export default function f() { return \"cy\"; }\n",
        default_export_aliases: &["c"],
    });
    assert!(
        fixture.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.result.status.code(),
        fixture.result.stdout,
        fixture.result.stderr,
    );
    let probe_path = fixture
        .wrapper_path
        .parent()
        .expect("wrapper has parent dir")
        .join("__probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./entry.js\");\nconsole.log(m.c === m.default && m.c() === \"cy\");\n",
    );
    assert_node_output(&probe_path, "true\n", "");
}

// ─── named_from_json_default ────────────────────────────────────────────

#[test]
fn named_from_json_default_generates_named_pulls_from_json_keys() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-named-from-json-default-",
        chunk_source: "export { version, flag } from \"lib\";\n",
        wrapper_shape: Some("named_from_json_default"),
        upstream_source: "{ \"version\": \"1.2.3\", \"flag\": true }\n",
        default_export_aliases: &[],
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
        wrapper_source.contains("export const version = _d.version;")
            && wrapper_source.contains("export const flag = _d.flag;")
            && wrapper_source.contains("export default _d;"),
        "JSON wrapper should pull each named export off the parsed default:\n{wrapper_source}",
    );
    let probe_path = fixture
        .wrapper_path
        .parent()
        .expect("wrapper has parent dir")
        .join("__probe.mjs");
    write_text_file(
        &probe_path,
        "const m = await import(\"./entry.js\");\n\
         console.log(`${m.version}:${m.flag}:${m.default.version}`);\n",
    );
    assert_node_output(&probe_path, "1.2.3:true:1.2.3\n", "");
}

#[test]
fn named_from_json_default_rejects_names_missing_from_json() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        temp_prefix: "vendor-swap-named-from-json-default-missing-",
        chunk_source: "export { missing } from \"lib\";\n",
        wrapper_shape: Some("named_from_json_default"),
        upstream_source: "{ \"version\": \"1.2.3\" }\n",
        default_export_aliases: &[],
    });
    assert!(
        !fixture.result.status.success(),
        "debundler must reject vendor named exports missing from the upstream JSON keys",
    );
    assert!(
        fixture
            .result
            .stderr
            .contains("named-from-json-default wrapper shape mismatch"),
        "expected JSON wrapper shape mismatch in stderr:\n{}",
        fixture.result.stderr,
    );
}
