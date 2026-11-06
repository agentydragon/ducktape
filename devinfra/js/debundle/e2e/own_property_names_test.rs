//! Guarded own-name inspection preserves exact keys without invoking getters.

use analysis::analysis_provenance::InferenceRule;
use analysis::execution_evidence::SkippedCallReason;
use analysis::reports::schema::BindingEffectAuthorityReport;
use analysis::reports::schema::InferenceUseReport;
use debundle_e2e_graph_support::FixtureGraphExt;
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
                    Member::new("makeValue"),
                    Member::new("getterCalls"),
                    Member::new("argumentThrew"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "field deferred 1 0 true\n");
    let assessment = fixture.candidate_relocation_assessment();
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let execution = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "argumentThrew")
        })
        .and_then(|node| node.synchronous_execution.as_ref())
        .unwrap();
    assert!(execution.complete, "{execution:#?}");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        panic!("caught source throw needs a complete operation receipt: {execution:#?}");
    };
    assert!(
        receipt
            .skipped_calls
            .iter()
            .any(|call| call.reason == SkippedCallReason::KnownThrow),
        "the native body was not entered: {receipt:#?}"
    );
}

#[test]
fn a_caught_argument_throw_retains_the_outer_and_later_call_inventory() {
    let fixture = run_fixture(FixtureOpts::new(
        "function take(value, unused) { return value; }\nfunction stop() { throw 'stopped'; }\nfunction later() { console.log('unexpected'); return 0; }\nconst result = (() => { try { return take(stop(), later()); } catch (error) { return error === 'stopped'; } })();\nconst anchor = 7;\nconsole.log(result, anchor);\n",
        vec![
            logical_module("provider", &[Member::new("take"), Member::new("stop"), Member::new("later"), Member::new("anchor")]),
            logical_module("observer", &[Member::new("result")]),
        ],
    ).with_unassigned_mode(unassigned_mode_inline()));
    assert_preserved_entry_output(&fixture, "true 7\n");
    let assessment = fixture.candidate_relocation_assessment();
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let execution = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .and_then(|node| node.synchronous_execution.as_ref())
        .unwrap();
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        panic!("actual caught throw must close its call inventory: {execution:#?}");
    };
    assert_eq!(
        receipt.calls.len(),
        2,
        "only the outer arrow and stop were invoked: {receipt:#?}"
    );
    assert_eq!(
        receipt.skipped_calls.len(),
        2,
        "the outer take and later argument must each remain accounted for: {receipt:#?}"
    );
    assert!(
        receipt
            .skipped_calls
            .iter()
            .all(|call| call.reason == SkippedCallReason::KnownThrow),
        "{receipt:#?}"
    );
}

#[test]
fn a_returning_try_bypasses_its_catch_calls() {
    let fixture = run_fixture(FixtureOpts::new(
        "function unreachable() { console.log('unexpected'); return 0; }\nconst result = (() => { try { return 41; } catch (error) { return unreachable(); } })();\nconst anchor = 7;\nconsole.log(result, anchor);\n",
        vec![logical_module("provider", &[Member::new("unreachable"), Member::new("anchor")]), logical_module("observer", &[Member::new("result")])],
    ).with_unassigned_mode(unassigned_mode_inline()));
    assert_preserved_entry_output(&fixture, "41 7\n");
    let graph = fixture.dependency_graph();
    let execution = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .and_then(|node| node.synchronous_execution.as_ref())
        .unwrap();
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        panic!("normal return bypass must retain handler evidence: {execution:#?}");
    };
    assert_eq!(receipt.skipped_calls.len(), 1, "{receipt:#?}");
    assert_eq!(
        receipt.skipped_calls[0].reason,
        SkippedCallReason::HandlerNotEntered
    );
}

#[test]
fn unsupported_handler_scopes_and_unknown_abrupt_paths_keep_conservative_coverage() {
    for (source, expected) in [
        (
            "const result = (() => { try { throw 1; } finally { return 2; } })();\nconst later = 42;\nconsole.log(result, later);\n",
            "2 42\n",
        ),
        (
            "const result = (() => { try { const local = 41; return local; } catch (error) { return 0; } })();\nconst later = 42;\nconsole.log(result, later);\n",
            "41 42\n",
        ),
        (
            "const result = (() => { try { return later; } catch (error) { return 7; } })();\nconst later = 42;\nconsole.log(result, later);\n",
            "7 42\n",
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(
            source,
            vec![logical_module(
                "all",
                &[Member::new("result"), Member::new("later")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        let graph = fixture.dependency_graph();
        let execution = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "result")
            })
            .and_then(|node| node.synchronous_execution.as_ref())
            .unwrap();
        assert!(
            !matches!(
                execution.binding_effect_authority,
                Some(BindingEffectAuthorityReport::OperationReceipt { .. })
            ),
            "unsupported or unknown completion cannot seal an operation receipt: {execution:#?}"
        );
    }
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
