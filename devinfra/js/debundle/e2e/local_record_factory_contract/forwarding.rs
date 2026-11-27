//! Forwarding wrappers keep nested factory results private unless the caller suffix publishes them.

use super::{FACTORY, FRESH_RECORD, execution, fixture_source, harness_members};
use analysis_vocabulary::report_schema::RecordFactoryRejectionReason;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

const TERMINAL_FORWARDING: &str = "const make = label => create({ label: () => label, values: [Flags.FIRST], execute: async () => {} });\nconst first = make(\"a\");\nconst second = make(\"b\");\n";

#[test]
fn terminal_forwarding_keeps_nested_factory_results_private() {
    let fixture = forwarding_fixture(FACTORY, TERMINAL_FORWARDING, &[]);
    let graph = fixture.dependency_graph();
    for binding in ["first", "second", "command"] {
        let execution = execution(&graph, binding);
        assert!(execution.complete, "{binding}: {execution:#?}");
        assert!(
            execution.precision_losses.is_empty(),
            "{binding}: {execution:#?}"
        );
    }
    let command = node_declaring(&graph, "command");
    assert!(command.purity.is_pure(), "{:?}", command.purity);
    assert_eq!(
        execution(&graph, "command").local_record_factory_uses.len(),
        1
    );
}

fn forwarding_fixture(
    factory: &str,
    before_command: &str,
    additional_statements: &[&str],
) -> Fixture {
    let source = fixture_source(
        "",
        factory,
        before_command,
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let mut statements = vec!["console.log(command.run(), command.weight);"];
    statements.extend_from_slice(additional_statements);
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&["make", "first", "second"]),
            &statements,
        )],
    ));
    assert_preserved_entry_output(&fixture, "169 0\n");
    fixture
}

#[test]
fn nonterminal_forwarding_retains_publication_from_the_caller_suffix() {
    let before_command = "const make = label => { const result = create({ label: () => label, values: [Flags.FIRST], execute: async () => {} }); return result; };\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    let private = forwarding_fixture(FACTORY, before_command, &[]);
    let graph = private.dependency_graph();
    let execution = execution(&graph, "command");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let publishing_suffix = "const make = label => { const result = create({ label: () => label, values: [Flags.FIRST], execute: async () => {} }); globalThis.retained = create; return result; };\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    assert_forwarding_factory_refused(&forwarding_fixture(FACTORY, publishing_suffix, &[]));
}

#[test]
fn terminal_forwarding_preserves_real_factory_escapes() {
    let publishing_factory = "function create(record) { globalThis.retained = create; return new Holder(record, () => later); }\n";
    assert_forwarding_factory_refused(&forwarding_fixture(
        publishing_factory,
        TERMINAL_FORWARDING,
        &[],
    ));

    let prior_escape = "globalThis.retained = create;\n".to_owned() + TERMINAL_FORWARDING;
    assert_forwarding_factory_refused(&forwarding_fixture(
        FACTORY,
        &prior_escape,
        &["globalThis.retained = create;"],
    ));
}

fn assert_forwarding_factory_refused(fixture: &Fixture) {
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    assert!(
        execution.local_record_factory_uses.is_empty(),
        "{execution:#?}"
    );
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::FactoryEscaped,
        "{execution:#?}"
    );
}
