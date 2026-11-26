//! Guarded own-name inspection preserves exact keys without invoking getters.

use analysis_vocabulary::provenance::InferenceRule;
use analysis_vocabulary::report_schema::InferenceUseReport;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;
use std::collections::BTreeSet;

const OWN_NAME_SOURCE: &str = r#"
let getterCalls = 0;
const names = Object.getOwnPropertyNames({
  z: 0,
  10: 0,
  2: 0,
  4294967294: 0,
  "01": 0,
  "4294967295": 0,
  a: 0,
  get value() { getterCalls++; return later; },
});
const later = 42;
console.log(names[0], names[1], names[2], names[3], names[4], names[5], names[6], names[7], getterCalls);
"#;
fn own_name_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("keys", &binding_members(["names", "getterCalls"])),
        logical_module("later", &[Member::new("later")]),
    ]
}

fn own_name_fixture() -> Fixture {
    run_fixture(FixtureOpts::new(OWN_NAME_SOURCE, own_name_modules()))
}

fn own_name_fixture_with_graphs() -> Fixture {
    run_fixture_with_graphs(FixtureOpts::new(OWN_NAME_SOURCE, own_name_modules()))
}

fn assert_own_name_extraction(fixture: &Fixture) {
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/keys.js",
        &["names", "getterCalls"],
        &["later"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/later.js",
        &["later"],
        &["names"],
    );
}

#[test]
fn ordinary_own_property_names_preserve_order_and_do_not_read_values() {
    let fixture = own_name_fixture();
    assert_preserved_entry_output(&fixture, "2 10 4294967294 z 01 4294967295 a value 0\n");
    assert_own_name_extraction(&fixture);
}

/// `docs/inference.md` documents this consumed-certificate receipt separately
/// from the runtime property-name and getter behavior above.
#[test]
fn own_name_projection_reports_its_documented_inference_receipt() {
    let fixture = own_name_fixture_with_graphs();
    assert_preserved_entry_output(&fixture, "2 10 4294967294 z 01 4294967295 a value 0\n");
    assert_own_name_extraction(&fixture);

    let graph = fixture.dependency_graph();
    let names = node_declaring(&graph, "names");
    let usage = names
        .synchronous_execution
        .as_ref()
        .expect("synchronous execution report")
        .inference_uses
        .iter()
        .find(|usage| usage.conclusion == InferenceRule::OwnPropertyNamesProjection)
        .expect("own-name projection inference");
    assert_eq!(
        usage,
        &InferenceUseReport {
            conclusion: InferenceRule::OwnPropertyNamesProjection,
            derivation_rules: BTreeSet::from([
                InferenceRule::NativeIntegrity,
                InferenceRule::NativeCallIdentity,
                InferenceRule::OwnPropertySourceOrigin,
                InferenceRule::OwnPropertyShape,
                InferenceRule::OwnPropertyPrefixState,
                InferenceRule::OwnPropertyNamesProjection,
            ]),
            value_input_refs: BTreeSet::new(),
            descriptor_copy_author_refs: Default::default(),
            argument_input_refs: BTreeSet::new(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
            imported_primitive_array_spread_input_refs: BTreeSet::new(),
        }
    );
}

#[test]
fn own_name_inspection_keeps_eager_argument_effects_and_throws() {
    let source = r#"
let argumentCalls = 0;
let getterCalls = 0;
function makeValue() { argumentCalls++; return 1; }
const names = Object.getOwnPropertyNames({
  field: makeValue(),
  get deferred() { getterCalls++; return later; },
});
const argumentThrew = (() => {
  try {
    Object.getOwnPropertyNames({ field: (() => { throw "argument"; })() });
    return false;
  } catch (error) {
    return error === "argument";
  }
})();
const later = 42;
console.log(names[0], names[1], argumentCalls, getterCalls, argumentThrew);
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "keys",
                &[
                    Member::new("names"),
                    Member::new("argumentCalls"),
                    // Increments `argumentCalls`, so it is claimed with it.
                    Member::new("makeValue"),
                    Member::new("getterCalls"),
                    Member::new("argumentThrew"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "field deferred 1 0 true\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/keys.js",
        &["names", "argumentThrew"],
        &["later"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/later.js",
        &["later"],
        &["names"],
    );
}

fn expect_own_name_split_rejection(source: &str, extra_members: &[&'static str]) {
    let mut keys = vec![Member::new("names")];
    keys.extend(extra_members.iter().copied().map(Member::new));
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("keys", &keys),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["keys", "later"],
        &[],
    );
}

#[test]
fn own_name_inspection_rejects_replaced_shadowed_proxy_and_mutated_inputs() {
    expect_own_name_split_rejection(
        "Object.getOwnPropertyNames = record => record.value;\nconst names = Object.getOwnPropertyNames({ get value() { return later; } });\nconst later = 42;\n",
        &[],
    );
    expect_own_name_split_rejection(
        "const Object = { getOwnPropertyNames(record) { return record.value; } };\nconst names = Object.getOwnPropertyNames({ get value() { return later; } });\nconst later = 42;\n",
        &["Object"],
    );
    expect_own_name_split_rejection(
        "const record = new Proxy({ get value() { return later; } }, { ownKeys(target) { target.value; return ['value']; } });\nconst names = Object.getOwnPropertyNames(record);\nconst later = 42;\n",
        &["record"],
    );
    expect_own_name_split_rejection(
        "const record = { get value() { return later; } };\n[0].forEach(() => { record.extra = 1; });\nconst names = Object.getOwnPropertyNames(record);\nconst later = 42;\n",
        &["record"],
    );
    expect_own_name_split_rejection(
        "const record = { get value() { return later; } };\nglobalThis.saved = record;\nconst names = Object.getOwnPropertyNames(globalThis.saved);\nconst later = 42;\n",
        &["record"],
    );
}

#[test]
fn own_property_completed_transfer_preserves_fresh_and_saved_acceptance_and_refusal() {
    saved_analysis::assert_staged_entry_output(
        FixtureOpts::new(OWN_NAME_SOURCE, own_name_modules()),
        "2 10 4294967294 z 01 4294967295 a value 0\n",
    );
    let source = "Object.getOwnPropertyNames = record => record.value;\nconst names = Object.getOwnPropertyNames({ get value() { return later; } });\nconst later = 42;\n";
    saved_analysis::assert_staged_run_matches(
        FixtureOpts::new(
            source,
            vec![
                logical_module("keys", &[Member::new("names")]),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &[],
    );
}
