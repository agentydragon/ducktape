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

/// The split moving `observed` (a read of `counter`) after `result`. When
/// `flag()` can hold a value its source-summary lane omits, `second` runs and
/// writes `counter`: the source prints `0 1`, the split `1 1`.
fn assert_reader_split_diverges(definitions: &str) {
    let source = format!(
        "let counter = 0;\nfunction first() {{ return 0; }}\nfunction second() {{ counter = 1; return 1; }}\nfunction noop() {{ return 0; }}\n{definitions}\nconst observed = counter;\nvar result = run();\nconsole.log(observed, counter);\n"
    );
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("reader", &[Member::new("observed")]),
                logical_module(
                    "run",
                    &[
                        Member::new("counter"),
                        Member::new("first"),
                        Member::new("second"),
                        Member::new("noop"),
                        Member::new("flag"),
                        Member::new("run"),
                        Member::new("result"),
                    ],
                ),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = rejected.translation_validation();
    assert_eq!(report["gate_accepts"], false, "{definitions}: {report:#}");
    assert_eq!(report["accepts"], false, "{definitions}: {report:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "0 1\n",
        "",
    );
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

/// Opposing control: a test on a call whose callee has no ordered plan (the
/// `if` keeps it on the source-summary lane) decides nothing the summary
/// cannot prove. A binding read before any assignment is `undefined`, so
/// `flag()` may be falsy and the arm writing `counter` is feasible.
#[test]
fn a_summarized_binding_read_before_its_assignment_keeps_the_falsy_arm() {
    for body in [
        "let r; if (first()) r = {}; return r;",
        "var r; if (first()) r = {}; return r;",
        "if (first()) { var r = {}; } return r;",
        "if (!first()) return r; var r = {}; return r;",
    ] {
        assert_reader_split_diverges(&format!(
            "function flag() {{ {body} }}\nfunction run() {{ return flag() ? first() : second(); }}"
        ));
    }
}

/// Opposing control: a local only a closure assigns has no assignment the
/// source-summary lane evaluates. It stays on the lane's fallback for an
/// absent binding rather than becoming `undefined` alone, so the truthy arm
/// that runs `second` is kept.
#[test]
fn a_summarized_local_only_a_closure_assigns_is_not_only_undefined() {
    assert_reader_split_diverges(
        "function flag() { let r; const set = () => { r = {}; }; if (first()) {} set(); return r; }\nfunction run() { return flag() ? second() : first(); }",
    );
}

/// Opposing control: a closure's write to its owner's local reaches the
/// owner's frame through a shared cell. Where the source-summary lane cannot
/// follow a value into a local (a closure's write, a value carried around a
/// loop, a closure reading a later write, a loop head's elements), the read
/// is unknown and the stored value is published. Either way a test (`?:`)
/// keeps both arms and a call through the local (`flag()()`) reaches `second`.
#[test]
fn a_summarized_local_holds_every_value_any_read_can_see() {
    let test = "function run() { return flag() ? first() : second(); }";
    let call = "function run() { return flag()(); }";
    for (body, run) in [
        (
            "let r = {}; const clear = () => { r = undefined; }; if (first()) {} clear(); return r;",
            test,
        ),
        (
            "let f = noop; const set = () => { f = second; }; if (first()) {} set(); return f;",
            call,
        ),
        (
            "let r = {}; let s = {}; for (const x of [1, 2]) { s = r; r = undefined; } if (first()) {} return s;",
            test,
        ),
        (
            "let f = noop; let g = noop; for (const x of [1, 2]) { g = f; f = second; } if (first()) {} return g;",
            call,
        ),
        (
            "let f = noop; const get = () => f; if (first()) {} f = second; return get();",
            call,
        ),
        (
            "let f = noop; let g = () => noop; for (const x of [1, 2]) { g = () => f; f = second; } if (first()) {} return g();",
            call,
        ),
        (
            "let r = {}; if (first()) {} for (r of [0]) {} return r;",
            test,
        ),
        (
            "if (first()) {} for (const h of [second]) { return h; } return noop;",
            call,
        ),
        (
            "let f = noop; const set = () => { f = second; }; set(); return f;",
            call,
        ),
    ] {
        assert_reader_split_diverges(&format!("function flag() {{ {body} }}\n{run}"));
    }
}
