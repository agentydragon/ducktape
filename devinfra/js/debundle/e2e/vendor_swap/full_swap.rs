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
        in_case(upstream_source, || {
            let fixture = full_swap(
                "export { x as default };\nconst x = 0;\n",
                Some(WrapperShape::NamedFromModuleDefault),
                upstream_source,
                &[],
            );
            assert_wrapper_named_from_module_default(&fixture);
        });
    }
}

fn assert_wrapper_named_from_module_default(fixture: &VendorSwapFixture) {
    assert_success(&fixture.result);

    assert_source_file(
        &fixture.wrapper_path,
        &["ping", "export default"],
        &["as default"],
    );

    // `generated_wrapper_path` is recorded relative to the manifest's own
    // directory, so resolving it there must give the wrapper's on-disk path.
    let manifest: Value = read_json(&fixture.manifest_path);
    let recorded = manifest["full"][&fixture.chunk_path]["generated_wrapper_path"]
        .as_str()
        .expect("manifest records generated_wrapper_path");
    let manifest_dir = fixture
        .manifest_path
        .parent()
        .expect("manifest path has a parent");
    assert_eq!(
        fs::canonicalize(manifest_dir.join(recorded))
            .expect("resolved manifest path canonicalizes"),
        fs::canonicalize(&fixture.wrapper_path).expect("wrapper path canonicalizes"),
        "manifest's generated_wrapper_path ({recorded}) must resolve to the wrapper file ({:?})",
        fixture.wrapper_path,
    );
}

struct WrapperCase {
    name: &'static str,
    chunk: &'static str,
    upstream: &'static str,
    default_export_aliases: &'static [&'static str],
    /// Probe module run beside the generated wrapper `entry.js`.
    script: &'static str,
    expected: &'static str,
}

fn assert_wrapper_cases(shape: WrapperShape, cases: &[WrapperCase]) {
    for case in cases {
        in_case(case.name, || {
            let fixture = full_swap(
                case.chunk,
                Some(shape),
                case.upstream,
                case.default_export_aliases,
            );
            assert_wrapper_output(&fixture, case.script, case.expected);
        });
    }
}

const PING_PONG_SCRIPT: &str = "import d, { ping, pong } from './entry.js'; console.log(ping(), pong(), ping === d.ping, pong === d.pong);";

/// `named_from_default` re-exports each key of upstream's object-literal
/// `export default` as a named export.
#[test]
fn named_from_default_wrapper_runtime_surface() {
    assert_wrapper_cases(
        WrapperShape::NamedFromDefault,
        &[
            WrapperCase {
                name: "key-value props with identifier names",
                chunk: "export { ping, pong } from \"lib\";\n",
                upstream: "export default {\n  ping: () => \"pong\",\n  pong: () => \"ping\",\n};\n",
                default_export_aliases: &[],
                script: PING_PONG_SCRIPT,
                expected: "pong ping true true\n",
            },
            // Generated export locals must avoid the upstream bindings
            // rather than redeclaring them.
            WrapperCase {
                name: "shorthand props",
                chunk: "export { ping, pong } from \"lib\";\n",
                upstream: "const ping = () => \"pong\";\nconst pong = () => \"ping\";\nexport default { ping, pong };\n",
                default_export_aliases: &[],
                script: PING_PONG_SCRIPT,
                expected: "pong ping true true\n",
            },
            WrapperCase {
                name: "shorthand mixed with a string-named prop",
                chunk: "export { ping, pong } from \"lib\";\n",
                upstream: "const ping = () => \"pong\";\nexport default { ping, \"pong\": () => \"ping\" };\n",
                default_export_aliases: &[],
                script: PING_PONG_SCRIPT,
                expected: "pong ping true true\n",
            },
            // The wrapper hoists upstream's default into a synthetic local
            // (historically `_d`); naive injection would redeclare it.
            WrapperCase {
                name: "upstream binds the synthetic default local",
                chunk: "export { ping } from \"lib\";\n",
                upstream: "const _d = \"taken\";\nexport default { ping: () => _d };\n",
                default_export_aliases: &[],
                script: "const m = await import(\"./entry.js\");\nconsole.log(m.ping());\n",
                expected: "taken\n",
            },
            WrapperCase {
                name: "reserved and string export names",
                chunk: "const a = 0; export { a as class, a as 'x-y' };",
                upstream: "export default { class: 1, 'x-y': 2 };",
                default_export_aliases: &[],
                script: "import * as m from './entry.js'; console.log(m.class, m['x-y']);",
                expected: "1 2\n",
            },
        ],
    );
}

#[test]
fn named_from_module_default_wrapper_runtime_surface() {
    assert_wrapper_cases(
        WrapperShape::NamedFromModuleDefault,
        &[
            WrapperCase {
                name: "upstream binds the synthetic default local",
                chunk: "export { x as default };\nconst x = 0;\n",
                upstream: "const __vendor_default__ = \"taken\";\nexport default function getter() { return __vendor_default__; }\n",
                default_export_aliases: &[],
                script: "const m = await import(\"./entry.js\");\nconsole.log(m.default());\n",
                expected: "taken\n",
            },
            WrapperCase {
                name: "chunk proves a named export aliases its default",
                chunk: "const x = () => \"val\";\nexport { x as default, x as alias };\n",
                upstream: "export default function f() { return \"val\"; }\n",
                default_export_aliases: &[],
                script: "const m = await import(\"./entry.js\");\nconsole.log(m.alias === m.default);\n",
                expected: "true\n",
            },
            // Tana's cytoscape chunk shape: the package default re-exported
            // under a single minified name with no `default` export; the
            // author asserts `c` is the package default.
            WrapperCase {
                name: "authored default alias",
                chunk: "const Ft = () => \"cy\";\nexport { Ft as c };\n",
                upstream: "export default function f() { return \"cy\"; }\n",
                default_export_aliases: &["c"],
                script: "const m = await import(\"./entry.js\");\nconsole.log(m.c === m.default && m.c() === \"cy\");\n",
                expected: "true\n",
            },
            WrapperCase {
                name: "reserved, string and upstream-colliding aliases",
                chunk: "const x = {}; export { x as class, x as 'x-y', x as existing };",
                upstream: "const existing = { value: 7 }; export default existing;",
                default_export_aliases: &["class", "x-y", "existing"],
                script: "import d, * as m from './entry.js'; console.log(m.class === d, m['x-y'] === d, m.existing.value);",
                expected: "true true 7\n",
            },
        ],
    );
}

#[test]
fn named_from_json_default_wrapper_runtime_surface() {
    assert_wrapper_cases(
        WrapperShape::NamedFromJsonDefault,
        &[
            WrapperCase {
                name: "named pulls from JSON keys",
                chunk: "export { version, flag } from \"lib\";\n",
                upstream: "{ \"version\": \"1.2.3\", \"flag\": true }\n",
                default_export_aliases: &[],
                script: "const m = await import(\"./entry.js\");\n\
                         console.log(`${m.version}:${m.flag}:${m.default.version}`);\n",
                expected: "1.2.3:true:1.2.3\n",
            },
            WrapperCase {
                name: "reserved and string export names",
                chunk: "const a = 0; export { a as class, a as 'x-y' };",
                upstream: "{\"class\":1,\"x-y\":2}",
                default_export_aliases: &[],
                script: "import * as m from './entry.js'; console.log(m.class, m['x-y']);",
                expected: "1 2\n",
            },
            WrapperCase {
                name: "__proto__ stays an own data property",
                chunk: "export const value = 0;",
                upstream: "{\"value\":1,\"__proto__\":{\"nested\":2}}",
                default_export_aliases: &[],
                script: "import data from './entry.js'; console.log(Object.hasOwn(data, '__proto__'), Object.getPrototypeOf(data) === Object.prototype);",
                expected: "true true\n",
            },
            WrapperCase {
                name: "export names do not shadow JSON or the payload local",
                chunk: "export const JSON = 0, _d = 0;",
                upstream: "{\"JSON\":1,\"_d\":2}",
                default_export_aliases: &[],
                script: "import * as m from './entry.js'; console.log(m.JSON, m._d);",
                expected: "1 2\n",
            },
        ],
    );
}

#[test]
fn full_swap_rejects_unprovable_export_shapes() {
    for (name, chunk, shape, upstream, diagnostics) in [
        // `export default class` is an `ExportDefaultDecl`, so the search
        // bails before the object-literal check. Out of scope on purpose:
        // `_d.K` on a class/function default reads static properties, not
        // the useful surface; such vendors use `named_from_module_default`.
        (
            "named_from_default over a class default",
            "export { ping } from \"lib\";\n",
            Some(WrapperShape::NamedFromDefault),
            "export default class {\n  ping() { return \"pong\"; }\n}\n",
            &["named-from-default: upstream has no export default declaration"][..],
        ),
        // `export default x;` (ExportDefaultExpr) participates in the
        // no-wrapper export-shape check like `export { x as default }`.
        (
            "no wrapper, upstream lacks the chunk's default",
            "const x = 0;\nexport default x;\n",
            None,
            "export const unrelated = 1;\n",
            &["export shape mismatch", "default"],
        ),
        // The wrapper emits `export const <name> = <default>;`, which only
        // holds when the chunk binds `<name>` to its default's local.
        (
            "named_from_module_default with an independent named export",
            "const x = 0;\nconst y = 1;\nexport { x as default, y as other };\n",
            Some(WrapperShape::NamedFromModuleDefault),
            "export default function f() { return \"pkg\"; }\n",
            &["named-from-module-default", "other"],
        ),
        // Anchor for the authored-alias wrapper case: without the
        // assertion the chunk alone cannot prove `c` is the default.
        (
            "named_from_module_default single named export without assertion",
            "const Ft = () => \"cy\";\nexport { Ft as c };\n",
            Some(WrapperShape::NamedFromModuleDefault),
            "export default function f() { return \"cy\"; }\n",
            &["named-from-module-default", "c"],
        ),
        (
            "named_from_json_default name missing from JSON",
            "export { missing } from \"lib\";\n",
            Some(WrapperShape::NamedFromJsonDefault),
            "{ \"version\": \"1.2.3\" }\n",
            &["named-from-json-default wrapper shape mismatch"],
        ),
    ] {
        in_case(name, || {
            assert_rejected(&full_swap(chunk, shape, upstream, &[]).result, diagnostics);
        });
    }
}

#[test]
fn full_swap_without_wrapper_accepts_named_default_alias_when_upstream_has_default() {
    let fixture = full_swap(
        "const x = 0;\nexport { x as default };\n",
        None,
        "const d = 1;\nexport default d;\n",
        &[],
    );
    assert_success(&fixture.result);
}

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

    let result = ws.run(
        json!({
            CHUNK_PATH: {
                "level": "swap",
                "identity": "lib/dist/index.mjs",
                "package": PACKAGE_NAME,
                "version": "1.0.0",
                "subpath": "dist/index.mjs",
            },
        }),
        &[(PACKAGE_NAME, &package_root)],
    );
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
