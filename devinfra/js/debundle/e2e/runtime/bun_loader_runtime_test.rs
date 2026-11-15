//! Pinned Bun runtime coverage for a loader call moved with an extracted function.

use debundle_e2e_support::*;
use std::fs;

const SOURCE: &str = r#"function loadPeer() {
  return import.meta.require("./peer.js").answer;
}

console.log(loadPeer());
export { loadPeer };
"#;

const PEER: &str = "const value = 7;\nexport { value as answer };\n";

fn loader_fixture(source: &str, source_paths: &[(&'static str, &str)]) -> Fixture {
    let peer_modules = [(
        "static/peer",
        vec![logical_module(
            "provider",
            &[Member::renamed("answer", "value")],
        )],
    )];
    run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("nested/loader", &[Member::new("loadPeer")])],
        )
        .with_extra_chunks(&[("static/peer", PEER)])
        .with_extra_chunk_logical_modules(&peer_modules)
        .with_source_paths(source_paths),
    )
}

fn assert_loader_emission(fixture: &Fixture, original_request: &str) {
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/nested/loader.js",
        &["import.meta.require", "../../../peer/entry.js"],
        &[original_request],
    );
    assert_module_source(
        &fixture.out_root,
        "static/peer/modules/provider.js",
        &["answer", "7"],
        &[],
    );
    assert_preserved_entry_output_with_bun(fixture, "7\n");
}

#[test]
fn extracted_nested_loader_function_loads_renamed_peer_under_pinned_bun() {
    assert_loader_emission(&loader_fixture(SOURCE, &[]), "./peer.js");
}

#[test]
fn loader_rebasing_resolves_bunfs_aliased_source_paths() {
    let source_paths = [
        ("static/app", "$bunfs/root/app.js"),
        ("static/peer", "$bunfs/root/peer.js"),
    ];
    assert_loader_emission(&loader_fixture(SOURCE, &source_paths), "./peer.js");
}

#[test]
fn absolute_loader_request_rebases_to_captured_source_path_alias() {
    let temp_dir = tempfile::TempDir::new().expect("temporary peer directory");
    let peer_path = temp_dir.path().join("peer.js");
    fs::write(&peer_path, PEER).expect("write temporary peer module");
    let absolute_peer_path = peer_path.to_str().expect("UTF-8 temporary path");
    let source = format!(
        "function loadPeer() {{\n  return import.meta.require({absolute_peer_path:?}).answer;\n}}\n\nconsole.log(loadPeer());\nexport {{ loadPeer }};\n"
    );
    // Absolute loader requests are canonicalized to source-root-relative
    // paths by the artifact resolver, just like `/$bunfs/root/peer.js`.
    let source_paths = [("static/peer", absolute_peer_path.trim_start_matches('/'))];
    let fixture = loader_fixture(&source, &source_paths);
    assert_loader_emission(&fixture, absolute_peer_path);
}
