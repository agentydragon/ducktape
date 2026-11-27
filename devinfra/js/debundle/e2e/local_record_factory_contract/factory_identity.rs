//! Annotated constructor factories admit fresh records and report the first identity or escape guard.

use super::{
    FACTORY, FRESH_RECORD, HOLDER, execution, fixture_opts, fixture_source, harness_members,
    members,
};
use analysis_vocabulary::report_schema::RecordFactoryRejectionReason;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

fn local_factory_result_rejection(
    source: &str,
    anonymous_statements: &[&str],
) -> (RecordFactoryRejectionReason, Option<usize>) {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&[]),
            anonymous_statements,
        )],
    ));
    assert_preserved_entry_output(&fixture, "169 0\n");
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(rejection.factory_binding, "create");
    assert!(rejection.source_location.is_some());
    (
        rejection.reason,
        rejection
            .first_callback_publication_owner
            .map(|owner| owner.0),
    )
}

#[test]
fn annotation_admits_a_direct_fresh_record_to_the_checked_constructor_factory() {
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let fixture = run_fixture_with_graphs(fixture_opts(&source, true, &[], &[], &[]));

    assert_preserved_entry_output(&fixture, "169 0\n");
    let graph = fixture.dependency_graph();
    let uses: Vec<_> = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|report| &report.local_record_factory_uses)
        .collect();
    assert_eq!(uses.len(), 1);
    assert_eq!(uses[0].factory_binding, "create");
    assert!(
        uses[0]
            .prototype_properties
            .iter()
            .any(|property| property == "weight")
    );
    assert!(uses[0].source_location.is_some());
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["command"],
        &["later"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["class Holder", "function create", "const command"],
        &[],
    );
}

#[test]
fn factory_identity_refusals_report_the_first_guard() {
    let escape = "globalThis.saved = create;";
    let reassign = "create = function (record) { return new Holder(record, () => later); };";
    let exported_factory = FACTORY.replace("function create", "export function create");
    for (factory, statement, expected) in [
        (
            FACTORY,
            Some(escape),
            RecordFactoryRejectionReason::FactoryEscaped,
        ),
        (
            exported_factory.as_str(),
            None,
            RecordFactoryRejectionReason::FactoryExported,
        ),
        (
            FACTORY,
            Some(reassign),
            RecordFactoryRejectionReason::FactoryFlowIdentity,
        ),
    ] {
        let before_command = statement.map_or(String::new(), |statement| format!("{statement}\n"));
        let source = fixture_source(
            "",
            factory,
            &before_command,
            &format!("create({FRESH_RECORD})"),
            "",
            "console.log(command.run(), command.weight);",
        );
        let (reason, publication_owner) =
            local_factory_result_rejection(&source, statement.as_slice());
        assert_eq!(reason, expected);
        assert_eq!(publication_owner, None, "{expected:?}");
    }
}

#[test]
fn escaped_class_members_report_the_first_global_member_write() {
    use analysis_vocabulary::execution_evidence::ConstructionRejection;
    use analysis_vocabulary::report_schema::ClassMembersPublicationWriter;

    let factory = FACTORY.replace("() => later", "() => 42");
    let source = format!(
        "{HOLDER}{factory}const first = create({{}});\nglobalThis.saved = first;\nconst command = create({{}});\nconsole.log(typeof command.run);\n"
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &members("create", &["Holder", "create", "first", "command"]),
            &[
                "globalThis.saved = first;",
                "console.log(typeof command.run);",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function\n");

    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::ConstructorProof
    );
    assert_eq!(
        rejection.construction,
        Some(ConstructionRejection::ClassMembersEscaped)
    );
    let publication = rejection
        .first_class_members_publication
        .as_ref()
        .expect("first actual class-members publication");
    assert_eq!(
        publication.writer,
        ClassMembersPublicationWriter::GlobalOrIntrinsicMemberWrite
    );
    let publication_owner = publication.top_level_owner.expect("publication owner");
    let source_offset = |position: Option<u32>| {
        position
            .expect("publication span")
            .checked_sub(1)
            .expect("SWC source starts at BytePos(1)") as usize
    };
    let start = source_offset(publication.span_start);
    let end = source_offset(publication.span_end);
    assert!(start < end);
    assert_eq!(&source[start..end], "globalThis.saved = first");
    let publication_node = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal == publication_owner)
        .expect("publication owner source unit");
    let publication_location = publication
        .source_location
        .as_ref()
        .expect("publication source location");
    assert_eq!(
        publication_node.source_location.as_ref(),
        Some(publication_location)
    );
    assert_eq!(
        publication_location.start_line,
        source
            .lines()
            .position(|line| line == "globalThis.saved = first;")
            .expect("publication line")
            + 1
    );
}
