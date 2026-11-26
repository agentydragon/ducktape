//! Generated wrappers and facades preserve reserved, string and dotted export names.

use super::*;

#[test]
fn wrappers_preserve_reserved_and_string_export_names() {
    for (shape, upstream) in [
        (
            WrapperShape::NamedFromDefault,
            "export default { class: 1, 'x-y': 2 };",
        ),
        (
            WrapperShape::NamedFromJsonDefault,
            "{\"class\":1,\"x-y\":2}",
        ),
    ] {
        let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
            chunk_source: "const a = 0; export { a as class, a as 'x-y' };",
            wrapper_shape: Some(shape),
            upstream_source: upstream,
            default_export_aliases: &[],
        });
        assert_wrapper_output(
            &fixture,
            "import * as m from './entry.js'; console.log(m.class, m['x-y']);",
            "1 2\n",
        );
    }
}

#[test]
fn json_wrapper_preserves_proto_as_an_own_data_property() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "export const value = 0;",
        wrapper_shape: Some(WrapperShape::NamedFromJsonDefault),
        upstream_source: "{\"value\":1,\"__proto__\":{\"nested\":2}}",
        default_export_aliases: &[],
    });
    assert_wrapper_output(
        &fixture,
        "import data from './entry.js'; console.log(Object.hasOwn(data, '__proto__'), Object.getPrototypeOf(data) === Object.prototype);",
        "true true\n",
    );
}

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
fn module_default_wrapper_aliases_reserved_string_and_colliding_names() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "const x = {}; export { x as class, x as 'x-y', x as existing };",
        wrapper_shape: Some(WrapperShape::NamedFromModuleDefault),
        upstream_source: "const existing = { value: 7 }; export default existing;",
        default_export_aliases: &["class", "x-y", "existing"],
    });
    assert_wrapper_output(
        &fixture,
        "import d, * as m from './entry.js'; console.log(m.class === d, m['x-y'] === d, m.existing.value);",
        "true true 7\n",
    );
}

#[test]
fn partial_swap_preserves_external_names_for_imports_and_reexports() {
    for upstream_export in ["class", "x-y", "path.value", "quote'and\"slash\\"] {
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
        assert_success(&fixture.result);
        let app = emitted_app_root(&fixture);
        install_node_module(&app, "lib", "1.0.0", &upstream);
        let probe = app.join("probe.mjs");
        write_text_file(
            &probe,
            "import * as m from './static/app/entry.js'; console.log(m['public-name']);",
        );
        assert_node_output(&probe, "7\n7\n", "");
    }
}

#[test]
fn json_wrapper_export_names_do_not_shadow_json_or_payload_local() {
    let fixture = run_full_swap_fixture(FullSwapFixtureArgs {
        chunk_source: "export const JSON = 0, _d = 0;",
        wrapper_shape: Some(WrapperShape::NamedFromJsonDefault),
        upstream_source: "{\"JSON\":1,\"_d\":2}",
        default_export_aliases: &[],
    });
    assert_wrapper_output(
        &fixture,
        "import * as m from './entry.js'; console.log(m.JSON, m._d);",
        "1 2\n",
    );
}

#[test]
fn bundled_facades_preserve_default_reserved_and_string_export_names() {
    for bundle_export in ["default", "class", "x-y"] {
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
        let spec =
            build_bundled_partial_swap_spec(&ws, json!({"static/megachunk.js": vendor}), None);
        assert_success(&ws.run_spec(&spec, &[("lib", &package_root)]));
        assert_node_output(&ws.out_root.join("app/static/app/entry.js"), "7\n", "");
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
    assert_success(&fixture.result);
    let app = emitted_app_root(&fixture);
    install_node_module(&app, "lib", "1.0.0", upstream);
    assert_node_output(&app.join("static/app/entry.js"), "7\n", "");
}

#[test]
fn bundled_member_swap_preserves_dotted_upstream_paths() {
    assert_bundled_dotted_export(PartialSwapKind::Member, "7\n");
}

#[test]
fn bundled_named_swap_preserves_literal_dotted_exports() {
    assert_bundled_dotted_export(PartialSwapKind::Named, "11\n");
}

fn assert_bundled_dotted_export(kind: PartialSwapKind, expected: &str) {
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
    let spec = build_bundled_partial_swap_spec(&ws, json!({"static/megachunk.js": vendor}), None);
    assert_success(&ws.run_spec(&spec, &[("lib", &package)]));
    assert_node_output(&ws.out_root.join("app/static/app/entry.js"), expected, "");
}

// ─── full swap: cross-module purity ─────────────────────────────────────
