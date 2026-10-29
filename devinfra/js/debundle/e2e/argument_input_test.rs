//! A selected local helper can borrow an enum table without making it escape.
use debundle_e2e_support::*;
use sha2::{Digest, Sha256};

const SOURCE: &str = r#"var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
function keys(input) { return Object.keys(input).filter((key) => Number.isNaN(+key)); }
const names = keys(Status);
const preferences = { [Status.READY]: true };
console.log(names.join(','), preferences.ready);
export { Status, keys, names, preferences };
"#;

fn input(source: &str, declaration: &str) -> ArgumentInputContract {
    ArgumentInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [keys]\n",
            declaration
        ))
        .expect("source selector"),
        parameter_index: 0,
        exact_arity: 1,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("The audited helper reads this argument without retaining it.".into()),
    }
}

fn opts(source: &str, input: Option<ArgumentInputContract>) -> FixtureOpts<'_> {
    let opts = FixtureOpts::new(source, vec![]);
    match input {
        Some(input) => opts.with_argument_inputs("static/app", vec![input]),
        None => opts,
    }
}

const DECLARATION: &str =
    "function keys(input) { return Object.keys(input).filter((key) => Number.isNaN(+key)); }";

fn purity(fixture: &Fixture, binding: &str) -> String {
    let graph = fixture.dependency_graph();
    let unit = source_unit_for_binding(&graph, binding);
    let node = graph.nodes.iter().find(|node| node.id == unit).unwrap();
    serde_json::to_value(node).unwrap()["purity"]["kind"]
        .as_str()
        .unwrap()
        .to_string()
}

#[test]
fn selected_helper_borrow_preserves_enum_plain_data() {
    let baseline = run_fixture(opts(SOURCE, None));
    assert_preserved_entry_output(&baseline, "READY true\n");
    assert_ne!(purity(&baseline, "preferences"), "pure");

    let fixture = run_fixture(opts(SOURCE, Some(input(SOURCE, DECLARATION))));
    assert_preserved_entry_output(&fixture, "READY true\n");
    assert_eq!(purity(&fixture, "preferences"), "pure");
    let graph = fixture.dependency_graph();
    let node_id = source_unit_for_binding(&graph, "preferences");
    let node = graph.nodes.iter().find(|node| node.id == node_id).unwrap();
    let report = serde_json::to_value(node).unwrap();
    assert_eq!(
        report["purity"]["dependencies"]["argument_inputs"][0]["input_index"],
        0
    );
    assert!(
        report["synchronous_execution"]["inference_uses"]
            .as_array()
            .is_some_and(|uses| uses.iter().any(|usage| {
                usage["argument_input_refs"][0]["input_index"] == 0
                    && usage["argument_input_spec_locations"][0]
                        .as_str()
                        .is_some_and(|location| location.contains("argument_inputs[0]"))
                    && usage["derivation_rules"].as_array().is_some_and(|rules| {
                        rules.contains(&serde_json::json!("argument_no_escape"))
                            && rules.contains(&serde_json::json!("stable_function"))
                    })
            }))
    );
}

#[test]
fn unrelated_argument_contract_does_not_admit_enum_plain_data() {
    let declaration = "function unrelated(input) { return Object.keys(input); }";
    let source = SOURCE.replace(DECLARATION, &format!("{DECLARATION}\n{declaration}"));
    let mut unrelated = input(&source, declaration);
    unrelated.source_match = serde_yaml::from_str(&format!(
        "match: {:?}\nbindings: [unrelated]\n",
        declaration
    ))
    .unwrap();
    let fixture = run_fixture(opts(&source, Some(unrelated)));
    assert_ne!(purity(&fixture, "preferences"), "pure");
}

#[test]
fn two_annotated_parameters_of_one_helper_are_both_protected() {
    let declaration = "function keys(first, second) { Object.keys(first); return Object.keys(second).filter((key) => Number.isNaN(+key)); }";
    let source = SOURCE
        .replace(DECLARATION, declaration)
        .replace("keys(Status)", "keys(Status, Status)");
    let mut first = input(&source, declaration);
    first.exact_arity = 2;
    let mut second = first.clone();
    second.parameter_index = 1;
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_argument_inputs("static/app", vec![first, second]),
    );
    assert_eq!(purity(&fixture, "preferences"), "pure");
}

#[test]
fn visible_mutation_return_and_capture_contradict_the_contract() {
    for replacement in [
        "function keys(input) { input.READY = 'wrong'; return []; }",
        "function keys(input) { Object.assign(input, { READY: 'wrong' }); return []; }",
        "function keys(input) { return input; }",
        "function keys(input) { throw input; }",
        "function keys(input) { globalThis.retained = input; return []; }",
        "function keys(input) { const retained = input; return []; }",
        "function keys(input) { return () => input; }",
    ] {
        let source = SOURCE.replace(DECLARATION, replacement);
        expect_rejection_containing_all(
            opts(&source, Some(input(&source, replacement))),
            &["argument_inputs[0]", "contradicts"],
        );
    }
}

#[test]
fn rebind_spread_and_extra_call_argument_do_not_use_the_contract() {
    for call in ["keys(...[Status])", "keys(Status, 2)"] {
        let source = SOURCE.replace("keys(Status)", call);
        let fixture = run_fixture(opts(&source, Some(input(&source, DECLARATION))));
        assert_ne!(purity(&fixture, "preferences"), "pure", "{call}");
    }
    let source = SOURCE.replace(
        "const names = keys(Status);",
        "keys = (input) => Object.keys(input);\nconst names = keys(Status);",
    );
    let fixture = run_fixture(opts(&source, Some(input(&source, DECLARATION))));
    assert_ne!(purity(&fixture, "preferences"), "pure");
}

#[test]
fn stale_hash_wrong_arity_and_ambiguous_selector_are_rejected() {
    let mut stale = input(SOURCE, DECLARATION);
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(opts(SOURCE, Some(stale)), &["argument_inputs[0]", "stale"]);

    let mut wrong_arity = input(SOURCE, DECLARATION);
    wrong_arity.exact_arity = 2;
    expect_rejection_containing_all(
        opts(SOURCE, Some(wrong_arity)),
        &["argument_inputs[0]", "exact arity"],
    );

    let mut ambiguous = input(SOURCE, DECLARATION);
    ambiguous.source_match = serde_yaml::from_str(&format!(
        "match: {:?}\nbindings: [keys, names]\n",
        DECLARATION
    ))
    .unwrap();
    expect_rejection_containing_all(
        opts(SOURCE, Some(ambiguous)),
        &["argument_inputs[0]", "exactly one declared binding"],
    );
}
