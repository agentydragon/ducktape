//! Swapped chunks keep purity and re-entry proofs, and swaps cannot capture source globals.

use super::*;

struct VendorRun {
    result: CommandResult,
    _root: CaseWorkspace,
}

/// Three `callWrap(...)` calls in `app`, interleaved across two logical
/// modules, where `callWrap` comes from `adapter` and `adapter` delegates to the
/// pure `vendorlib::wrap`. `swap_adapter` fully swaps `adapter`.
fn run_interleaved_calls_through_adapter(swap_adapter: bool) -> VendorRun {
    const APP_PATH: &str = "static/app.js";
    const ADAPTER_PATH: &str = "static/adapter.js";
    const VENDORLIB_PATH: &str = "static/vendorlib.js";
    const PACKAGE_NAME: &str = "lib";
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (
            APP_PATH,
            "import { callWrap } from \"./adapter.js\";\n\
         const A = callWrap(() => \"a\");\n\
         const B = callWrap(() => \"b\");\n\
         const C = callWrap(() => \"c\");\n\
         console.log(A, B, C);\n\
         export { A, B, C };\n",
        ),
        (
            ADAPTER_PATH,
            "import { wrap } from \"./vendorlib.js\";\n\
         export function callWrap(f) { return wrap(f); }\n",
        ),
        (
            VENDORLIB_PATH,
            "export function wrap(f) { return { impl: f }; }\n",
        ),
    ]);
    let package_root = ws.write_upstream_package(
        "upstream",
        PACKAGE_NAME,
        "1.0.0",
        "dist/index.mjs",
        "export const callWrap = (f) => ({ impl: f });\n",
    );
    let vendor = if swap_adapter {
        json!({ADAPTER_PATH: {
            "level": "swap",
            "identity": "lib/dist/index.mjs",
            "package": PACKAGE_NAME,
            "version": "1.0.0",
            "subpath": "dist/index.mjs",
        }})
    } else {
        json!({})
    };
    let member = |name: &str| json!({"name": name, "selector": {"binding": {"name": name}}});
    let spec = build_bundled_partial_swap_spec(
        &ws,
        vendor,
        Some(json!({
            "logical_modules": {
                "static/app": {
                    "mod_a": {"members": [member("A"), member("C")]},
                    "mod_b": {"members": [member("B")]},
                },
            },
            "import_order": { "static/app": ["mod_a", "mod_b"] },
            "unassigned_mode": {"static/app": {"kind": "inline_in_entry"}},
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": ws.out_root.join("reports").join("tree"),
                "target_dir": "modules",
            },
        })),
    );
    VendorRun {
        result: ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)]),
        _root: ws.root,
    }
}

/// A full swap promises the package behaves like the bundled chunk, so the
/// cross-module purity oracle derives verdicts from the bundled body: the
/// interleaved calls through `adapter` are pure whether or not it is swapped.
#[test]
fn full_swapped_chunk_keeps_cross_module_purity() {
    assert_success(&run_interleaved_calls_through_adapter(false).result);
    assert_success(&run_interleaved_calls_through_adapter(true).result);
}

fn browser_bundled_reentry_fixture(bundle_source: &str, loader: &str) -> VendorSwapFixture {
    const PACKAGE: &str = "factory-kit";
    let (ws, bundle_path) = setup_bundled_partial_swap(
        "factory.js",
        bundle_source,
        &[
            (
                "static/vendor.js",
                "try { require('optional-loader'); } catch {}\nexport const factory = () => 'ready';\nexport const keep = () => {};\n",
            ),
            (
                "static/app.js",
                "import { factory, keep } from './vendor.js';\nconst prefix = (keep(), new globalThis.Date());\nconst value = factory();\nexport function read() { return value; }\n",
            ),
        ],
    );
    let package_root = ws.write_upstream_package(
        "upstream/factory-kit",
        PACKAGE,
        "1.0.0",
        "index.js",
        "export default () => 'ready';\n",
    );
    let member = |name: &str| json!({"name": name, "selector": {"binding": {"name": name}}});
    let spec = build_bundled_partial_swap_spec(
        &ws,
        json!({"static/vendor.js": bundled_vendor(
            "closed replacement factory",
            &bundle_path,
            &[(PACKAGE, bundled_package("1.0.0", "index.js", "factory", None))],
            &[("factory", swap_symbol(PACKAGE, PartialSwapKind::Default, None, None))],
        )}),
        Some(json!({
            "host_behavior": {"synchronous_module_loader": loader},
            "logical_modules": {"static/app": {
                "startup": {"members": [member("prefix")]},
                "values": {"members": [member("value"), member("read")]},
            }},
            "import_order": { "static/app": ["startup", "values"] },
            "unassigned_mode": {"static/app": {"kind": "inline_in_entry"}},
            "materialize_logical_modules": {
                "prune_other_chunks": false,
                "report_out_dir": ws.out_root.join("reports/tree"),
                "target_dir": "modules",
            },
        })),
    );
    let result = ws.run_spec(&spec, &[(PACKAGE, &package_root)]);
    VendorSwapFixture {
        result,
        chunk_path: "static/vendor.js".to_string(),
        wrapper_path: ws.out_root.join("app/static/app/entry.js"),
        manifest_path: ws.manifest_path,
        _root: ws.root,
    }
}

#[test]
fn closed_bundled_replacement_preserves_browser_export_proof() {
    let fixture =
        browser_bundled_reentry_fixture("export const factory = () => 'ready';\n", "none");
    let report: Value = read_json(
        &fixture
            ._root
            .path()
            .join("workspace/out/reports/tree/static/app/export_reentry.json"),
    );
    assert_eq!(report["policy"], "no_static_importer_reentry", "{report:#}");
    assert_success(&fixture.result);
    let probe = fixture.wrapper_path.with_file_name("probe.mjs");
    write_text_file(
        &probe,
        "import { read } from './entry.js'; console.log(read());\n",
    );
    assert_node_output(&probe, "ready\n", "");
}

#[test]
fn bundled_reentry_proof_requires_closed_bundle() {
    for (bundle, loader) in [
        (
            "import './app.js'; export const factory = () => 'ready';\n",
            "none",
        ),
        (
            "await Promise.resolve(); export const factory = () => 'ready';\n",
            "none",
        ),
    ] {
        let fixture = browser_bundled_reentry_fixture(bundle, loader);
        assert_rejected(&fixture.result, &["atomic-group conflict"]);
    }
}

#[test]
fn named_swap_cannot_capture_a_source_global() {
    const PACKAGE_NAME: &str = "mobx-react-lite";
    const VERSION: &str = "4.0.7";
    const SUBPATH: &str = "dist/index.js";
    const UPSTREAM: &str = "export const observer = (value) => value;\n";
    for (materialized, with_reports, dry_run) in [
        (false, false, false),
        (false, true, false),
        (false, false, true),
        (true, false, false),
        (true, false, true),
    ] {
        let caller = if materialized {
            "import { o as wrap } from \"./megachunk.js\";\n\
             export function read() { return typeof observer + \":\" + wrap(\"X\"); }\n\
             console.log(read());\n"
        } else {
            "import { o as wrap } from \"./megachunk.js\";\n\
             console.log(typeof observer + \":\" + wrap(\"X\"));\n"
        };
        let (ws, package_root) = setup_partial_swap_consumer_fixture(
            "export const o = (value) => value;\n",
            caller,
            PACKAGE_NAME,
            VERSION,
            SUBPATH,
            UPSTREAM,
        );
        write_text_file(
            &ws.snapshot_root.join("package.json"),
            "{\"type\":\"module\"}\n",
        );
        assert_node_output(&ws.snapshot_root.join("static/app.js"), "undefined:X\n", "");

        let vendor = partial_swap_vendor(
            "megachunk named swap capturing a global",
            &[(PACKAGE_NAME, partial_package(VERSION, SUBPATH, None))],
            &[(
                "o",
                swap_symbol(PACKAGE_NAME, PartialSwapKind::Named, Some("observer"), None),
            )],
        );
        let mut spec = ws.transform_spec(json!({ "static/megachunk.js": vendor }));
        if materialized {
            spec["logical_modules"] = json!({
                "static/app": {"read": {"members": [
                    {"name": "read", "selector": {"binding": {"name": "read"}}}
                ]}}
            });
            spec["import_order"] = json!({ "static/app": ["read"] });
            spec["unassigned_mode"] = json!({"static/app": {"kind": "inline_in_entry"}});
            spec["materialize_logical_modules"] = json!({"prune_other_chunks": false});
        }
        if with_reports {
            spec["materialize_logical_modules"] =
                json!({ "report_out_dir": ws.out_root.join("reports/tree") });
        }
        let extra_args: &[&str] = if dry_run { &["--dry-run"] } else { &[] };
        let result = ws.run_spec_with_args(&spec, &[(PACKAGE_NAME, &package_root)], extra_args);
        let case =
            format!("materialized={materialized}, reports={with_reports}, dry_run={dry_run}");
        assert_rejected_case(&case, &result, &["source global `observer` captured"]);
        assert!(!ws.out_root.join("app/static/app/entry.js").exists());
    }
}

#[test]
fn partial_swap_drops_primitive_array_iteration_without_invoking_typeof_operand() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\nfunction dormant() { registry.hit = true; }\nconst entries = [typeof dormant];\nconst all = entries.map(entry => entry);\nexport const e6 = () => all.length > 0;\nexport const keepMe = () => Object.keys(registry).length;\n",
    );
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true 0\n", "");
}
