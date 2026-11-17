//! URL-rebasing tests: when a function containing a relative URL literal
//! moves from the entry into a nested module, the URL string must be
//! rewritten so it still resolves to the same target.

use debundle_e2e_support::*;

#[test]
fn rebases_and_executes_dynamic_import_of_a_captured_chunk() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"async function load() { const module = await import("./lazy.js"); return module.answer; }
load().then(console.log);
export { load };
"#,
            vec![logical_module("nested/loader", &[Member::new("load")])],
        )
        .with_extra_chunks(&[("static/lazy", "export const answer = 42;")])
        .with_browser_harness(),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/nested/loader.js",
        &[r#"import("../../../lazy/entry.js")"#],
        &[],
    );
    assert_preserved_entry_output(&fixture, "42\n");
    assert_node_output(
        &fixture
            .harness_root
            .as_ref()
            .unwrap()
            .join("app/bootstrap.js"),
        "42\n",
        "",
    );
}

#[test]
fn resource_url_reads_the_same_captured_stylesheet() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { readFileSync } from "node:fs";
function readStyle() { return readFileSync(new URL("./theme.css", import.meta.url), "utf8"); }
console.log(readStyle());
"#,
            vec![logical_module("nested/style", &[Member::new("readStyle")])],
        )
        .with_extra_files(&[("static/theme.css", "body { color: green; }")])
        .with_browser_harness(),
    );
    assert_preserved_entry_output(&fixture, "body { color: green; }\n");
    assert_node_output(
        &fixture
            .harness_root
            .as_ref()
            .unwrap()
            .join("app/bootstrap.js"),
        "body { color: green; }\n",
        "",
    );
}

#[test]
fn external_dynamic_import_remains_executable() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "function load() { return import('data:text/javascript,export const answer = 42;'); } load().then(module => console.log(module.answer));",
            vec![logical_module("loader", &[Member::new("load")])],
        ).with_browser_harness(),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}
