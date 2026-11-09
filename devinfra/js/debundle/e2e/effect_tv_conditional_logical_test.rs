//! Report-only effect translation validation: conditional (`?:`) and logical
//! (`&&`, `||`, `??`) operators through the kernel's fork, joined by
//! `alternatives/` when every path of the body has completed.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn assert_commuting_relocation(fixture: &Fixture, case: &str) {
    let report = fixture.effect_translation_validation();
    assert!(
        report["swapped_pairs"].as_u64().unwrap() > 0,
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
}

/// A boolean module constant decides no test, so each body forks; the joined
/// receipt still closes the root's binding effects. `null` decides `??`.
#[test]
fn callable_branches_join_into_one_receipt_across_an_independent_inversion() {
    for (body, expected) in [
        (
            "return verbose ? { kind: 'yes' } : { kind: 'no' };",
            "7 {\"kind\":\"yes\"}\n",
        ),
        (
            "return verbose && { kind: 'yes' };",
            "7 {\"kind\":\"yes\"}\n",
        ),
        ("return verbose || { kind: 'no' };", "7 true\n"),
        (
            "return missing ?? { kind: 'absent' };",
            "7 {\"kind\":\"absent\"}\n",
        ),
        (
            "const label = verbose ? 'yes' : 'no'; return [label, missing ?? 'set'];",
            "7 [\"yes\",\"set\"]\n",
        ),
        (
            "return verbose ? (missing ? 1 : 2) : (missing || 3);",
            "7 2\n",
        ),
    ] {
        let source = format!(
            "const verbose = true;\nconst missing = null;\nfunction pick() {{ {body} }}\nconst independent = 7;\nvar result = pick();\nconsole.log(independent, JSON.stringify(result));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "pick",
                &[
                    Member::new("verbose"),
                    Member::new("missing"),
                    Member::new("pick"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        assert_commuting_relocation(&fixture, body);
    }
}

/// An object is truthy and not nullish, so the short-circuited operand never
/// runs: `choose` calls nothing and reads nothing `fallback` would, and the
/// split from the later binding `fallback` reads is accepted.
#[test]
fn an_object_operand_decides_the_test_without_running_the_other_operand() {
    for body in [
        "return settings || fallback();",
        "return settings ?? fallback();",
    ] {
        let source = format!(
            "const settings = {{ name: 'configured' }};\nfunction fallback() {{ return {{ name: later }}; }}\nfunction choose() {{ {body} }}\nconst independent = 7;\nconst chosen = choose();\nconst later = 'late';\nconsole.log(independent, chosen.name, later);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "chosen",
                &[
                    Member::new("settings"),
                    Member::new("fallback"),
                    Member::new("choose"),
                    Member::new("chosen"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "7 configured late\n");
        assert_inline_operation_receipt(&fixture, "chosen", 1, body);
        assert_commuting_relocation(&fixture, body);
    }
}

/// Opposing control: the arm the call does not take at run time is still a
/// feasible path of the body, so its read of the later `var` stays a
/// dependency. Splitting the reader from the value would print the
/// initialized value where the source prints `undefined`.
#[test]
fn a_read_on_either_arm_keeps_the_split_from_its_later_value_rejected() {
    for (body, original, emitted) in [
        (
            "return flag ? 'early' : String(later);",
            "early undefined\n",
            "early set\n",
        ),
        (
            "return flag && String(later);",
            "undefined false\n",
            "set false\n",
        ),
        (
            "return flag || String(later);",
            "true undefined\n",
            "true set\n",
        ),
    ] {
        let source = format!(
            "function pick(flag) {{ {body} }}\nconst seen = [pick(true), pick(false)].join(' ');\nvar later = 'set';\nconsole.log(seen);\nexport {{ seen, pick, later }};\n"
        );
        let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
            &source,
            vec![
                logical_module("reader", &[Member::new("seen")]),
                logical_module("values", &[Member::new("later")]),
            ],
        ));
        let report = rejected.translation_validation();
        assert_eq!(report["gate_accepts"], false, "{body}: {report:#}");
        assert_eq!(report["accepts"], false, "{body}: {report:#}");
        assert_node_output(
            &rejected
                .source_root
                .join(format!("{}.js", rejected.chunk_id)),
            original,
            "",
        );
        assert_node_output(&rejected.entry_path(), emitted, "");
    }
}

/// Opposing control: a call on one arm only is a may-call of the body. Its
/// write to module state stays ordered against a later reader, and the root
/// gets no receipt naming a call that some path skips.
#[test]
fn a_call_on_one_arm_only_stays_a_may_effect_without_a_receipt() {
    let source = "const state = { count: 0 };\nfunction bump() { state.count = state.count + 1; return state.count; }\nfunction maybe(flag) { return flag ? bump() : 0; }\nconst first = maybe(true);\nconst observed = state.count;\nconsole.log(first, observed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "counter",
            &[
                Member::new("state"),
                Member::new("bump"),
                Member::new("maybe"),
                Member::new("first"),
                Member::new("observed"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 1\n");
    let execution = binding_execution_report(&fixture, "first");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
}
