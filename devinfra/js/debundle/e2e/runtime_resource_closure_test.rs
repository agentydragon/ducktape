use debundle_e2e_support::*;
use serde_json::Value;

#[test]
fn closure_distinguishes_emitted_captured_missing_external_and_unknown() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"function resources(path) {
  return [import("./lazy.js"), import("./missing.js"), import("./unlisted.js"),
    import("https://example.invalid/external.js"),
    new URL("./theme.css", import.meta.url), new URL(path, import.meta.url),
    new URL("./theme.css?cache=1", import.meta.url), new URL("theme.css", import.meta.url), import("theme.css"), new Worker("./worker.js")];
}
console.log(typeof resources);
"#,
            vec![logical_module("resources", &[Member::new("resources")])],
        )
        .with_extra_chunks(&[("static/lazy", "export const answer = 42;")])
        .with_extra_files(&[
            ("static/theme.css", "body {}"),
            ("static/unlisted.js", "export const answer = 42;"),
        ])
        .with_browser_harness(),
    );
    assert_preserved_entry_output(&fixture, "function\n");
    let report: Value = read_json(
        &fixture
            .harness_root
            .as_ref()
            .unwrap()
            .join("reports/runtime.json"),
    );
    let resources = report["runtime_resources"]
        .as_array()
        .expect("resource rows");
    let destinations: Vec<_> = resources.iter().map(|row| &row["destination"]).collect();
    assert!(
        destinations
            .iter()
            .any(|row| row["unresolved"]["reason"] == "document_relative_worker")
    );
    assert!(
        destinations
            .iter()
            .any(|row| row["emitted"]["path"] == "static/lazy/entry.js")
    );
    assert!(
        destinations
            .iter()
            .any(|row| row["captured"]["path"] == "static/theme.css")
    );
    assert!(
        destinations.iter().any(
            |row| row["unresolved"]["reason"]["missing_capture"]["path"] == "static/missing.js"
        )
    );
    assert!(destinations.iter().any(
        |row| row["unresolved"]["reason"]["missing_emission"]["path"] == "static/unlisted.js"
    ));
    assert!(
        destinations
            .iter()
            .any(|row| row["external"]["url"] == "https://example.invalid/external.js")
    );
    assert!(
        destinations
            .iter()
            .any(|row| row["unresolved"]["reason"] == "non_literal")
    );
    assert!(
        destinations
            .iter()
            .any(|row| row["unresolved"]["reason"] == "unresolved_url_identity")
    );
    assert!(
        resources
            .iter()
            .any(|row| row["resource"]["specifier"] == "theme.css"
                && row["destination"]["external"]["url"] == "theme.css")
    );
    assert!(
        resources
            .iter()
            .any(|row| row["resource"]["specifier"] == "theme.css"
                && row["destination"]["captured"]["path"] == "static/theme.css")
    );
}

#[test]
fn external_html_module_entry_remains_in_the_executed_bootstrap() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "function ready() { return 42; } console.log(ready());",
            vec![logical_module("ready", &[Member::new("ready")])],
        )
        .with_browser_harness()
        .with_extra_files(&[("index.html", r#"<!doctype html><script type="module" src="./static/app.js"></script><script type="module" src="data:text/javascript,console.log('external')"></script>"#)]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    assert_node_output(
        &fixture
            .harness_root
            .as_ref()
            .unwrap()
            .join("app/bootstrap.js"),
        "42\nexternal\n",
        "",
    );
}
