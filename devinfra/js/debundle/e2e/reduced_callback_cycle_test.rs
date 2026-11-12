use debundle_e2e_support::*;

#[test]
fn numeric_loops_in_initializers_can_split() {
    let source = "const input = [1];\n\
                  const marker = (() => { let n = 1; while (n < 2) n++; return n; })();\n\
                  const upper = input.map(x => { let n = x; while (n < 2) n++; return n; });\n\
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
            serde_json::from_value(
                serde_json::json!({"intrinsics_never_written": {"families": ["Array"]}}),
            )
            .unwrap(),
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    assert!(outcome.source.status.success(), "{}", outcome.source.stderr);
    assert_eq!(outcome.source.stdout, "2 2\n");
    assert!(
        outcome.emitted.status.success(),
        "{}",
        outcome.emitted.stderr
    );
    assert_eq!(outcome.emitted.stdout, outcome.source.stdout);
    assert!(outcome.accepted, "numeric loop split was rejected");
}
