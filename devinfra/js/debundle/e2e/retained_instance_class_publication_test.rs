//! Retained local instances must not be confused with escaped class members.
use analysis::reports::schema::{ConstructionRejection, RecordFactoryRejectionReason};
use debundle_e2e_support::*;

const MAP_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(selected[0](), again.callback());
"#;

const LOCAL_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
const again = create({ value: 8 });
console.log("ready");
"#;

const OPAQUE_SOURCE: &str = r#"import { retain } from "./opaque.js";
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
retain(first);
const again = create({ value: 8 });
console.log("ready");
"#;

const OPAQUE_CHUNK: &str = "export function retain(value) { globalThis.saved = value; }\n";

fn module(bindings: &[&'static str], statements: &[&str]) -> LogicalModuleEntry {
    let members = bindings
        .iter()
        .map(|name| {
            if *name == "create" {
                Member::new(name).with_retained_record_factory()
            } else {
                Member::new(name)
            }
        })
        .collect::<Vec<_>>();
    if statements.is_empty() {
        logical_module("all", &members)
    } else {
        logical_module_with_anon("all", &members, statements)
    }
}

fn modules(bindings: &[&'static str]) -> Vec<LogicalModuleEntry> {
    vec![module(bindings, &[])]
}

fn source_unit<'a>(
    graph: &'a analysis::DependencyGraphReport,
    binding: &str,
) -> &'a analysis::reports::schema::SourceUnitReport {
    graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("missing source unit for {binding}"))
}

fn assert_factory_complete(graph: &analysis::DependencyGraphReport, binding: &str) {
    let execution = source_unit(graph, binding)
        .synchronous_execution
        .as_ref()
        .expect("factory execution report");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    assert!(
        execution.local_record_factory_rejections.is_empty(),
        "{execution:#?}"
    );
}

fn assert_class_members_escape_refusal(graph: &analysis::DependencyGraphReport, binding: &str) {
    let execution = source_unit(graph, binding)
        .synchronous_execution
        .as_ref()
        .expect("later factory execution report");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected the later factory proof to be refused: {execution:#?}");
    };
    assert_eq!(rejection.factory_binding, "create");
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::ConstructorProof
    );
    assert_eq!(
        rejection.construction,
        Some(ConstructionRejection::ClassMembersEscaped)
    );
}

#[test]
fn mapping_a_retained_instance_publishes_class_members_conservatively() {
    let fixture = run_fixture(FixtureOpts::new(
        MAP_SOURCE,
        modules(&["Box", "create", "first", "retained", "selected", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "7 8\n");

    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");

    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert!(!selected.precision_losses.is_empty(), "{selected:#?}");

    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn locally_retained_instance_does_not_publish_class_members() {
    let fixture = run_fixture(FixtureOpts::new(
        LOCAL_SOURCE,
        modules(&["Box", "create", "first", "retained", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "ready\n");

    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    assert_factory_complete(&graph, "again");
}

#[test]
fn opaque_consumer_of_an_instance_still_blocks_later_construction() {
    let fixture = run_fixture(
        FixtureOpts::new(
            OPAQUE_SOURCE,
            vec![module(
                &["Box", "create", "first", "again"],
                &["retain(first);"],
            )],
        )
        .with_extra_chunks(&[("static/opaque", OPAQUE_CHUNK)]),
    );
    assert_preserved_entry_output(&fixture, "ready\n");

    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    assert_class_members_escape_refusal(&graph, "again");
}
