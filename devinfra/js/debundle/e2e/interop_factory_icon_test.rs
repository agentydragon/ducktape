//! An imported field helper stores a callback without invoking it during
//! icon initialization. This reduces one pinned Excalidraw icons.tsx
//! boundary: `field` models the callback timing of __publicField, `node`
//! models imported JSX construction, and `icon` is the selected value.

use debundle_e2e_support::*;

const SOURCE: &str = r#"import { field, node } from "./peer.js";
field(() => later);
const icon = node("svg");
const later = () => icon;
console.log(globalThis.get()());
"#;

const PEER: &str = r#"export const field = (callback) => globalThis.get = callback;
export const node = (tag) => tag;
"#;

fn fixture(modules: Vec<LogicalModuleEntry>) -> FixtureOpts<'static> {
    FixtureOpts::new(SOURCE, modules)
        .with_extra_chunks(&[("static/peer", PEER)])
        .with_unassigned_mode(unassigned_mode_coarse_atomic_groups())
}

#[test]
fn unsplit_source_and_emitted_tree_both_print_svg() {
    let fixture = run_fixture(fixture(vec![]));
    assert_preserved_entry_output(&fixture, "svg\n");
}

#[test]
fn expected_split_prints_the_same_value_under_node() {
    // This is a witness for this concrete fixture, not generated Debundle
    // output or a proof about arbitrary imported functions. The authored
    // `icons` module owns only `icon`; the other bindings are placed in
    // illustrative generated residual groups. Their exact auto paths and
    // grouping are not part of this assertion.
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("package.json"), r#"{"type":"module"}"#).unwrap();
    std::fs::write(root.path().join("peer.js"), PEER).unwrap();
    std::fs::write(
        root.path().join("icons.js"),
        "import { node } from './peer.js';\n\
         export const icon = node('svg');\n",
    )
    .unwrap();
    std::fs::write(
        root.path().join("residual.js"),
        "import { field } from './peer.js';\n\
         import { icon } from './icons.js';\n\
         field(() => later);\n\
         const later = () => icon;\n\
         export const print = () => console.log(globalThis.get()());\n",
    )
    .unwrap();
    let entry = root.path().join("entry.js");
    std::fs::write(&entry, "import { print } from './residual.js';\nprint();\n").unwrap();
    assert_node_output(&entry, "svg\n", "");
}

#[test]
fn icon_value_can_move_past_uninvoked_callback_registration() {
    let fixture = run_fixture(fixture(vec![logical_module(
        "icons",
        &[Member::new("icon")],
    )]));
    assert_preserved_entry_output(&fixture, "svg\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/icons.js",
        &["icon"],
        &[],
    );
}
