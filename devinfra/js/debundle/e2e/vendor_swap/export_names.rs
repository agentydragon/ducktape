//! Generated wrappers and facades preserve reserved, string and dotted export names.

use super::*;

#[test]
fn vendor_namespaces_reject_reserved_binding_names() {
    let (ws, root) = setup_partial_swap_consumer_fixture(
        "export const a = 1;",
        "import { a } from '../megachunk/entry.js'; console.log(a);",
        "lib",
        "1.0.0",
        "index.js",
        "export const value = 1;",
    );
    let vendor = partial_swap_vendor(
        "reserved namespace",
        &[("lib", partial_package("1.0.0", "index.js", Some("class")))],
        &[(
            "a",
            swap_symbol("lib", PartialSwapKind::Member, Some("value"), None),
        )],
    );
    let fixture = run_partial_swap_with_mark(ws, vendor, &[("lib", &root)]);
    assert_rejected(&fixture.result, &["not a valid JS identifier"]);
}

#[test]
fn partial_swap_preserves_external_names_for_imports_and_reexports() {
    for upstream_export in ["class", "x-y", "path.value", "quote'and\"slash\\"] {
        in_case(upstream_export, || {
            let name = serde_json::to_string(upstream_export).unwrap();
            let upstream = format!("const value = 7; export {{ value as {name} }};");
            let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
                kind: PartialSwapKind::Named,
                package_name: "lib",
                package_version: "1.0.0",
                subpath: "index.js",
                chunk_source: "export const a = 7;",
                caller_source: "import { a as local } from '../megachunk/entry.js'; export { a as 'public-name' } from '../megachunk/entry.js'; console.log(local);",
                upstream_source: &upstream,
                chunk_export: "a",
                upstream_export: Some(upstream_export),
            });
            let app = fixture.install_emitted("lib", "1.0.0", &upstream);
            assert_probe_output(
                &app,
                "import * as m from './static/app/entry.js'; console.log(m['public-name']);",
                "7\n7\n",
            );
        });
    }
}

#[test]
fn partial_swap_preserves_residual_dependency_without_its_ast() {
    let ws = VendorTestWorkspace::new();
    ws.write_chunks(&[
        ("helper.js", "export const helper = () => 'helper';\n"),
        (
            "vendor.js",
            "import { helper } from './helper.js';\nexport const old = () => 'bundled';\nexport const residual = () => helper();\n",
        ),
        (
            "app.js",
            "import { old, residual } from './vendor.js';\nconsole.log(old() + ':' + residual());\n",
        ),
    ]);
    let upstream = "export const replacement = () => 'upstream';\n";
    let package = ws.write_upstream_package("upstream/lib", "lib", "1.0.0", "index.js", upstream);
    let vendor = partial_swap_vendor(
        "residual dependency",
        &[("lib", partial_package("1.0.0", "index.js", None))],
        &[(
            "old",
            swap_symbol("lib", PartialSwapKind::Named, Some("replacement"), None),
        )],
    );
    assert_success(&ws.run(json!({"vendor.js": vendor}), &[("lib", &package)]));
    let app = ws.out_root.join("app");
    install_node_module(&app, "lib", "1.0.0", upstream);
    assert_node_output(&app.join("app/entry.js"), "upstream:helper\n", "");
}

#[test]
fn partial_member_swap_preserves_dotted_upstream_paths() {
    let upstream = "export const convert = { nested: { 'read-value': () => 7 } };";
    let fixture = run_partial_swap_kind_fixture(PartialSwapKindFixtureArgs {
        kind: PartialSwapKind::Member,
        package_name: "lib",
        package_version: "1.0.0",
        subpath: "index.js",
        chunk_source: "export const a = () => 7;",
        caller_source: "import { a } from '../megachunk/entry.js'; console.log(a());",
        upstream_source: upstream,
        chunk_export: "a",
        upstream_export: Some("convert.nested.read-value"),
    });
    let app = fixture.install_emitted("lib", "1.0.0", upstream);
    assert_node_output(&app.join("static/app/entry.js"), "7\n", "");
}

#[test]
fn bundled_facades_preserve_default_reserved_and_string_export_names() {
    for bundle_export in ["default", "class", "x-y"] {
        in_case(bundle_export, || {
            let name = serde_json::to_string(bundle_export).unwrap();
            let bundle = format!("const value = {{ answer: 7 }}; export {{ value as {name} }};");
            let (ws, bundle_path) = setup_bundled_partial_swap(
                "lib.js",
                &bundle,
                &[
                    ("static/megachunk.js", "export const a = { answer: 7 };"),
                    (
                        "static/app.js",
                        "import { a } from '../megachunk/entry.js'; console.log(a.answer);",
                    ),
                ],
            );
            let package_root = ws.write_upstream_package(
                "upstream/lib",
                "lib",
                "1.0.0",
                "index.js",
                "exports.answer = 7;",
            );
            let vendor = bundled_vendor(
                "external facade name",
                &bundle_path,
                &[(
                    "lib",
                    bundled_package("1.0.0", "index.js", bundle_export, None),
                )],
                &[(
                    "a",
                    swap_symbol("lib", PartialSwapKind::Namespace, None, None),
                )],
            );
            let spec = ws.transform_spec(json!({"static/megachunk.js": vendor}));
            assert_success(&ws.run_spec(&spec, &[("lib", &package_root)]));
            assert_node_output(&ws.out_root.join("app/static/app/entry.js"), "7\n", "");
        });
    }
}

/// The bundle exports both a nested `convert.nested.read-value` member path
/// (7) and a literal export named `convert.nested.read-value` (11): member
/// kind must walk the path, named kind must import the literal name.
#[test]
fn bundled_swaps_resolve_dotted_upstream_exports_by_kind() {
    for (kind, expected) in [
        (PartialSwapKind::Member, "7\n"),
        (PartialSwapKind::Named, "11\n"),
    ] {
        in_case(&format!("{kind:?}"), || {
            let (ws, bundle_path) = setup_bundled_partial_swap(
                "lib.js",
                "export const Lib = { convert: { nested: { 'read-value': () => 7 } }, 'convert.nested.read-value': () => 11 };",
                &[
                    ("static/megachunk.js", "export const a = () => 7;"),
                    (
                        "static/app.js",
                        "import { a } from '../megachunk/entry.js'; console.log(a());",
                    ),
                ],
            );
            let package = ws.write_upstream_package(
                "upstream/lib",
                "lib",
                "1.0.0",
                "index.js",
                "export const convert = { nested: { 'read-value': () => 7 } }; const literal = () => 11; export { literal as 'convert.nested.read-value' };",
            );
            let vendor = bundled_vendor(
                "dotted member path",
                &bundle_path,
                &[(
                    "lib",
                    bundled_package("1.0.0", "index.js", "Lib", Some("Upstream")),
                )],
                &[(
                    "a",
                    swap_symbol("lib", kind, Some("convert.nested.read-value"), None),
                )],
            );
            let spec = ws.transform_spec(json!({"static/megachunk.js": vendor}));
            assert_success(&ws.run_spec(&spec, &[("lib", &package)]));
            assert_node_output(&ws.out_root.join("app/static/app/entry.js"), expected, "");
        });
    }
}
