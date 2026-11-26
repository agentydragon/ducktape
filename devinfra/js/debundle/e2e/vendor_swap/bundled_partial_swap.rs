//! Bundled partial swaps replace a package family with one ESM facade.

use super::*;

#[test]
fn bundled_partial_swap_replaces_react_cjs_family_with_singleton_esm_facade() {
    // React-family swaps need more than plain partial-swap. The browser
    // cannot load React's npm CJS entry directly as an importmapped ESM file,
    // and React + jsx-runtime must share one module singleton. This fixture is
    // the smallest contract for that shape: a namespace import for `react`, a
    // namespace import for `react/jsx-runtime`, and a shared internal cell
    // that proves both aliases came from one bundled package family rather
    // than from a residual in-blob copy plus raw CJS.
    // The Node probe below is the minimal gate; a browser importmap/load test would
    // be a stronger browser-level proof.
    const MEGACHUNK_PATH: &str = "static/megachunk.js";
    const CALLER_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "react";
    const JSX_RUNTIME_NAME: &str = "react/jsx-runtime";
    const PACKAGE_VERSION: &str = "18.3.1";

    let (ws, bundle_path) = setup_bundled_partial_swap(
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
        json!({MEGACHUNK_PATH: bundled_vendor("React CJS family bundled partial swap fixture", &bundle_path, &[(PACKAGE_NAME, bundled_package(PACKAGE_VERSION, "index.js", "React", None)), (JSX_RUNTIME_NAME, bundled_package(PACKAGE_VERSION, "jsx-runtime.js", "jsxRuntime", None))], &[("a", swap_symbol(PACKAGE_NAME, PartialSwapKind::Namespace, None, None)), ("j", swap_symbol(JSX_RUNTIME_NAME, PartialSwapKind::Namespace, None, None))])}),
        None,
    );
    let result = ws.run_spec(
        &spec,
        &[
            (PACKAGE_NAME, &package_root),
            (JSX_RUNTIME_NAME, &package_root),
        ],
    );
    assert_success(&result);

    let probe_path = ws.out_root.join("__run_entry.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "package:package\n", "");
}

#[test]
fn bundled_partial_swap_runtime_cannot_mix_swapped_client_with_residual_singleton_user() {
    // Runtime regression for React-like singleton package families. Swapping a renderer
    // facade while leaving a component in the residual vendor chunk can mix two
    // dispatcher singletons: the swapped renderer initializes the package copy,
    // but the residual component still reads the in-blob copy. The emitted app
    // must run as `package`, rather than throwing `residual dispatcher is null`
    // as it did in the original React-like mixed-copy regression.
    // TODO: add a browser/importmap load probe alongside this Node check once
    // the e2e harness has a browser runner.
    const VENDOR_PATH: &str = "static/vendor.js";
    const APP_PATH: &str = "static/app.js";
    const PACKAGE_NAME: &str = "singleton-kit";
    const CLIENT_NAME: &str = "singleton-kit/client";
    const PACKAGE_VERSION: &str = "1.0.0";

    let (ws, bundle_path) = setup_bundled_partial_swap(
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
        json!({VENDOR_PATH: bundled_vendor("singleton runtime mixed-copy fixture", &bundle_path, &[(PACKAGE_NAME, bundled_package(PACKAGE_VERSION, "index.js", "Hooks", None)), (CLIENT_NAME, bundled_package(PACKAGE_VERSION, "client.js", "Client", None))], &[("a", swap_symbol(PACKAGE_NAME, PartialSwapKind::Namespace, None, None)), ("h", swap_symbol(CLIENT_NAME, PartialSwapKind::Namespace, None, None))])}),
        None,
    );
    let result = ws.run_spec(
        &spec,
        &[(PACKAGE_NAME, &package_root), (CLIENT_NAME, &package_root)],
    );
    assert_success(&result);

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
        json!({VENDOR_PATH: bundled_vendor("materialized import bundled partial swap fixture", &bundle_path, &[(PACKAGE_NAME, bundled_package(PACKAGE_VERSION, "observer.js", "observe", None))], &[("o", swap_symbol(PACKAGE_NAME, PartialSwapKind::Default, None, None))])}),
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
            "import_order": { "static/app": ["helpers/observer"] },
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
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

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
        json!({VENDOR_PATH: bundled_vendor("materialized named alias bundled partial swap fixture", &bundle_path, &[(PACKAGE_NAME, bundled_package(PACKAGE_VERSION, "index.js", "Toolkit", Some("Toolkit")))], &[("m", swap_symbol(PACKAGE_NAME, PartialSwapKind::Named, Some("map"), None)), ("k", swap_symbol(PACKAGE_NAME, PartialSwapKind::Named, Some("make"), None))])}),
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
            "import_order": { "static/app": ["helpers/toolkit"] },
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
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

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
        "zod.esbuilt.js",
        "const Zod = { instanceof: Ctor => `package:${Ctor.name}` };\n\
         export { Zod };\n",
        &[
            (
                VENDOR_PATH,
                "function nY(Ctor) { return `vendor:${Ctor.name}`; }\n\
                 const shorthand = { nY };\n\
                 const schema = shorthand.nY(URL);\n\
                 const shadowed = ((nY) => ({ nY }).nY(URL))(Ctor => `shadow:${Ctor.name}`);\n\
                 export const keep = `${schema}|${shadowed}`;\n",
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
        json!({VENDOR_PATH: bundled_vendor("local helper bundled partial swap fixture", &bundle_path, &[(PACKAGE_NAME, bundled_package(PACKAGE_VERSION, "index.js", "Zod", Some("Zod")))], &[("zodInstanceof", swap_symbol(PACKAGE_NAME, PartialSwapKind::Named, Some("instanceof"), Some("nY")))])}),
        None,
    );
    let result = ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]);
    assert_success(&result);

    let probe_path = ws.out_root.join("__run_local_helper.mjs");
    write_text_file(
        &probe_path,
        "await import(\"./app/static/app/entry.js\");\n",
    );
    assert_node_output(&probe_path, "package:URL|shadow:URL\n", "");
}
