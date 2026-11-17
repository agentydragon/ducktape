//! A selected placement view keeps full-base selector decisions and premises.

use debundle_e2e_support::*;

const SOURCE: &str = r#"
function anchor() {}
function other(argument) {}
const a = anchor.length;
const b = other.length;
console.log(a, b);
export { a, b };
"#;

fn options() -> FixtureOpts<'static> {
    let modules = [
        (
            "anchor",
            "members: [{name: Anchor, selector: {binding: {name: anchor}}}]",
        ),
        (
            "fixed",
            "members: [{name: FixedA, selector: {reads_member: {member: length, object: Anchor}}}]",
        ),
        (
            "kept",
            "source_matches: [{match: 'const value = OBJECT.length;', bindings: [{local: value, name: Kept}]}]",
        ),
    ]
    .into_iter()
    .map(|(path, yaml)| (path.to_string(), serde_yaml::from_str(yaml).unwrap()))
    .collect();
    FixtureOpts::new(SOURCE, modules).with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn new_placement_can_claim_an_omitted_unit_without_changing_retained_identity() {
    // FixedA disambiguates Kept in the full base. Both full-base claims must
    // retain their resolved identities even when only Kept owns output here.
    let base = run_fixture(options().with_import_order(&["anchor", "fixed", "kept"]));
    assert_entry_output(&base, "0 1\n");
    assert_generated_module_after_entry_script(
        &base,
        "const {Kept} = await import('./static/app/modules/kept.js'); console.log(Kept);",
        "1\n",
    );

    let view = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        view.path(),
        serde_json::to_vec(&serde_json::json!({
            "modules": [{"chunk": "static/app", "module": "kept"}],
            "placements": {
                "static/app": {
                    "placed": {
                        "source_matches": [{
                            "match": "const value = Anchor.length;",
                            "bindings": [{"local": "value", "name": "Placed"}]
                        }]
                    }
                }
            }
        }))
        .unwrap(),
    )
    .unwrap();
    let selected = run_fixture(
        options()
            .with_import_order(&["kept", "placed"])
            .with_graphs()
            .with_extra_args(&["--ownership-subset", view.path().to_str().unwrap()]),
    );
    assert_entry_output(&selected, "0 1\n");
    let graph: serde_json::Value = read_gzip_json(
        &selected
            .report_root
            .join("static/app/dependency_graph.json.gz"),
    );
    assert_eq!(graph["import_order"], serde_json::json!(["kept", "placed"]));
    assert_eq!(
        graph["ownership_subset"],
        serde_json::json!([{"module":"kept"}])
    );
    assert_generated_module_after_entry_script(
        &selected,
        "const kept = await import('./static/app/modules/kept.js'); const placed = await import('./static/app/modules/placed.js'); console.log(JSON.stringify([Object.keys(kept).sort(), Object.keys(placed).sort(), kept.Kept, placed.Placed]));",
        "[[\"Kept\"],[\"Placed\"],1,0]\n",
    );
    for omitted in ["anchor", "fixed"] {
        assert!(
            !selected
                .out_root
                .join(format!("static/app/modules/{omitted}.js"))
                .exists()
        );
    }
}

fn selected_view(value: serde_json::Value) -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), serde_json::to_vec(&value).unwrap()).unwrap();
    file
}

#[test]
fn omitted_import_remains_a_binding_anchor_for_new_placement() {
    let view = selected_view(serde_json::json!({
        "modules": [],
        "placements": {"static/app": {"placed": {
            "members": [{"name":"PlacedFactory", "selector":{"binding":{"name":"imported", "kind":"import_specifier"}}}],
            "source_matches": [{
                "match": "const output = Factory();",
                "bindings": [{"local": "output", "name": "Result"}]
            }]
        }}}
    }));
    let fixture = run_fixture(FixtureOpts::new(
            "import { make as imported } from './peer.js';\nconst value = imported();\nconsole.log(value);\n",
            vec![logical_module("imports", &[Member::renamed_with_kind(
                "Factory", "imported", Some("import_specifier")
            )])],
        )
        .with_extra_chunks(&[("static/peer", "export function make() { return 42; }\n")])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["placed"]).with_extra_args(&["--ownership-subset", view.path().to_str().unwrap()]));
    assert_preserved_entry_output(&fixture, "42\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/placed.js",
        &["Result", "PlacedFactory"],
        &["Factory"],
    );
}

#[test]
fn fresh_anonymous_placement_moves_the_complete_statement() {
    let view = selected_view(serde_json::json!({
        "modules": [{"chunk":"static/app", "module":"kept"}],
        "placements": {"static/app": {"placed": {
            "anonymous_statements": [{"source_match": {"match": "console.log(value);"}}]
        }}}
    }));
    let fixture = run_fixture(
        FixtureOpts::new(
            "const value = 7;\nconsole.log(value);\n",
            vec![logical_module("kept", &[Member::new("value")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["kept", "placed"])
        .with_extra_args(&["--ownership-subset", view.path().to_str().unwrap()]),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/placed.js",
        &["console.log(value)"],
        &["const value"],
    );
}

#[test]
fn retained_destructuring_companion_cannot_be_claimed_again() {
    let options = || {
        FixtureOpts::new(
            "const {left, right} = {left: 1, right: 2};\nconsole.log(left, right);\n",
            vec![logical_module("kept", &[Member::new("left")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
    };
    let base = run_fixture(options().with_import_order(&["kept"]));
    assert_preserved_entry_output(&base, "1 2\n");
    let view = selected_view(serde_json::json!({
        "modules": [{"chunk":"static/app", "module":"kept", "bindings":["left"]}],
        "placements": {"static/app": {"placed": {
            "members": [{"name":"Companion", "selector":{"binding":{"name":"right"}}}]
        }}}
    }));
    let rejected = run_rejection_fixture(
        options()
            .with_import_order(&["kept", "placed"])
            .with_extra_args(&["--ownership-subset", view.path().to_str().unwrap()])
            .with_rejection_exit(1),
    );
    assert!(!rejected.out_root.join("app").exists());
}

#[test]
fn placement_uses_an_omitted_owners_necessary_purity_premise() {
    const RUNTIME: &str = "globalThis.opaque = value => value;\nexport {};\n";
    let options = |purity| {
        FixtureOpts::new(
            r#"import './runtime.js';
function wrapper(value) { return globalThis.opaque(value); }
const first = wrapper("a");
const middle = wrapper("b");
const last = wrapper("c");
console.log(first, middle, last);
"#,
            vec![
                logical_module(
                    "omitted",
                    &[Member::renamed("Wrapper", "wrapper").with_purity(purity)],
                ),
                logical_module("kept", &binding_members(["first", "last"])),
            ],
        )
        .with_extra_files(&[
            ("static/runtime.js", RUNTIME),
            ("static/app/runtime.js", RUNTIME),
        ])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["kept", "placed"])
    };
    let view = selected_view(serde_json::json!({
        "modules": [{"chunk":"static/app", "module":"kept"}],
        "placements": {"static/app": {"placed": {
            "source_matches": [{
                "match":"const output = Wrapper(\"b\");",
                "bindings":[{"local":"output", "name":"Middle"}]
            }]
        }}}
    }));
    let args = ["--ownership-subset", view.path().to_str().unwrap()];
    let fixture = run_fixture(options(MemberPurity::Pure).with_extra_args(&args));
    assert_preserved_entry_output(&fixture, "a b c\n");
    let rejected = run_rejection_fixture(
        options(MemberPurity::Default)
            .with_extra_args(&args)
            .with_rejection_exit(1),
    );
    assert!(!rejected.out_root.join("app").exists());
}
