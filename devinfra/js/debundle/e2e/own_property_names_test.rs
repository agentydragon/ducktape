//! Guarded own-name inspection preserves exact keys without invoking getters.

use analysis::reports::schema::{InferenceRule, InferenceUse};
use debundle_e2e_support::*;
use std::collections::BTreeSet;

fn own_name_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("keys", &[Member::new("names"), Member::new("getterCalls")]),
        logical_module("later", &[Member::new("later")]),
    ]
}

#[test]
fn ordinary_own_property_names_preserve_order_and_do_not_read_values() {
    let source = r#"
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
    let fixture = run_fixture(FixtureOpts::new(source, own_name_modules()));
    assert_preserved_entry_output(&fixture, "2 10 4294967294 z 01 4294967295 a value 0\n");

    let graph = fixture.dependency_graph();
    let names = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "names")
        })
        .expect("own-name source unit");
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
        &InferenceUse {
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
    Object.getOwnPropertyNames({ field: (() => { throw new Error("argument"); })() });
    return false;
  } catch (error) {
    return error.message === "argument";
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
                    Member::new("getterCalls"),
                    Member::new("argumentThrew"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "field deferred 1 0 true\n");
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
