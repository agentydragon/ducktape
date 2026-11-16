//! End-to-end checks for canonical cross-chunk export resolution.

use debundle_e2e_support::*;

#[test]
fn named_and_default_imports_follow_reexport_chains() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { finalNamed } from "./bridge-b.js";
import finalDefault from "./bridge-b.js";
function run() { console.log(finalNamed(), finalDefault()); }
run();
export { run };
"#,
            vec![logical_module("run", &[Member::new("run")])],
        )
        .with_extra_chunks(&[
            (
                "static/leaf",
                "export function named() { return 'named'; }\nexport default function defaultValue() { return 'default'; }\n",
            ),
            (
                "static/bridge-a",
                "export { named as renamed } from './leaf.js';\nexport { default as renamedDefault } from './leaf.js';\n",
            ),
            (
                "static/bridge-b",
                "export { renamed as finalNamed } from './bridge-a.js';\nexport { renamedDefault as default } from './bridge-a.js';\n",
            ),
        ]),
    );

    assert_preserved_entry_output(&fixture, "named default\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn namespace_reexport_keeps_static_method_receiver() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { apiNamespace } from "./bridge.js";
function run() { console.log(apiNamespace.api.read()); }
run();
export { run };
"#,
            vec![logical_module("run", &[Member::new("run")])],
        )
        .with_extra_chunks(&[
            (
                "static/leaf",
                "export const api = { value: 41, read() { return this.value + 1; } };\n",
            ),
            (
                "static/bridge",
                "export * as apiNamespace from './leaf.js';\n",
            ),
        ]),
    );

    assert_preserved_entry_output(&fixture, "42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn star_diamond_with_one_origin_resolves_named_import() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { value } from "./barrel.js";
console.log(value);
"#,
            vec![],
        )
        .with_extra_chunks(&[
            ("static/leaf", "export const value = 42;\n"),
            ("static/left", "export * from './leaf.js';\n"),
            ("static/right", "export * from './leaf.js';\n"),
            (
                "static/barrel",
                "export * from './left.js';\nexport * from './right.js';\n",
            ),
        ]),
    );

    assert_preserved_entry_output(&fixture, "42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn distinct_star_origins_are_absent_from_namespace() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import * as ns from "./barrel.js";
console.log(ns.value);
"#,
            vec![],
        )
        .with_extra_chunks(&[
            ("static/left", "export const value = 'left';\n"),
            ("static/right", "export const value = 'right';\n"),
            (
                "static/barrel",
                "export * from './left.js';\nexport * from './right.js';\n",
            ),
        ]),
    );

    // Namespace imports remain valid when star resolution is ambiguous; ESM
    // omits the ambiguous name instead of selecting either provider.
    assert_preserved_entry_output(&fixture, "undefined\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn cyclic_reexports_do_not_invent_a_provider() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import * as ns from "./cycle-a.js";
console.log(ns.value);
"#,
            vec![],
        )
        .with_extra_chunks(&[
            ("static/cycle-a", "export * from './cycle-b.js';\n"),
            ("static/cycle-b", "export * from './cycle-a.js';\n"),
        ]),
    );

    assert_preserved_entry_output(&fixture, "undefined\n");
    assert_all_emitted_js_checks(&fixture);
}
