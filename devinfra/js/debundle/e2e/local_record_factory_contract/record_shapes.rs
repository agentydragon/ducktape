//! Record shapes are admitted as retained callbacks or closed inputs, or refused with a typed reason.

use super::{
    FACTORY, FRESH_RECORD, assert_atomic_fallback, execution, fixture_opts, fixture_source,
    harness_members,
};
use analysis_vocabulary::report_schema::RecordFactoryRejectionReason;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

/// Runs the call unsplit (so the gate accepts) and returns the typed reason
/// the annotated call's record was refused for.
fn record_rejection(
    before_command: &str,
    call: &str,
) -> (RecordFactoryRejectionReason, Option<String>) {
    let source = fixture_source(
        "",
        FACTORY,
        before_command,
        call,
        "",
        "console.log(typeof command.run);",
    );
    let extra: Vec<_> = ["handler", "label"]
        .into_iter()
        .filter(|name| before_command.contains(&format!("const {name} ")))
        .collect();
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&extra),
            &["console.log(typeof command.run);"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function\n");
    let graph = fixture.dependency_graph();
    let [rejection] = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|report| &report.local_record_factory_rejections)
        .collect::<Vec<_>>()[..]
    else {
        panic!("expected one record-factory rejection");
    };
    assert_eq!(rejection.factory_binding, "create");
    assert!(rejection.source_location.is_some());
    (rejection.reason, rejection.property.clone())
}

#[test]
fn proven_record_values_are_admitted() {
    const RUN_AND_WEIGHT: &str = "console.log(command.run(), command.weight);";
    const EXECUTE: &str = "execute: () => later,";
    for (case, before_command, replaced, replacement, callback_members, log, output) in [
        (
            "method shorthand is a retained callback",
            "",
            EXECUTE,
            "execute() { return later; },",
            &[][..],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "function declaration is a retained callback",
            "function handler() { return later; }\n",
            EXECUTE,
            "execute: handler,",
            &["handler"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "const arrow is a retained callback",
            "const handler = () => later;\n",
            EXECUTE,
            "execute: handler,",
            &["handler"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "shorthand property names a retained callback",
            "const execute = () => later;\n",
            EXECUTE,
            "execute,",
            &["execute"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "shared primitive array is a closed input",
            "const modes = [Flags.FIRST, 2, \"ready\"];\n",
            EXECUTE,
            "execute: () => later,\n  modes,",
            &["modes"],
            "console.log(command.record.modes[0], command.run());",
            "1 169\n",
        ),
        (
            "shared primitive record is a closed input",
            "const modes = { first: Flags.FIRST, label: \"ready\" };\n",
            EXECUTE,
            "execute: () => later,\n  modes,",
            &["modes"],
            "console.log(command.record.modes.first, command.run());",
            "1 169\n",
        ),
        (
            "template literal of proven primitives",
            "const label = \"Example\";\n",
            EXECUTE,
            "execute: () => later,\n  name: `${label}-${Flags.SECOND}-${1}`,",
            &["label"],
            "console.log(command.record.name, command.run());",
            "Example-2-1 169\n",
        ),
        (
            "logical not of a literal",
            "",
            "enabled: true",
            "enabled: !false",
            &[],
            "console.log(command.record.nested.enabled, command.run());",
            "true 169\n",
        ),
    ] {
        eprintln!("case: {case}");
        let record = FRESH_RECORD.replace(replaced, replacement);
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            &format!("create({record})"),
            "",
            log,
        );
        let fixture =
            run_fixture_with_graphs(fixture_opts(&source, true, callback_members, &[], &[]));
        assert_preserved_entry_output(&fixture, output);
        let graph = fixture.dependency_graph();
        let reports: Vec<_> = graph
            .nodes
            .iter()
            .filter_map(|node| node.synchronous_execution.as_ref())
            .collect();
        assert_eq!(
            reports
                .iter()
                .map(|report| report.local_record_factory_uses.len())
                .sum::<usize>(),
            1,
            "{case}"
        );
        assert!(
            reports
                .iter()
                .all(|report| report.local_record_factory_rejections.is_empty()),
            "{case}"
        );
        let command = node_declaring(&graph, "command");
        assert!(command.purity.is_pure(), "{case}: {:?}", command.purity);
        let execution = execution(&graph, "command");
        assert!(execution.complete, "{case}: {execution:#?}");
        assert_eq!(execution.local_record_factory_uses.len(), 1, "{case}");
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/callbacks/wrapped.js",
            &["command"],
            &["later"],
        );
    }
}

#[test]
fn logical_not_does_not_hide_an_effectful_constructor_operand() {
    let record = FRESH_RECORD.replace("enabled: true", "enabled: !effect()");
    let before_command =
        "const state = { count: 0 };\nfunction effect() { state.count++; return false; }\n";
    let source = fixture_source(
        "",
        FACTORY,
        before_command,
        &format!("create({record})"),
        "",
        "console.log(command.record.nested.enabled, state.count, command.run());",
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&["state", "effect"]),
            &["console.log(command.record.nested.enabled, state.count, command.run());"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "true 1 169\n");

    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected the effectful operand to refuse the record-factory proof: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::UnsupportedValue
    );
}

#[test]
fn unproven_values_in_admitted_positions_fall_back() {
    for (before_command, call, callback_members) in [
        // A method under a computed key.
        (
            "const key = \"execute\";\n",
            "create({ [key]() { return later; } })",
            vec!["key"],
        ),
        // A reassigned function binding.
        (
            "let handler = () => later;\nhandler = () => later + 1;\n",
            "create({ execute: handler })",
            vec!["handler"],
        ),
        // An object binding, whose identity is shared and mutable.
        (
            "const handler = { valueOf() { return later; } };\n",
            "create({ execute: handler })",
            vec!["handler"],
        ),
        // A function binding initialized only after the call (TDZ).
        ("", "create({ execute: handler })", vec![]),
        // A shared array holding a callback.
        (
            "const handler = [() => later];\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // A shared array given a callback before the call.
        (
            "const handler = [1];\nhandler.push(() => later);\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // A shared record exposed to unknown code before the call.
        (
            "const handler = { mode: 1 };\nglobalThis.saved = handler;\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // `ToString` of an object runs its `toString`.
        (
            "const label = { toString() { return String(later); } };\n",
            "create({ execute: () => later, name: `${label}` })",
            vec!["label"],
        ),
        // A proxy can intercept the conversion's property reads.
        (
            "const label = new Proxy({}, { get: () => () => later });\n",
            "create({ execute: () => later, name: `${label}` })",
            vec!["label"],
        ),
        // A substitution evaluated by a call.
        (
            "",
            "create({ execute: () => later, name: `${String(1)}` })",
            vec![],
        ),
        // A getter-backed member read.
        (
            "const label = { get value() { return later; } };\n",
            "create({ execute: () => later, name: `${label.value}` })",
            vec!["label"],
        ),
    ] {
        let after = if callback_members.is_empty() && call.contains("handler") {
            "const handler = () => later;\n"
        } else {
            ""
        };
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            after,
            "console.log(command.run());",
        );
        let later_members: &[&str] = if after.is_empty() { &[] } else { &["handler"] };
        let statements: Vec<_> = before_command
            .lines()
            .filter(|line| !line.starts_with("const ") && !line.starts_with("let "))
            .collect();
        assert_atomic_fallback(&source, &callback_members, later_members, &statements);
    }
}

#[test]
fn refused_record_shapes_report_a_typed_reason() {
    for (before_command, call, reason, property) in [
        (
            "",
            "create({ ...{ execute: () => later } })",
            RecordFactoryRejectionReason::Spread,
            None,
        ),
        (
            "",
            "create({ execute: () => later, get name() { return \"x\"; } })",
            RecordFactoryRejectionReason::Accessor,
            None,
        ),
        (
            "const label = { toString() { return \"x\"; } };\n",
            "create({ execute: () => later, name: `${label}` })",
            RecordFactoryRejectionReason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: `${String(1)}` })",
            RecordFactoryRejectionReason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "const handler = { execute: () => later };\n",
            "create({ execute: () => later, nested: handler })",
            RecordFactoryRejectionReason::UnprovenBinding,
            Some("nested"),
        ),
        (
            "const label = { value: \"x\" };\n",
            "create({ execute: () => later, name: label.value })",
            RecordFactoryRejectionReason::UnprovenMemberRead,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: String(1) })",
            RecordFactoryRejectionReason::UnsupportedValue,
            Some("name"),
        ),
    ] {
        assert_eq!(
            record_rejection(before_command, call),
            (reason, property.map(str::to_owned)),
            "{call}"
        );
    }
}
