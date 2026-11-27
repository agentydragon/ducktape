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
            "static/app.js",
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
    let vendor = partial_swap_vendor(
        "materialized sibling collision fixture",
        &[(
            PACKAGE_NAME,
            partial_package(PACKAGE_VERSION, SUBPATH, Some("z")),
        )],
        &[(
            "q",
            swap_symbol(PACKAGE_NAME, PartialSwapKind::Member, Some("q"), None),
        )],
    );
    let spec = ws.transform_spec_with(
        json!({ VENDOR_PATH: vendor }),
        materialize_app_modules(
            &[("pkg/util", &["q"])],
            json!({"prune_other_chunks": false}),
        ),
    );
    assert_success(&ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]));
    assert_probe_output(
        &ws.out_root,
        "const { out } = await import(\"./app/static/app/entry.js\");\nconsole.log(out());\n",
        "app\n",
    );
}

#[test]
fn partial_swap_references_rewritten_parity_across_materialized_and_passthrough_consumers() {
    // `references_rewritten` must count one shorthand value read in each
    // consumer file, while preserving the source-local property key. The
    // shadowed shorthand remains a local read and does not add to the count.
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const PACKAGE_NAME: &str = "zod";
    const PACKAGE_VERSION: &str = "3.23.8";
    const SUBPATH: &str = "lib/index.mjs";

    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\n\
         export const movedFlag = ({ zodBoolean }).zodBoolean() && kept() === 7;\n\
         export function stay() { return ({ zodBoolean }).zodBoolean(); }\n\
         export function shadow(zodBoolean) { return ({ zodBoolean }).zodBoolean(); }\n",
        PACKAGE_NAME,
        PACKAGE_VERSION,
        SUBPATH,
        ZOD_TRUE,
    );
    let vendor = partial_swap_vendor(
        "references_rewritten parity fixture",
        &[(
            PACKAGE_NAME,
            partial_package(PACKAGE_VERSION, SUBPATH, Some("z")),
        )],
        &[(
            "e6",
            swap_symbol(PACKAGE_NAME, PartialSwapKind::Member, Some("boolean"), None),
        )],
    );
    let spec = ws.transform_spec_with(
        json!({ MEGACHUNK_PATH: vendor }),
        materialize_app_modules(
            &[("flags", &["movedFlag"])],
            json!({"prune_other_chunks": false, "target_dir": "modules"}),
        ),
    );
    assert_success(&ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]));

    let manifest: Value = read_json(&ws.manifest_path);
    assert_eq!(
        manifest["partial"][MEGACHUNK_PATH]["symbols"]["e6"]["references_rewritten"].as_u64(),
        Some(2),
        "one rewrite per file class (materialized module + residual entry):\n{manifest:#}",
    );

    install_node_module(
        &ws.out_root.join("app"),
        PACKAGE_NAME,
        PACKAGE_VERSION,
        ZOD_TRUE,
    );
    assert_probe_output(
        &ws.out_root,
        "const { stay, movedFlag, shadow } = await import(\"./app/static/app/entry.js\");\n\
         console.log(`${stay()}:${movedFlag}:${shadow(() => \"LOCAL\")}`);\n",
        "true:true:LOCAL\n",
    );
}

#[test]
fn partial_swap_proto_shorthand_preserves_data_property_and_shadowing() {
    const UPSTREAM: &str = "export const boolean = () => \"UPSTREAM\";\n";
    let fixture = zod_swap(
        "export const e6 = () => \"VENDOR\";\n",
        "import { e6 as __proto__ } from \"../megachunk/entry.js\";\n\
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
        UPSTREAM,
    );
    let app_root = fixture.install_emitted("zod", "3.23.8", UPSTREAM);
    assert_probe_output(
        &app_root,
        "const { inspect } = await import(\"./static/app/entry.js\");\nconsole.log(inspect());\n",
        "true|true|true|UPSTREAM|true|true|LOCAL\n",
    );
}

#[test]
fn partial_swap_does_not_rewrite_shadowing_inner_binding() {
    // The identifier rewriter must be hygiene-aware: a nested parameter
    // named like the import local (`zodFlag`) shadows it. Keying the
    // rewrite on the textual symbol would miscompile the inner read into
    // `z.boolean()`; `fromParam` must reflect the argument passed to `pick`.
    const UPSTREAM: &str = "export const boolean = () => \"UPSTREAM\";\n";
    let fixture = zod_swap(
        "export const e6 = () => \"UPSTREAM\";\nexport const keepMe = () => 7;\n",
        "import { e6 as zodFlag, keepMe as kept } from \"../megachunk/entry.js\";\n\
         function pick(zodFlag) { return zodFlag(); }\n\
         export const fromImport = zodFlag();\n\
         export const fromParam = pick(() => \"PARAM\");\n\
         export const keepUse = kept();\n",
        UPSTREAM,
    );
    let app_root = fixture.install_emitted("zod", "3.23.8", UPSTREAM);
    assert_probe_output(
        &app_root,
        "const m = await import(\"./static/app/entry.js\");\n\
         console.log(`${m.fromImport}:${m.fromParam}`);\n",
        "UPSTREAM:PARAM\n",
    );
}

#[test]
fn partial_swap_whole_import_kinds_rewrite_caller_import() {
    for (name, args, present) in [
        // The chunk export is the package namespace; member reads stay and
        // only the import becomes `import * as React from "react"`.
        (
            "namespace",
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Namespace,
                package_name: "react",
                package_version: "18.3.1",
                subpath: "index.js",
                chunk_source: "export const a = { useState: () => 1, useEffect: () => 2 };\nexport const keepMe = () => 7;\n",
                caller_source: "import { a as React, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return React.useState() + kept(); }\n",
                upstream_source: "export const useState = () => 1;\nexport const useEffect = () => 2;\n",
                chunk_export: "a",
                upstream_export: None,
            },
            &[
                "import * as React from \"react\"",
                "React.useState()",
                "keepMe as kept",
            ][..],
        ),
        // The chunk export is the package default (e.g. clsx); direct calls
        // stay and only the import becomes `import z from "clsx"`.
        (
            "default",
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Default,
                package_name: "clsx",
                package_version: "2.1.1",
                subpath: "dist/clsx.mjs",
                chunk_source: "export const aQ = (...args) => args.join(' ');\nexport const keepMe = () => 7;\n",
                caller_source: "import { aQ as z, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return z(\"a\", \"b\") + kept(); }\n",
                upstream_source: "export default (...args) => args.join(' ');\n",
                chunk_export: "aQ",
                upstream_export: None,
            },
            &[
                "import z from \"clsx\"",
                "z(\"a\", \"b\")",
                "keepMe as kept",
            ],
        ),
        // A local already named like the upstream export needs no alias.
        (
            "named, local already matches",
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Named,
                package_name: "mobx-react-lite",
                package_version: "4.0.7",
                subpath: "dist/index.js",
                chunk_source: "export const o = (Component) => Component;\n",
                caller_source: "import { o as observer } from \"../megachunk/entry.js\";\nexport function go() { return observer(\"X\"); }\n",
                upstream_source: "export const observer = (Component) => Component;\n",
                chunk_export: "o",
                upstream_export: Some("observer"),
            },
            &[
                "import { observer } from \"mobx-react-lite\"",
                "observer(\"X\")",
            ],
        ),
    ] {
        in_case(name, || {
            let fixture = run_partial_swap_kind_fixture(args);
            assert_success(&fixture.result);
            assert_source_file(&fixture.caller_emitted_path, present, &[]);
        });
    }
}

#[test]
fn partial_swap_named_kind_auto_renames_local_binding() {
    // `import { o as mobxObserver }` of a single named package export: the
    // import loses its alias and every value reference uses the upstream
    // name. Shorthand keeps its source-local key, and the same spelling in
    // a nested parameter remains a local binding.
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
    let app_root = fixture.install_emitted(
        "mobx-react-lite",
        "4.0.7",
        "export const observer = value => value;\n",
    );
    assert_probe_output(
        &app_root,
        "const { go } = await import(\"./static/app/entry.js\");\nconsole.log(go());\n",
        "X|shadow:local|7\n",
    );
}

#[test]
fn partial_swap_local_exports_of_swapped_imports_stay_bound() {
    // A local export specifier names its binding outside any expression.
    // Member reads re-export the package's named export; a named-kind rename
    // exports the renamed import under the source's public name. Unrewritten,
    // the module exports an unbound name and fails to link. (`parse`, not a
    // reserved word like `boolean`, so the named kind does rename the local.)
    const UPSTREAM: &str = "export const parse = () => \"UPSTREAM\";\n";
    for kind in [PartialSwapKind::Member, PartialSwapKind::Named] {
        in_case(&format!("{kind:?}"), || {
            let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
                kind,
                package_name: "zod",
                package_version: "3.23.8",
                subpath: "lib/index.mjs",
                chunk_source: "export const e6 = () => \"VENDOR\";\nexport const keepMe = () => 7;\n",
                caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\n\
                                 export { zodBoolean, zodBoolean as aliased };\n\
                                 export function go() { return `${zodBoolean()}|${kept()}`; }\n",
                upstream_source: UPSTREAM,
                chunk_export: "e6",
                upstream_export: Some("parse"),
            });
            let app_root = fixture.install_emitted("zod", "3.23.8", UPSTREAM);
            assert_probe_output(
                &app_root,
                "const { go, zodBoolean, aliased } = await import(\"./static/app/entry.js\");\n\
                 console.log(`${go()}|${zodBoolean()}|${aliased === zodBoolean}`);\n",
                "UPSTREAM|7|UPSTREAM|true\n",
            );
        });
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

// ─── partial-swap consumer soundness ────────────────────────────────────
//
// Besides `ImportDecl`, `export { x } from "<chunk>"` re-exports and
// `import * as M from "<chunk>"` namespace imports reach the chunk's export
// surface. Re-exports of `named`/`default`/`namespace` symbols are rewritten
// against the upstream package; member-kind re-exports, namespace imports
// and `export *` are rejected at plan time, since the strip pass removes
// those names and the consumer would link-fail or read `undefined`.

#[test]
fn partial_swap_rewrites_reexports_from_consumer() {
    for (args, log, expected) in [
        (
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Named,
                package_name: "zod",
                package_version: "3.23.8",
                subpath: "lib/index.mjs",
                chunk_source: "export const e6 = () => \"UPSTREAM\";\nexport const keepMe = () => 7;\n",
                caller_source: "export { e6 as zodBoolean, keepMe } from \"../megachunk/entry.js\";\n",
                upstream_source: "export const boolean = () => \"UPSTREAM\";\n",
                chunk_export: "e6",
                upstream_export: Some("boolean"),
            },
            "`${m.zodBoolean()}:${m.keepMe()}`",
            "UPSTREAM:7\n",
        ),
        (
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Default,
                package_name: "clsx",
                package_version: "2.1.1",
                subpath: "dist/clsx.mjs",
                chunk_source: "export const aQ = (...args) => args.join(\"+\");\nexport const keepMe = () => 7;\n",
                caller_source: "export { aQ as z, keepMe } from \"../megachunk/entry.js\";\n",
                upstream_source: "export default (...args) => args.join(\"+\");\n",
                chunk_export: "aQ",
                upstream_export: None,
            },
            "m.z(\"a\", \"b\")",
            "a+b\n",
        ),
        (
            PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Namespace,
                package_name: "react",
                package_version: "18.3.1",
                subpath: "index.js",
                chunk_source: "export const a = { useState: () => 1 };\nexport const keepMe = () => 7;\n",
                caller_source: "export { a as React, keepMe } from \"../megachunk/entry.js\";\n",
                upstream_source: "export const useState = () => 1;\n",
                chunk_export: "a",
                upstream_export: None,
            },
            "m.React.useState()",
            "1\n",
        ),
    ] {
        in_case(&format!("{:?}", args.kind), || {
            let (name, version, source) = (
                args.package_name,
                args.package_version,
                args.upstream_source,
            );
            let fixture = run_partial_swap_kind_fixture(args);
            let app_root = fixture.install_emitted(name, version, source);
            assert_probe_output(
                &app_root,
                &format!(
                    "const m = await import(\"./static/app/entry.js\");\nconsole.log({log});\n"
                ),
                expected,
            );
        });
    }
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
        in_case(label, || {
            let fixture = zod_swap(vendor_source, consumer, ZOD_TRUE);
            assert_rejected(&fixture.result, &diagnostics);
        });
    }
}

// ─── analysis of swapped implementations ────────────────────────────────
//
// A swap promises the replacement behaves like the bundled implementation
// (SPEC.md § Vendor swap equivalence), so facts derived from the bundled
// body justify splits around calls that run the replacement.

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
    let spec = ws.transform_spec_with(
        json!({"static/megachunk.js": partial_swap_vendor(
            "equivalent export fixture",
            &[("fac", partial_package("1.0.0", "lib/index.mjs", Some("fac")))],
            &[("object", swap_symbol("fac", PartialSwapKind::Member, Some("object"), None))],
        )}),
        materialize_app_modules(
            &[
                ("callbacks/wrapped", &["wrapped"]),
                ("values/later", &["later"]),
            ],
            json!({"prune_other_chunks": false, "report_out_dir": &report_root}),
        ),
    );
    assert_success(&ws.run_spec(&spec, &[("fac", &package_root)]));
    let modules: Value = read_json(&report_root.join("static/app/modules.json"));
    assert_eq!(
        modules["imported_callables"]["object"]["foreign_call"]["replacement_package"],
        "fac"
    );
    let app_root = ws.out_root.join("app");
    install_node_module(&app_root, "fac", "1.0.0", PROVIDER);
    assert_node_output(&app_root.join("static/app/entry.js"), "42\n", "");
}

#[test]
fn partial_swap_rewrites_import_peeled_into_coarse_residual() {
    const UPSTREAM: &str = "export default value => `package:${value}`;\n";
    let (ws, package_root) = setup_partial_swap_consumer_fixture(
        "const a = value => `bundled:${value}`; export { a };\n",
        "import { a as U } from \"./megachunk.js\";\n\
         const residual = U(\"ok\");\n\
         export function App() { return residual; }\n",
        "fac",
        "1.0.0",
        "lib/index.mjs",
        UPSTREAM,
    );
    let spec = ws.transform_spec_with(
        json!({"static/megachunk.js": partial_swap_vendor(
            "coarse residual import fixture",
            &[("fac", partial_package("1.0.0", "lib/index.mjs", None))],
            &[("a", swap_symbol("fac", PartialSwapKind::Default, None, None))],
        )}),
        json!({
            "logical_modules": { "static/app": { "App": { "members": [member("App")] } } },
            "import_order": {
                "static/app": ["__auto/residual_groups/0000", "App"],
            },
            "unassigned_mode": { "static/app": { "kind": "coarse_atomic_groups" } },
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": ws.out_root.join("reports/tree"),
            },
        }),
    );
    assert_success(&ws.run_spec(&spec, &[("fac", &package_root)]));
    install_node_module(&ws.out_root.join("app"), "fac", "1.0.0", UPSTREAM);
    assert_probe_output(
        &ws.out_root,
        "const { App } = await import('./app/static/app/App.js');\nconsole.log(App());\n",
        "package:ok\n",
    );
}
