//! Imported initializer bodies and their evaluation-time availability.

use super::{assert_unmoved_output, expect_single_binding_rejection};
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
use debundle_e2e_support::*;

const ENTRY: &str = "field(() => later);\n\
const icon = node('svg');\n\
const later = () => icon;\n\
console.log(globalThis.get()());\n";
const PROVIDER: &str = "export const field = callback => globalThis.get = callback;\n\
export const node = tag => tag;\n";

#[test]
fn passive_multideclaration_provider_preserves_the_local_split() {
    for (prefix, peers, members) in [
        (
            "import { field, node } from './peer.js';\n",
            &[("static/peer", PROVIDER)][..],
            &[Member::new("icon")][..],
        ),
        (
            "const field = callback => globalThis.get = callback;\nconst node = tag => tag;\n",
            &[][..],
            &binding_members(["node", "icon"])[..],
        ),
    ] {
        let source = format!("{prefix}{ENTRY}");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![logical_module("icons", members)])
                .with_extra_chunks(peers)
                .with_import_order(&["icons"])
                .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "svg\n");
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/icons.js",
            &["icon"],
            &[],
        );
    }
}

#[test]
fn passive_provider_cycles_and_saved_analysis_preserve_availability() {
    // Lazy references create a real cycle between emitted provider owners.
    // Neither owner's initialization executes either reference.
    let provider = "export const first = () => second;\n\
        export const second = () => first;\n\
        const field = callback => globalThis.get = callback;\n\
        const node = function (tag) { return tag; };\n\
        const unrelated = 0;\nexport { field, node };\n";
    let source = format!(
        "import {{ field, node, first, second }} from './peer.js';\n{ENTRY}\nconsole.log(first() === second, second() === first);\n"
    );
    let peers = [("static/peer", provider)];
    let provider_modules = [(
        "static/peer",
        vec![
            logical_module("first", &binding_members(["first", "node"])),
            logical_module("second", &binding_members(["second", "field"])),
        ],
    )];
    let (case, _fixture) = staged(
        FixtureOpts::new(
            &source,
            vec![logical_module("icons", &[Member::new("icon")])],
        )
        .with_extra_chunks(&peers)
        .with_extra_chunk_logical_modules(&provider_modules)
        .with_chunk_import_order("static/peer", &["first", "second", "residual"])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(
        &case.root().join("snapshot/static/app.js"),
        "svg\ntrue true\n",
        "",
    );
    let fresh = case.run(&[]);
    let saved = case.run(&["--analysis", path(&case.artifact)]);
    for invocation in [&fresh, &saved] {
        assert!(
            invocation.result.status.success(),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "svg\ntrue true\n",
            "",
        );
    }
    assert_same_invocation_trees(&fresh, &saved);
}

#[test]
fn active_mutable_and_captured_providers_keep_the_conservative_split() {
    for provider in [
        // A constant declaration with a primitive initializer is inert; a
        // read, call, allocation, pattern or executable statement is not.
        "const unrelated = globalThis.value;\nexport const field = callback => globalThis.get = callback;\nexport const node = tag => tag;\n",
        "globalThis.ready = true;\nexport const field = callback => globalThis.get = callback;\nexport const node = tag => tag;\n",
        "export const field = callback => globalThis.get = callback;\nexport let node = tag => 'old';\nnode = tag => tag;\n",
        "export const field = callback => globalThis.get = callback;\nconst value = 'svg';\nexport const node = tag => value;\n",
        "import './other.js';\nexport const field = callback => globalThis.get = callback;\nexport const node = tag => tag;\n",
    ] {
        let source = format!("import {{ field, node }} from './peer.js';\n{ENTRY}");
        let peers = [
            ("static/peer", provider),
            ("static/other", "export const value = 0;\n"),
        ];
        assert_unmoved_output(&source, &peers, "svg\n");
        expect_single_binding_rejection(&source, &peers, "icons", "icon");
    }
}

#[test]
fn early_import_cycle_keeps_the_callables_tdz() {
    let provider = "import { observe } from './app.js';\n\
        globalThis.early = observe();\n\
        export const field = callback => globalThis.get = callback;\n\
        export const node = tag => tag;\n";
    let source = format!(
        "import {{ field, node }} from './peer.js';\n\
        export function observe() {{ try {{ return node('svg'); }} catch (error) {{ return error.name; }} }}\n\
        {ENTRY}\nconsole.log(globalThis.early);\n"
    );
    let peers = [("static/peer", provider)];
    assert_unmoved_output(&source, &peers, "svg\nReferenceError\n");
    expect_single_binding_rejection(&source, &peers, "icons", "icon");
}

#[test]
fn a_callback_invoking_body_does_not_gain_the_readiness_summary() {
    let provider = "export const field = callback => globalThis.get = callback;\n\
        export const node = tag => { try { globalThis.get(); } catch (error) { globalThis.early = error.name; } return tag; };\n";
    let source = format!(
        "import {{ field, node }} from './peer.js';\n{ENTRY}\nconsole.log(globalThis.early);\n"
    );
    let peers = [("static/peer", provider)];
    assert_unmoved_output(&source, &peers, "svg\nReferenceError\n");
    expect_single_binding_rejection(&source, &peers, "icons", "icon");
}
