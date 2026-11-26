//! Partial-swap import kinds, materialized modules and consumer re-exports rewrite consistently.

use super::*;

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

    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (
            VENDOR_PATH,
            "export const q = () => \"vendor\";\nexport const keep = () => 7;\n",
        ),
        (
            APP_PATH,
            "const q = () => \"app\";\nexport function out() { return q(); }\n",
        ),
    ]);
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        "export const q = () => \"package\";\n",
    );

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
        "import_order": { "static/app": ["pkg/util"] },
        "unassigned_mode": {
            "static/app": { "kind": "inline_in_entry" },
        },
        "materialize_logical_modules": {
            "prune_other_chunks": false,
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

    let probe_path = ws.out_root.join("__run_materialized_collision.mjs");
    write_text_file(
        &probe_path,
        "const { out } = await import(\"./app/static/app/entry.js\");\nconsole.log(out());\n",
    );
    assert_node_output(&probe_path, "app\n", "");
}

#[test]
fn partial_swap_references_rewritten_parity_across_materialized_and_passthrough_consumers() {
    // `references_rewritten` must count one shorthand value read in each
    // consumer file, while preserving the source-local property key. The
    // shadowed shorthand remains a local read and does not add to the count.
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "zod";
    const PACKAGE_VERSION: &str = "3.23.8";
    const SUBPATH: &str = "lib/index.mjs";

    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (
            MEGACHUNK_PATH,
            "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        ),
        (
            APP_PATH,
            "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\n\
         export const movedFlag = ({ zodBoolean }).zodBoolean() && kept() === 7;\n\
         export function stay() { return ({ zodBoolean }).zodBoolean(); }\n\
         export function shadow(zodBoolean) { return ({ zodBoolean }).zodBoolean(); }\n",
        ),
    ]);
    let package_root = ws.write_upstream_package(
        &format!("upstream/{PACKAGE_NAME}"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        "export const boolean = () => true;\n",
    );

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
        "import_order": { "static/app": ["flags"] },
        "unassigned_mode": {
            "static/app": { "kind": "inline_in_entry" },
        },
        "materialize_logical_modules": {
            "prune_other_chunks": false,
            "target_dir": "modules",
        },
        "write_js_tree": { "out_dir": &ws.out_root },
    });
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

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
        "const { stay, movedFlag, shadow } = await import(\"./app/static/app/entry.js\");\n\
         console.log(`${stay()}:${movedFlag}:${shadow(() => \"LOCAL\")}`);\n",
    );
    assert_node_output(&probe_path, "true:true:LOCAL\n", "");
}

#[test]
fn partial_swap_proto_shorthand_preserves_data_property_and_shadowing() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => \"VENDOR\";\n",
        caller_source: "import { e6 as __proto__ } from \"../megachunk/entry.js\";\n\
                        const value = { __proto__ };\n\
                        const originalPrototype = Object.getPrototypeOf(value);\n\
                        function shadow(__proto__) { return { __proto__ }; }\n\
                        const shadowed = shadow(() => \"LOCAL\");\n\
                        export function inspect() { return [\n\
                          Object.prototype.hasOwnProperty.call(value, \"__proto__\"),\n\
                          originalPrototype === Object.prototype,\n\
                          Object.getPrototypeOf(value) === originalPrototype,\n\
                          value.__proto__(),\n\
                          Object.prototype.hasOwnProperty.call(shadowed, \"__proto__\"),\n\
                          Object.getPrototypeOf(shadowed) === Object.prototype,\n\
                          shadowed.__proto__(),\n\
                        ].join(\"|\"); }\n",
        upstream_source: "export const boolean = () => \"UPSTREAM\";\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => \"UPSTREAM\";\n",
    );
    let probe_path = app_root.join("__run_proto_shorthand.mjs");
    write_text_file(
        &probe_path,
        "const { inspect } = await import(\"./static/app/entry.js\");\nconsole.log(inspect());\n",
    );
    assert_node_output(&probe_path, "true|true|true|UPSTREAM|true|true|LOCAL\n", "");
}

#[test]
fn partial_swap_namespace_kind_replaces_whole_import() {
    // Caller has `import { a as React } from "../megachunk/entry.js"`
    // where the chunk export `a` is the package's whole namespace
    // object. References use member access (`React.useState(...)`),
    // which must stay intact post-swap. Only the import statement
    // should change to `import * as React from "react"`.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: PartialSwapKind::Namespace,
        package_name: "react",
        package_version: "18.3.1",
        subpath: "index.js",
        chunk_source: "export const a = { useState: () => 1, useEffect: () => 2 };\nexport const keepMe = () => 7;\n",
        caller_source: "import { a as React, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return React.useState() + kept(); }\n",
        upstream_source: "export const useState = () => 1;\nexport const useEffect = () => 2;\n",
        chunk_export: "a",
        upstream_export: None,
    });

    assert_success(&fixture.result);
    assert_source_file(
        &fixture.caller_emitted_path,
        &[
            "import * as React from \"react\"",
            "React.useState()",
            "keepMe as kept",
        ],
        &[],
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
        kind: PartialSwapKind::Default,
        package_name: "clsx",
        package_version: "2.1.1",
        subpath: "dist/clsx.mjs",
        chunk_source: "export const aQ = (...args) => args.join(' ');\nexport const keepMe = () => 7;\n",
        caller_source: "import { aQ as z, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return z(\"a\", \"b\") + kept(); }\n",
        upstream_source: "export default (...args) => args.join(' ');\n",
        chunk_export: "aQ",
        upstream_export: None,
    });

    assert_success(&fixture.result);
    assert_source_file(
        &fixture.caller_emitted_path,
        &[
            "import z from \"clsx\"",
            "z(\"a\", \"b\")",
            "keepMe as kept",
        ],
        &[],
    );
}

#[test]
fn partial_swap_named_kind_auto_renames_local_binding() {
    // Caller has `import { o as mobxObserver } from "../megachunk/entry.js"`
    // where the chunk export `o` is a single named export of the package
    // (e.g. `mobx-react-lite#observer`). The local alias `mobxObserver`
    // is whatever the chunker emitted — the partial-swap rewrites both
    // the import (no alias) AND every value reference in the file to use the
    // upstream export name. Shorthand keeps its source-local key, and the
    // same spelling in a nested parameter remains a local binding.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: PartialSwapKind::Named,
        package_name: "mobx-react-lite",
        package_version: "4.0.7",
        subpath: "dist/index.js",
        chunk_source: "export const o = (Component) => Component;\nexport const keepMe = () => 7;\n",
        caller_source: "import { o as mobxObserver, keepMe as kept } from \"../megachunk/entry.js\";\n\
                         const shadowed = ((mobxObserver) => ({ mobxObserver }).mobxObserver(\"local\"))(value => `shadow:${value}`);\n\
                         export function go() { return `${({ mobxObserver }).mobxObserver(\"X\")}|${shadowed}|${kept()}`; }\n",
        upstream_source: "export const observer = (Component) => Component;\n",
        chunk_export: "o",
        upstream_export: Some("observer"),
    });

    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "mobx-react-lite",
        "4.0.7",
        "export const observer = value => value;\n",
    );
    let probe_path = app_root.join("__run_named_shorthand.mjs");
    write_text_file(
        &probe_path,
        "const { go } = await import(\"./static/app/entry.js\");\nconsole.log(go());\n",
    );
    assert_node_output(&probe_path, "X|shadow:local|7\n", "");
}

#[test]
fn partial_swap_local_exports_of_swapped_imports_stay_bound() {
    // A local export specifier names its binding outside any expression.
    // Member reads re-export the package's named export; a named-kind rename
    // exports the renamed import under the source's public name. Unrewritten,
    // the module exports an unbound name and fails to link. (`parse`, not a
    // reserved word like `boolean`, so the named kind does rename the local.)
    for kind in [PartialSwapKind::Member, PartialSwapKind::Named] {
        let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
            kind,
            package_name: "zod",
            package_version: "3.23.8",
            subpath: "lib/index.mjs",
            chunk_source: "export const e6 = () => \"VENDOR\";\nexport const keepMe = () => 7;\n",
            caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\n\
                             export { zodBoolean, zodBoolean as aliased };\n\
                             export function go() { return `${zodBoolean()}|${kept()}`; }\n",
            upstream_source: "export const parse = () => \"UPSTREAM\";\n",
            chunk_export: "e6",
            upstream_export: Some("parse"),
        });

        assert_success(&fixture.result);
        let app_root = emitted_app_root(&fixture);
        install_node_module(
            &app_root,
            "zod",
            "3.23.8",
            "export const parse = () => \"UPSTREAM\";\n",
        );
        let probe_path = app_root.join("__run_local_export.mjs");
        write_text_file(
            &probe_path,
            "const { go, zodBoolean, aliased } = await import(\"./static/app/entry.js\");\n\
             console.log(`${go()}|${zodBoolean()}|${aliased === zodBoolean}`);\n",
        );
        assert_node_output(&probe_path, "UPSTREAM|7|UPSTREAM|true\n", "");
    }
}

#[test]
fn partial_swap_rejects_local_export_of_a_dotted_member_read() {
    // `z.nested.boolean` is a property read with no live export form; left
    // in place, `export { zodBoolean }` would name an unbound local.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "export const e6 = () => true;\n",
        "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport { zodBoolean };\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const nested = { boolean: () => true };\n",
    );
    let vendor = partial_swap_vendor(
        "dotted member local export fixture",
        &[("zod", partial_package("3.23.8", "lib/index.mjs", Some("z")))],
        &[(
            "e6",
            swap_symbol("zod", PartialSwapKind::Member, Some("nested.boolean"), None),
        )],
    );
    let fixture = run_partial_swap_with_mark(ws, vendor, &[("zod", &package_root)]);
    assert_rejected(
        &fixture.result,
        &["static/app/entry.js", "zodBoolean", "e6"],
    );
}

#[test]
fn partial_swap_named_kind_no_rewrite_when_local_already_matches() {
    // When the caller-side local binding already matches the upstream
    // export name, the emitted import drops the `as` alias and no
    // identifier rewrite is needed.
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: PartialSwapKind::Named,
        package_name: "mobx-react-lite",
        package_version: "4.0.7",
        subpath: "dist/index.js",
        chunk_source: "export const o = (Component) => Component;\n",
        caller_source: "import { o as observer } from \"../megachunk/entry.js\";\nexport function go() { return observer(\"X\"); }\n",
        upstream_source: "export const observer = (Component) => Component;\n",
        chunk_export: "o",
        upstream_export: Some("observer"),
    });

    assert_success(&fixture.result);
    assert_source_file(
        &fixture.caller_emitted_path,
        &[
            "import { observer } from \"mobx-react-lite\"",
            "observer(\"X\")",
        ],
        &[],
    );
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

    assert_success(&fixture.result);

    // The top-level import-local use is rewritten to the facade member.
    // The shadowing parameter and its read must survive verbatim — the
    // inner `zodFlag()` is a call on the parameter, not the import local.
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

#[test]
fn partial_swap_split_uses_summary_of_swapped_export_body() {
    // The bundled `object` only allocates, so `wrapped` retains its callback
    // without running it and `later` may move to a module of its own.
    const PROVIDER: &str =
        "export function object(shape) { const kind = \"object\"; return { kind, shape }; }\n";
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        &format!("{PROVIDER}export const keep = () => 7;\n"),
        "import { object } from \"./megachunk.js\";\n\
         const wrapped = object({ read: () => later });\n\
         const later = 42;\n\
         console.log(wrapped.shape.read());\n",
        "fac",
        "1.0.0",
        "lib/index.mjs",
        PROVIDER,
    );
    let report_root = ws.out_root.join("reports").join("tree");
    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({"static/megachunk.js": partial_swap_vendor(
            "equivalent export fixture",
            &[("fac", partial_package("1.0.0", "lib/index.mjs", Some("fac")))],
            &[("object", swap_symbol("fac", PartialSwapKind::Member, Some("object"), None))],
        )}),
        Some(json!({
            "logical_modules": {
                "static/app": {
                    "callbacks/wrapped": {
                        "members": [{ "name": "wrapped", "selector": { "binding": { "name": "wrapped" } } }],
                    },
                    "values/later": {
                        "members": [{ "name": "later", "selector": { "binding": { "name": "later" } } }],
                    },
                },
            },
            "import_order": { "static/app": ["callbacks/wrapped", "values/later"] },
            "unassigned_mode": { "static/app": { "kind": "inline_in_entry" } },
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": &report_root,
            },
        })),
    );
    let result = ws.run_spec(&spec, &[("fac", &package_root)]);
    assert_success(&result);
    let modules: Value = read_json(&report_root.join("static/app/modules.json"));
    assert_eq!(
        modules["imported_callables"]["object"]["foreign_call"]["replacement_package"],
        "fac"
    );
    let app_root = ws.out_root.join("app");
    install_node_module(&app_root, "fac", "1.0.0", PROVIDER);
    assert_node_output(&app_root.join("static/app/entry.js"), "42\n", "");
}

// ─── partial-swap consumer soundness ────────────────────────────────────
//
// The per-symbol rewrite handles `ImportDecl` consumers. Two other consumer
// shapes can reference a partially-swapped chunk's export surface:
// `export { x } from "<chunk>"` re-exports and `import * as M from
// "<chunk>"` namespace imports. Re-exports of `named`/`default`/`namespace`
// kind symbols are rewritten against the upstream package; everything else
// (member-kind re-exports, namespace imports, `export *`) must make the
// pipeline bail (the plan-time consumer gate rejects them before any
// output) — the strip pass removes those names from the chunk's export
// surface, so an unrewritten consumer either link-fails (re-export)
// or silently reads `undefined` (namespace member).

#[test]
fn partial_swap_rewrites_import_peeled_into_coarse_residual() {
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "const a = value => `bundled:${value}`; export { a };\n",
        "import { a as U } from \"./megachunk.js\";\n\
         const residual = U(\"ok\");\n\
         export function App() { return residual; }\n",
        "fac",
        "1.0.0",
        "lib/index.mjs",
        "export default value => `package:${value}`;\n",
    );
    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({"static/megachunk.js": partial_swap_vendor(
            "coarse residual import fixture",
            &[("fac", partial_package("1.0.0", "lib/index.mjs", None))],
            &[("a", swap_symbol("fac", PartialSwapKind::Default, None, None))],
        )}),
        Some(json!({
            "logical_modules": {
                "static/app": {
                    "App": {
                        "members": [{ "name": "App", "selector": { "binding": { "name": "App" } } }],
                    },
                },
            },
            "import_order": {
                "static/app": ["__auto/residual_groups/0000", "App"],
            },
            "unassigned_mode": { "static/app": { "kind": "coarse_atomic_groups" } },
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": ws.out_root.join("reports/tree"),
            },
        })),
    );
    let result = ws.run_spec(&spec, &[("fac", &package_root)]);
    assert_success(&result);
    install_node_module(
        &ws.out_root.join("app"),
        "fac",
        "1.0.0",
        "export default value => `package:${value}`;\n",
    );
    let probe_path = ws.out_root.join("__run_coarse_residual.mjs");
    write_text_file(
        &probe_path,
        "const { App } = await import('./app/static/app/App.js');\nconsole.log(App());\n",
    );
    assert_node_output(&probe_path, "package:ok\n", "");
}

#[test]
fn partial_swap_rewrites_named_kind_reexport_from_consumer() {
    // `export { e6 as zodBoolean } from "<chunk>"` of a kind=named swapped
    // symbol must be rewritten to re-export the upstream package, exactly
    // like ImportDecl consumers — otherwise the strip pass removes `e6`
    // from the chunk's export surface while the re-export still references
    // it, guaranteeing a module-link failure in the emitted tree.
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "export const e6 = () => \"UPSTREAM\";\nexport const keepMe = () => 7;\n",
        "export { e6 as zodBoolean, keepMe } from \"../megachunk/entry.js\";\n",
        "zod",
        "3.23.8",
        "lib/index.mjs",
        "export const boolean = () => \"UPSTREAM\";\n",
    );
    let vendor = partial_swap_vendor(
        "named re-export consumer fixture",
        &[("zod", partial_package("3.23.8", "lib/index.mjs", None))],
        &[(
            "e6",
            swap_symbol("zod", PartialSwapKind::Named, Some("boolean"), None),
        )],
    );
    let fixture = run_partial_swap_with_mark(ws, vendor, &[("zod", &package_root)]);

    assert_success(&fixture.result);
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
        "export const aQ = (...args) => args.join(\"+\");\nexport const keepMe = () => 7;\n",
        "export { aQ as z, keepMe } from \"../megachunk/entry.js\";\n",
        "clsx",
        "2.1.1",
        "dist/clsx.mjs",
        "export default (...args) => args.join(\"+\");\n",
    );
    let vendor = partial_swap_vendor(
        "default re-export consumer fixture",
        &[("clsx", partial_package("2.1.1", "dist/clsx.mjs", None))],
        &[(
            "aQ",
            swap_symbol("clsx", PartialSwapKind::Default, None, None),
        )],
    );
    let fixture = run_partial_swap_with_mark(ws, vendor, &[("clsx", &package_root)]);

    assert_success(&fixture.result);
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
        "export const a = { useState: () => 1 };\nexport const keepMe = () => 7;\n",
        "export { a as React, keepMe } from \"../megachunk/entry.js\";\n",
        "react",
        "18.3.1",
        "index.js",
        "export const useState = () => 1;\n",
    );
    let vendor = partial_swap_vendor(
        "namespace re-export consumer fixture",
        &[("react", partial_package("18.3.1", "index.js", None))],
        &[(
            "a",
            swap_symbol("react", PartialSwapKind::Namespace, None, None),
        )],
    );
    let fixture = run_partial_swap_with_mark(ws, vendor, &[("react", &package_root)]);

    assert_success(&fixture.result);
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
fn partial_swap_rejects_consumers_that_would_lose_swapped_exports() {
    // Namespace reads would become undefined; member-kind re-exports have no
    // live namespace-member equivalent; export-star would silently lose names.
    for (label, vendor_source, consumer, diagnostics) in [
        (
            "vendor-partial-swap-namespace-consumer-",
            "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
            "import * as M from \"../megachunk/entry.js\";\nexport const r = M.e6();\n",
            ["namespace", "static/megachunk"],
        ),
        (
            "vendor-partial-swap-member-reexport-",
            "export const e6 = () => true;\n",
            "export { e6 as zodBoolean } from \"../megachunk/entry.js\";\n",
            ["e6", "re-export"],
        ),
        (
            "vendor-partial-swap-export-star-",
            "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
            "export * from \"../megachunk/entry.js\";\n",
            ["export *", "static/megachunk"],
        ),
    ] {
        let (ws, package_root) = setup_partial_swap_consumer_fixture(
            vendor_source,
            consumer,
            "zod",
            "3.23.8",
            "lib/index.mjs",
            "export const boolean = () => true;\n",
        );
        let vendor = partial_swap_vendor(
            label,
            &[("zod", partial_package("3.23.8", "lib/index.mjs", Some("z")))],
            &[(
                "e6",
                swap_symbol("zod", PartialSwapKind::Member, Some("boolean"), None),
            )],
        );
        let fixture = run_partial_swap_with_mark(ws, vendor, &[("zod", &package_root)]);
        assert_rejected_case(label, &fixture.result, &diagnostics);
    }
}
