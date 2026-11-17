//! Checked activation-local stores compose effects through the ordered kernel.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

#[test]
fn local_assignment_receipts_allow_an_independent_statement_inversion() {
    for body in [
        "let value = 1; value = 2; return value;",
        "let value; return (value = 2);",
        "let value = 1; return (value = 2);",
        "let value = 1; value = (value = 2); return value;",
        "let value = 1; value = value + 1; return value;",
    ] {
        let source = format!(
            "function compute() {{ {body} }}\nconst independent = 7;\nvar result = compute();\nconsole.log(independent, result);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "compute",
                    &binding_members(["compute", "result"]),
                )],
            )
            .with_import_order(&["compute", "residual"])
            .with_graphs(),
        );
        assert_preserved_entry_output(&fixture, "7 2\n");
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        assert_effect_verdict_accepts(&fixture, body);
    }
}

#[test]
fn initialized_parameter_assignments_compose_the_same_checked_store_receipt() {
    for parameters in ["value", "value = 1"] {
        let source = format!(
            "function compute({parameters}) {{ value = 2; return value; }}\nconst independent = 7;\nvar result = compute();\nconsole.log(independent, result);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "compute",
                    &binding_members(["compute", "result"]),
                )],
            )
            .with_import_order(&["compute", "residual"])
            .with_graphs(),
        );
        assert_preserved_entry_output(&fixture, "7 2\n");
        assert_inline_operation_receipt(&fixture, "result", 1, parameters);
        assert_effect_verdict_accepts(&fixture, parameters);
    }
}

#[test]
fn assignments_transport_actual_callable_selection_without_manufacturing_it() {
    for (selected, authority) in [
        ("readSeed", "operation_receipt"),
        ("alias", "ordered_root_initializer_uncovered"),
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ return seed; }}\nconst alias = {{ run: readSeed }}.run;\nfunction invoke() {{ let selected; selected = {selected}; return selected(); }}\nvar result = invoke();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &binding_members(["seed", "readSeed", "alias", "invoke", "result"]),
                )],
            )
            .with_import_order(&["all"])
            .with_graphs(),
        );
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], authority,
            "a store transports actual selection, not evidence inferred from an equal value: {selected}: {execution:#}"
        );
    }
}

#[test]
fn assignment_receipts_preserve_abrupt_stores_and_right_hand_sides() {
    for (body, error) in [
        ("value = 2; let value; return value;", "ReferenceError"),
        ("const value = 1; value = 2; return value;", "TypeError"),
        (
            "let value = 1; value = later; let later; return value;",
            "ReferenceError",
        ),
        ("let value = 1; value = fail(); return value;", "rhs failed"),
    ] {
        let source = format!(
            "function fail() {{ throw Error('rhs failed'); }}\nfunction compute() {{ {body} }}\nvar result = compute();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &binding_members(["fail", "compute", "result"]),
                )],
            )
            .with_import_order(&["all"])
            .with_graphs(),
        );
        assert_preserved_entry_error(&fixture, error);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "an abrupt store or operand cannot seal normal coverage: {body}: {execution:#}"
        );
    }
}

#[test]
fn assignment_to_a_later_parameter_in_its_tdz_cannot_seal_normal_coverage() {
    let fixture = run_fixture(FixtureOpts::new(
        "function compute(value = (later = 1), later = 2) { return value; }\nvar result = compute();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &binding_members(["compute", "result"]),
        )],
    ).with_import_order(&["all"]).with_graphs());
    assert_preserved_entry_error(&fixture, "ReferenceError");
    let execution = binding_execution_report(&fixture, "result");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an uninitialized parameter is not an activation-local mutable store: {execution:#}"
    );
}

#[test]
fn assignments_do_not_cover_captured_cells_or_effectful_operands() {
    for (body, expected) in [
        (
            "let value = 1; const read = () => value; value = 2; return read();",
            "2 0\n",
        ),
        ("let value = 1; value = observe(); return value;", "2 1\n"),
    ] {
        let source = format!(
            "let calls = 0;\nfunction observe() {{ calls++; return 2; }}\nfunction compute() {{ {body} }}\nvar result = compute();\nconsole.log(result, calls);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &binding_members(["calls", "observe", "compute", "result"]),
                )],
            )
            .with_import_order(&["all"])
            .with_graphs(),
        );
        assert_preserved_entry_output(&fixture, expected);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "the store cannot discharge other missing coverage: {body}: {execution:#}"
        );
    }
}
