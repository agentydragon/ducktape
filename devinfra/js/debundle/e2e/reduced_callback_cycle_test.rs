use debundle_e2e_support::*;

#[test]
fn string_normalization_and_array_map_can_split() {
    let source = "const input = ['a'];\n\
                  const marker = 'X'.toLowerCase();\n\
                  const upper = input.map(x => x.toUpperCase());\n\
                  console.log(upper[0], marker);\n";
    let outcome = run_fixture_to_gate_outcome(
        FixtureOpts::new(
            source,
            vec![
                logical_module("arrays", &[Member::new("input"), Member::new("upper")]),
                logical_module("normalization", &[Member::new("marker")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_program_premises(
            serde_json::from_value(serde_json::json!({
                "intrinsics_never_written": {"families": ["Array"]}
            }))
            .unwrap(),
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    assert!(outcome.source.status.success(), "{}", outcome.source.stderr);
    assert_eq!(outcome.source.stdout, "A x\n");
    assert!(
        outcome.emitted.status.success(),
        "{}",
        outcome.emitted.stderr
    );
    assert_eq!(outcome.emitted.stdout, outcome.source.stdout);
    assert!(outcome.accepted, "the valid module split was rejected");
}
