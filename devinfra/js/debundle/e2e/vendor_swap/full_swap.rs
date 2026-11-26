//! Full swaps replace a vendor chunk with a generated wrapper over the upstream package.

use super::*;

#[test]
fn named_from_module_default_accepts_all_upstream_export_forms() {
    // Named exports before/after their declaration and anonymous function/class
    // defaults must all avoid TDZ and preserve the wrapper's runtime surface.
    for upstream_source in [
        r#"const lib = { ping() { return "pong"; } };
const aux = "side";
export { lib as default, aux };
"#,
        r#"export { lib as default };
const lib = { ping() { return "pong"; } };
"#,
        r#"export default function () { return "ping"; }
"#,
        r#"export default class {
  ping() { return "ping"; }
}
"#,
    ] {
        let fixture = run_named_from_module_default_fixture(upstream_source);
        assert_wrapper_named_from_module_default(&fixture);
    }
}

fn assert_wrapper_named_from_module_default(fixture: &VendorSwapFixture) {
    assert_success(&fixture.result);

    assert_source_file(
        &fixture.wrapper_path,
        &["ping", "export default"],
        &["as default"],
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

fn run_named_from_module_default_fixture(upstream_source: &str) -> VendorSwapFixture {
    run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "export { x as default };\nconst x = 0;\n",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source,
        default_export_aliases: &[],
    })
}

#[test]
fn named_from_default_accepts_object_literal_prop_forms() {
    // The wrapper picks up each key of upstream's object-literal
    // `export default` and re-exports it as a named export. Every prop form
    // must yield the same values.
    for upstream_source in [
        // Canonical accepted shape: `KeyValue` props with `Ident` names.
        r#"export default {
  ping: () => "pong",
  pong: () => "ping",
};
"#,
        // Shorthand props (`{ ping, pong }` — local binding names used
        // directly as both key and value). Generated export locals must
        // avoid the upstream bindings rather than redeclaring them.
        r#"const ping = () => "pong";
const pong = () => "ping";
export default { ping, pong };
"#,
        // Mixed: `Shorthand` plus a `KeyValue` prop with a `Str` name in the
        // same default export.
        r#"const ping = () => "pong";
export default { ping, "pong": () => "ping" };
"#,
    ] {
        let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
            upstream_source,
            chunk_source: "export { ping, pong } from \"lib\";\n",
        });

        assert_wrapper_output(
            &fixture,
            "import d, { ping, pong } from './entry.js'; console.log(ping(), pong(), ping === d.ping, pong === d.pong);",
            "pong ping true true\n",
        );
    }
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
    // `named_from_module_default_accepts_all_upstream_export_forms`).
    let upstream_source = r#"export default class {
  ping() { return "pong"; }
}
"#;

    let fixture = run_named_from_default_fixture(NamedFromDefaultFixtureArgs {
        upstream_source,
        chunk_source: "export { ping } from \"lib\";\n",
    });

    assert_rejected(
        &fixture.result,
        &["named-from-default: upstream has no export default declaration"],
    );
}

struct NamedFromDefaultFixtureArgs<'a> {
    upstream_source: &'a str,
    chunk_source: &'a str,
}

fn run_named_from_default_fixture(args: NamedFromDefaultFixtureArgs<'_>) -> VendorSwapFixture {
    run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: args.chunk_source,
        wrapper_shape: Some(WrapperShape::NamedFromDefault),
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
fn full_swap_bails_when_mapping_key_is_a_real_export_of_another_local() {
    // The boundary mapping is keyed by vendor-LOCAL name (`export { a as
    // alpha }` → a→alpha). If the vendor ALSO genuinely exports the name
    // `a` bound to a *different* local (`export { z as a }`), a caller's
    // `import { a }` refers to local `z` — rewriting it to `import { alpha
    // as a }` would silently rebind it to local `a`'s value. The stage must
    // bail on the colliding shape.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (
            VENDOR_PATH,
            "const a = \"local-a\";\nconst z = \"local-z\";\nexport { a as alpha, z as a };\n",
        ),
        (
            APP_PATH,
            "import { a } from \"../vendor/entry.js\";\nconsole.log(a);\n",
        ),
    ]);

    let spec = json!({
        "vendor": {
            VENDOR_PATH: {
                "level": "swap", "identity": "boundary collision fixture",
                "package": "fixture-package", "version": "1.0.0", "subpath": "index.js",
            },
        },
        "inputs": { "input_root": &ws.snapshot_root, "js_list_path": &ws.js_list_path },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    let result = ws.run_spec(&spec, &[]);
    assert_rejected(&result, &["collides", "`a`"]);
}

// ─── full swap: caller contract + export-shape validation ───────────────

#[test]
fn full_swap_with_caller_preserves_import_for_removed_chunk() {
    // `level: swap` removes the vendor chunk from the emitted tree and
    // leaves the caller's import path intact. A host maps that path to the
    // swapped package; this test pins the emitted path.
    const CHUNK_PATH: &str = "static/lib-X.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "lib";
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (CHUNK_PATH, "export const ping = () => \"vendor\";\n"),
        (
            APP_PATH,
            "import { ping } from \"../lib-X/entry.js\";\nconsole.log(ping());\n",
        ),
    ]);
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        "1.0.0",
        "dist/index.mjs",
        "export const ping = () => \"pkg\";\n",
    );

    let spec = ws.transform_spec(json!({
        CHUNK_PATH: {
            "level": "swap",
            "identity": "lib/dist/index.mjs",
            "package": PACKAGE_NAME,
            "version": "1.0.0",
            "subpath": "dist/index.mjs",
        },
    }));
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

    assert!(
        !ws.out_root.join("app/static/lib-X").exists(),
        "fully-swapped chunk must be removed from the emitted tree",
    );
    // The caller import is intentionally dangling and resolved by the host import map.
    assert_source_file(
        &ws.out_root.join("app/static/app/entry.js"),
        &["../lib-X/entry.js"],
        &[],
    );
}

#[test]
fn full_swap_without_wrapper_requires_upstream_default_for_default_export() {
    // `export default x;` (ExportDefaultExpr) must participate in the
    // no-wrapper export-shape check the same way `export { x as default }`
    // does: if the upstream package has no default export, callers that
    // import the chunk's default would break post-swap.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const x = 0;\nexport default x;\n",
        wrapper_shape: None,
        upstream_source: "export const unrelated = 1;\n",
        default_export_aliases: &[],
    });
    assert_rejected(&fixture.result, &["export shape mismatch", "default"]);
}

#[test]
fn full_swap_without_wrapper_accepts_named_default_alias_when_upstream_has_default() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const x = 0;\nexport { x as default };\n",
        wrapper_shape: None,
        upstream_source: "const d = 1;\nexport default d;\n",
        default_export_aliases: &[],
    });
    assert_success(&fixture.result);
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
    assert_wrapper_output(
        &fixture,
        "const m = await import(\"./entry.js\");\nconsole.log(m.ping());\n",
        "taken\n",
    );
}

#[test]
fn named_from_module_default_wrapper_avoids_upstream_default_local_collision() {
    let upstream_source = "const __vendor_default__ = \"taken\";\n\
                           export default function getter() { return __vendor_default__; }\n";
    let fixture = run_named_from_module_default_fixture(upstream_source);
    assert_wrapper_output(
        &fixture,
        "const m = await import(\"./entry.js\");\nconsole.log(m.default());\n",
        "taken\n",
    );
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
        chunk_source: "const x = 0;\nconst y = 1;\nexport { x as default, y as other };\n",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source: "export default function f() { return \"pkg\"; }\n",
        default_export_aliases: &[],
    });
    assert_rejected(&fixture.result, &["named-from-module-default", "other"]);
}

#[test]
fn named_from_module_default_accepts_verified_default_aliases() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const x = () => \"val\";\nexport { x as default, x as alias };\n",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source: "export default function f() { return \"val\"; }\n",
        default_export_aliases: &[],
    });
    assert_wrapper_output(
        &fixture,
        "const m = await import(\"./entry.js\");\nconsole.log(m.alias === m.default);\n",
        "true\n",
    );
}

#[test]
fn named_from_module_default_rejects_single_named_export_without_assertion() {
    // Tana's cytoscape chunk shape: the package default is re-exported under
    // a single minified name (`export { Ft as c }`) with no `default` export
    // of its own. The static check cannot prove `c` aliases the package
    // default from the chunk alone, so without an explicit assertion the
    // swap must bail rather than silently re-export the default under `c`.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const Ft = () => \"cy\";\nexport { Ft as c };\n",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source: "export default function f() { return \"cy\"; }\n",
        default_export_aliases: &[],
    });
    assert_rejected(&fixture.result, &["named-from-module-default", "c"]);
}

#[test]
fn named_from_module_default_admits_authored_default_alias() {
    // Same chunk shape as above, but the author asserts via
    // `default_export_aliases` that `c` is the package default. The swap
    // then succeeds and the wrapper re-exports the package default under `c`.
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const Ft = () => \"cy\";\nexport { Ft as c };\n",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source: "export default function f() { return \"cy\"; }\n",
        default_export_aliases: &["c"],
    });
    assert_wrapper_output(
        &fixture,
        "const m = await import(\"./entry.js\");\nconsole.log(m.c === m.default && m.c() === \"cy\");\n",
        "true\n",
    );
}

// ─── named_from_json_default ────────────────────────────────────────────

#[test]
fn named_from_json_default_generates_named_pulls_from_json_keys() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "export { version, flag } from \"lib\";\n",
        wrapper_shape: Some(WrapperShape::NamedFromJsonDefault),
        upstream_source: "{ \"version\": \"1.2.3\", \"flag\": true }\n",
        default_export_aliases: &[],
    });
    assert_success(&fixture.result);
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
        chunk_source: "export { missing } from \"lib\";\n",
        wrapper_shape: Some(WrapperShape::NamedFromJsonDefault),
        upstream_source: "{ \"version\": \"1.2.3\" }\n",
        default_export_aliases: &[],
    });
    assert_rejected(
        &fixture.result,
        &["named-from-json-default wrapper shape mismatch"],
    );
}
