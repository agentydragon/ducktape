//! Swapped chunks keep purity and re-entry proofs, and swaps cannot capture source globals.

use super::*;

/// Three `callWrap(...)` calls in `app`, interleaved across two logical
/// modules, where `callWrap` comes from `adapter` and `adapter` delegates to the
/// pure `vendorlib::wrap`. `swap_adapter` fully swaps `adapter`.
fn run_interleaved_calls_through_adapter(swap_adapter: bool) -> CommandResult {
    const ADAPTER_PATH: &str = "static/adapter.js";
    const PACKAGE_NAME: &str = "lib";
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        (
            "static/app.js",
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
            "static/vendorlib.js",
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
    let spec = ws.transform_spec_with(
        vendor,
        materialize_app_modules(
            &[("mod_a", &["A", "C"]), ("mod_b", &["B"])],
            json!({
                "prune_other_chunks": false,
                "report_out_dir": ws.out_root.join("reports").join("tree"),
                "target_dir": "modules",
            }),
        ),
    );
    ws.run_spec(&spec, &[(PACKAGE_NAME, &package_root)])
}

/// A full swap promises the package behaves like the bundled chunk, so the
/// cross-module purity oracle derives verdicts from the bundled body: the
/// interleaved calls through `adapter` are pure whether or not it is swapped.
#[test]
fn full_swapped_chunk_keeps_cross_module_purity() {
    assert_success(&run_interleaved_calls_through_adapter(false));
    assert_success(&run_interleaved_calls_through_adapter(true));
}

struct ReentryFixture {
    result: CommandResult,
    ws: VendorTestWorkspace,
}

fn browser_bundled_reentry_fixture(bundle_source: &str, loader: &str) -> ReentryFixture {
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
    let mut extra = materialize_app_modules(
        &[("startup", &["prefix"]), ("values", &["value", "read"])],
        json!({
            "prune_other_chunks": false,
            "report_out_dir": ws.out_root.join("reports/tree"),
            "target_dir": "modules",
        }),
    );
    extra["host_behavior"] = json!({"synchronous_module_loader": loader});
    let spec = ws.transform_spec_with(
        json!({"static/vendor.js": bundled_vendor(
            "closed replacement factory",
            &bundle_path,
            &[(PACKAGE, bundled_package("1.0.0", "index.js", "factory", None))],
            &[("factory", swap_symbol(PACKAGE, PartialSwapKind::Default, None, None))],
        )}),
        extra,
    );
    let result = ws.run_spec(&spec, &[(PACKAGE, &package_root)]);
    ReentryFixture { result, ws }
}

#[test]
fn closed_bundled_replacement_preserves_browser_export_proof() {
    let fixture =
        browser_bundled_reentry_fixture("export const factory = () => 'ready';\n", "none");
    let report: Value = read_json(
        &fixture
            .ws
            .out_root
            .join("reports/tree/static/app/export_reentry.json"),
    );
    assert_eq!(report["policy"], "no_static_importer_reentry", "{report:#}");
    assert_success(&fixture.result);
    assert_probe_output(
        &fixture.ws.out_root.join("app/static/app"),
        "import { read } from './entry.js'; console.log(read());\n",
        "ready\n",
    );
}

/// Anchors for the closed-bundle proof above: a bundle that imports the app
/// or awaits at top level could re-enter, so the split must be refused.
#[test]
fn bundled_reentry_proof_requires_closed_bundle() {
    for bundle in [
        "import './app.js'; export const factory = () => 'ready';\n",
        "await Promise.resolve(); export const factory = () => 'ready';\n",
    ] {
        in_case(bundle, || {
            let fixture = browser_bundled_reentry_fixture(bundle, "none");
            assert_rejected(&fixture.result, &["atomic-group conflict"]);
        });
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
        let case =
            format!("materialized={materialized}, reports={with_reports}, dry_run={dry_run}");
        in_case(&case, || {
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

            let vendor = json!({ "static/megachunk.js": partial_swap_vendor(
                "megachunk named swap capturing a global",
                &[(PACKAGE_NAME, partial_package(VERSION, SUBPATH, None))],
                &[(
                    "o",
                    swap_symbol(PACKAGE_NAME, PartialSwapKind::Named, Some("observer"), None),
                )],
            ) });
            let mut spec = if materialized {
                ws.transform_spec_with(
                    vendor,
                    materialize_app_modules(
                        &[("read", &["read"])],
                        json!({"prune_other_chunks": false}),
                    ),
                )
            } else {
                ws.transform_spec(vendor)
            };
            if with_reports {
                spec["materialize_logical_modules"] =
                    json!({ "report_out_dir": ws.out_root.join("reports/tree") });
            }
            let extra_args: &[&str] = if dry_run { &["--dry-run"] } else { &[] };
            let result = ws.run_spec_with_args(&spec, &[(PACKAGE_NAME, &package_root)], extra_args);
            assert_rejected(&result, &["source global `observer` captured"]);
            assert!(!ws.out_root.join("app/static/app/entry.js").exists());
        });
    }
}
