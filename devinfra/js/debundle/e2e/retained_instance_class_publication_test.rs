//! Retained local instances must not be confused with escaped class members.
use analysis::execution_evidence::ConstructionRejection;
use analysis::graph::EffectCellReport;
use analysis::reports::schema::{
    BindingEffectAuthorityReport, LexicalEffectRoot, RecordFactoryRejectionReason,
};
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;
use serde_json::Value as JsonValue;

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

const DIRECT_FIELD_READ_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const selected = first.callback;
const again = create({ value: 8 });
console.log(selected(), again.callback());
"#;

const ESCAPED_FIELD_READ_SOURCE: &str = r#"import { retain } from "./opaque.js";
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
retain(first);
const selected = first.callback;
const again = create({ value: 8 });
console.log(typeof selected, again.callback());
"#;

const GETTER_REPLACED_FIELD_SOURCE: &str = r#"let reads = 0;
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
Object.defineProperty(first, "callback", { configurable: true, get() { reads++; return () => 41; } });
const selected = first.callback;
console.log(selected(), reads);
"#;

const OVERWRITTEN_FIELD_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
first.callback = () => 41;
const selected = first.callback;
console.log(selected());
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

const BOUNDED_FALLBACK_PEER: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(...callbacks) {\n  for (const callback of callbacks) {\n    const result = callback();\n    if (typeof result === 'function') result();\n  }\n}\n",
)];

const CALLBACK_POOL_SIZE: usize = 70;

fn bounded_fallback_witness_source() -> String {
    let mut source = String::from(
        r#"import { host } from "./peer.js";
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const shelf = { nested: [first] };
const reader = (shelf => () => shelf.nested[0].callback)(shelf);
"#,
    );
    let fillers = (0..CALLBACK_POOL_SIZE)
        .map(|index| format!("() => {index}"))
        .collect::<Vec<_>>()
        .join(", ");
    source.push_str(&format!(
        "host(reader, {fillers});\nconst again = create({{ value: 8 }});\nconsole.log(first.callback(), again.callback());\n"
    ));
    source
}

const PROJECTED_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const projected = first.record.value;
const retained = [projected];
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(typeof selected[0], again.callback());
"#;

const ALIASED_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first, first];
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(selected[0](), selected[1](), again.callback());
"#;

const OPAQUE_MAPPED_RESULT_SOURCE: &str = r#"import { retain } from "./opaque.js";
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
const selected = retained.map(item => item.callback);
retain(selected);
const again = create({ value: 8 });
console.log(selected[0](), again.callback());
"#;

const INVOKED_MAPPED_RESULT_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
const selected = retained.map(item => item.callback);
const invoked = selected[0]();
const again = create({ value: 8 });
console.log(invoked, again.callback());
"#;

const MUTATED_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first, first];
const selected = retained.map((item, index) => {
  if (index === 0) item.callback = () => 10;
  return item.callback;
});
const again = create({ value: 8 });
console.log(selected[0](), selected[1](), again.callback());
"#;

const REPLACED_MAP_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
Array.prototype.map = function(callback) { globalThis.saved = this[0]; return [callback(this[0])]; };
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(selected[0](), again.callback());
"#;

const GETTER_SOURCE: &str = r#"globalThis.reads = 0;
class Box {
  get callback() { globalThis.reads++; return () => this.record.value; }
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first];
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(selected[0](), again.callback(), globalThis.reads);
"#;

const MUTATED_ARRAY_SOURCE: &str = r#"class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first, first];
const selected = retained.map((item, index) => {
  if (index === 0) retained[1] = { callback: () => 99 };
  return item.callback;
});
const again = create({ value: 8 });
console.log(selected[0](), selected[1](), again.callback());
"#;

const UNKNOWN_SOURCE: &str = r#"import { make } from "./opaque.js";
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first, make()];
const selected = retained.map(item => item.callback);
const again = create({ value: 8 });
console.log(selected[0](), selected[1](), again.callback());
"#;

const UNKNOWN_CHUNK: &str = "export function make() { return { callback: () => 19 }; }\n";

const MUTATED_CLASS_SOURCE: &str = r#"globalThis.seen = 0;
class Box {
  callback = () => this.record.value;
  constructor(record) { this.record = record; }
}
function create(record) { return new Box(record); }
const first = create({ value: 7 });
const retained = [first, first];
const selected = retained.map((item, index) => {
  if (index === 0) Object.setPrototypeOf(Box.prototype, {
    set record(value) { globalThis.seen = value.value; }
  });
  return item.callback;
});
const again = create({ value: 8 });
console.log(selected[0](), globalThis.seen);
"#;

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

fn assert_checked_instance_field_read(graph: &analysis::DependencyGraphReport, binding: &str) {
    let node = source_unit(graph, binding);
    let execution = node
        .synchronous_execution
        .as_ref()
        .expect("field read execution report");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        execution.binding_effect_authority.as_ref()
    else {
        panic!("{binding} lacks a checked operation receipt: {execution:#?}");
    };
    assert_eq!(receipt.owner, node.statement_ordinal, "{receipt:#?}");
    assert!(
        matches!(&receipt.root, LexicalEffectRoot::BindingInitializer { binding: name, .. } if name == binding),
        "{receipt:#?}"
    );
    let reads = &execution
        .heap_frontier
        .as_ref()
        .expect("field read heap evidence")
        .heap_access_provenance
        .executed_operations
        .reads;
    assert_eq!(reads.count, reads.sample.len(), "{reads:#?}");
    assert!(
        reads.sample.iter().any(|cell| matches!(
            cell,
            EffectCellReport::OwnProperty { property, .. } if property == "callback"
        )),
        "missing actual non-assumed callback field read: {reads:#?}"
    );
}

fn assert_no_checked_instance_field_read(graph: &analysis::DependencyGraphReport, binding: &str) {
    let execution = source_unit(graph, binding)
        .synchronous_execution
        .as_ref()
        .expect("field read execution report");
    assert!(
        !matches!(
            execution.binding_effect_authority.as_ref(),
            Some(BindingEffectAuthorityReport::OperationReceipt { .. })
        ),
        "{binding} acquired an unproved operation receipt: {execution:#?}"
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

fn class_members_escape_rejection<'a>(
    graph: &'a analysis::DependencyGraphReport,
    binding: &str,
) -> &'a analysis::reports::schema::LocalRecordFactoryRejection {
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
    rejection
}

#[test]
fn mapping_a_retained_instance_does_not_publish_class_members() {
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
    assert!(selected.complete, "{selected:#?}");
    assert!(selected.precision_losses.is_empty(), "{selected:#?}");

    assert_factory_complete(&graph, "again");
}

#[test]
fn checked_instance_own_field_read_keeps_later_construction_private() {
    let fixture = run_fixture(FixtureOpts::new(
        DIRECT_FIELD_READ_SOURCE,
        modules(&["Box", "create", "first", "selected", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "7 8\n");

    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("field read execution report");
    assert!(selected.complete, "{selected:#?}");
    assert!(selected.precision_losses.is_empty(), "{selected:#?}");
    assert_checked_instance_field_read(&graph, "selected");
    assert_factory_complete(&graph, "again");
}

#[test]
fn escaped_instance_cannot_supply_a_later_own_field_read() {
    let fixture = run_fixture(
        FixtureOpts::new(
            ESCAPED_FIELD_READ_SOURCE,
            vec![module(
                &["Box", "create", "first", "selected", "again"],
                &["retain(first);"],
            )],
        )
        .with_extra_chunks(&[("static/opaque", OPAQUE_CHUNK)]),
    );
    assert_preserved_entry_output(&fixture, "function 8\n");

    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("field read execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_no_checked_instance_field_read(&graph, "selected");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn replaced_instance_getter_runs_without_an_own_data_read_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        GETTER_REPLACED_FIELD_SOURCE,
        vec![module(
            &["reads", "Box", "create", "first", "selected"],
            &[
                "Object.defineProperty(first, \"callback\", { configurable: true, get() { reads++; return () => 41; } });",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41 1\n");
    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    assert_no_checked_instance_field_read(&graph, "selected");
}

#[test]
fn overwritten_instance_field_loses_its_constructor_read_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        OVERWRITTEN_FIELD_SOURCE,
        vec![module(
            &["Box", "create", "first", "selected"],
            &["first.callback = () => 41;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    assert_no_checked_instance_field_read(&graph, "selected");
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
    let publication = class_members_escape_rejection(&graph, "again")
        .first_class_members_publication
        .as_ref()
        .expect("first actual class-members publication");
    let publication = serde_json::to_value(publication).expect("serialize publication");
    assert_eq!(publication["writer"], "bounded_invocation_fallback");
    // Opaque callback analysis visits the stored arrow before recording
    // argument retention. Its unsupported read retains the actual receiver.
    assert_eq!(publication["top_level_owner"], 4, "{publication:#}");
    let callback = "() => this.record.value";
    let start = OPAQUE_SOURCE.find(callback).expect("receiver callback") + 1;
    assert_eq!(publication["span_start"], start, "{publication:#}");
    assert_eq!(
        publication["span_end"],
        start + callback.len(),
        "{publication:#}"
    );
    assert_eq!(
        publication["bounded_fallback_input_path"],
        serde_json::json!({
            "kind": "retained",
            "seed": {"kind": "captured_receiver"},
            "edges": [{"kind": "class_members_capability"}],
        }),
        "{publication:#}"
    );
}

#[test]
fn bounded_pool_fallback_reports_the_original_retained_input_path() {
    let source = bounded_fallback_witness_source();
    let host_statement = source
        .lines()
        .find(|line| line.starts_with("host("))
        .expect("opaque host call statement");
    let bindings = ["Box", "create", "first", "shelf", "reader", "again"];
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![module(&bindings, &[host_statement])])
            .with_extra_chunks(&BOUNDED_FALLBACK_PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7 8\n");

    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    let publication = class_members_escape_rejection(&graph, "again")
        .first_class_members_publication
        .as_ref()
        .expect("first actual class-members publication");
    let report: JsonValue = serde_json::to_value(publication).expect("serialize publication");
    assert_eq!(report["writer"], "bounded_invocation_fallback");
    assert!(report.get("span_start").is_none());
    assert!(report.get("span_end").is_none());

    let path = &report["bounded_fallback_input_path"];
    assert_eq!(path["kind"], "retained", "{path:#}");
    assert_eq!(path["seed"]["kind"], "argument", "{path:#}");
    assert_eq!(path["seed"]["position"], 0, "{path:#}");
    let edges = path["edges"].as_array().expect("typed retained edges");
    assert!(
        edges[0]["kind"] == "closure_snapshot" || edges[0]["kind"] == "closure_cell",
        "{path:#}"
    );
    assert_eq!(edges[0]["binding"], "shelf", "{path:#}");
    assert_eq!(edges[1]["kind"], "object_property", "{path:#}");
    assert_eq!(edges[1]["property"], "nested", "{path:#}");
    assert_eq!(edges[2]["kind"], "object_property", "{path:#}");
    assert_eq!(edges[2]["property"], "0", "{path:#}");
    assert_eq!(edges[3]["kind"], "class_members_capability", "{path:#}");

    let callsite = &report["bounded_fallback_callsite"];
    assert!(callsite.is_object(), "{report:#}");
    assert!(callsite["top_level_owner"].is_number(), "{callsite:#}");
    let start = u32::try_from(
        callsite["span_start"]
            .as_u64()
            .expect("bounded fallback call-site span start"),
    )
    .expect("span start fits u32");
    let end = u32::try_from(
        callsite["span_end"]
            .as_u64()
            .expect("bounded fallback call-site span end"),
    )
    .expect("span end fits u32");
    let start = start
        .checked_sub(1)
        .expect("SWC source starts at BytePos(1)") as usize;
    let end = end.checked_sub(1).expect("SWC source starts at BytePos(1)") as usize;
    assert!(start < end, "{callsite:#}");
    assert!(source[start..end].contains("host("), "{callsite:#}");
}

#[test]
fn projected_primitive_does_not_gain_constructed_instance_kind() {
    let fixture = run_fixture(FixtureOpts::new(
        PROJECTED_SOURCE,
        modules(&[
            "Box",
            "create",
            "first",
            "projected",
            "retained",
            "selected",
            "again",
        ]),
    ));
    assert_preserved_entry_output(&fixture, "undefined 8\n");
    let graph = fixture.dependency_graph();
    assert_factory_complete(&graph, "first");
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn repeated_aliases_are_iterated_without_claiming_unique_instances() {
    let fixture = run_fixture(FixtureOpts::new(
        ALIASED_SOURCE,
        modules(&["Box", "create", "first", "retained", "selected", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "7 7 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(selected.complete, "{selected:#?}");
    assert_factory_complete(&graph, "again");
}

#[test]
fn opaque_consumer_of_mapped_callbacks_still_publishes_class_capabilities() {
    let fixture = run_fixture(
        FixtureOpts::new(
            OPAQUE_MAPPED_RESULT_SOURCE,
            vec![module(
                &["Box", "create", "first", "retained", "selected", "again"],
                &["retain(selected);"],
            )],
        )
        .with_extra_chunks(&[("static/opaque", OPAQUE_CHUNK)]),
    );
    assert_preserved_entry_output(&fixture, "7 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn invoking_an_opaque_mapped_element_preserves_callback_reach() {
    let fixture = run_fixture(FixtureOpts::new(
        INVOKED_MAPPED_RESULT_SOURCE,
        modules(&[
            "Box", "create", "first", "retained", "selected", "invoked", "again",
        ]),
    ));
    assert_preserved_entry_output(&fixture, "7 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(selected.complete, "{selected:#?}");
    let invoked = source_unit(&graph, "invoked")
        .synchronous_execution
        .as_ref()
        .expect("element invocation report");
    assert!(!invoked.complete, "{invoked:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn mutation_during_iteration_invalidates_the_instance_read_proof() {
    let fixture = run_fixture(FixtureOpts::new(
        MUTATED_SOURCE,
        modules(&["Box", "create", "first", "retained", "selected", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "10 10 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    let again = source_unit(&graph, "again")
        .synchronous_execution
        .as_ref()
        .expect("later factory report");
    assert!(
        !again.local_record_factory_rejections.is_empty(),
        "{again:#?}"
    );
}

#[test]
fn replaced_array_map_keeps_the_opaque_callback_path() {
    let fixture = run_fixture(FixtureOpts::new(
        REPLACED_MAP_SOURCE,
        vec![module(
            &["Box", "create", "first", "retained", "selected", "again"],
            &[
                "Array.prototype.map = function(callback) { globalThis.saved = this[0]; return [callback(this[0])]; };",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn instance_getter_runs_and_refuses_the_own_data_read_proof() {
    let fixture = run_fixture(FixtureOpts::new(
        GETTER_SOURCE,
        vec![module(
            &["Box", "create", "first", "retained", "selected", "again"],
            &["globalThis.reads = 0;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 8 2\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn array_slot_mutation_during_iteration_invalidates_the_source_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        MUTATED_ARRAY_SOURCE,
        modules(&["Box", "create", "first", "retained", "selected", "again"]),
    ));
    assert_preserved_entry_output(&fixture, "7 99 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn unknown_array_slot_prevents_instance_only_iteration() {
    let fixture = run_fixture(
        FixtureOpts::new(
            UNKNOWN_SOURCE,
            modules(&["Box", "create", "first", "retained", "selected", "again"]),
        )
        .with_extra_chunks(&[("static/opaque", UNKNOWN_CHUNK)]),
    );
    assert_preserved_entry_output(&fixture, "7 19 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    assert_class_members_escape_refusal(&graph, "again");
}

#[test]
fn class_prototype_mutation_during_iteration_revokes_constructor_proof() {
    let fixture = run_fixture(FixtureOpts::new(
        MUTATED_CLASS_SOURCE,
        vec![module(
            &["Box", "create", "first", "retained", "selected", "again"],
            &["globalThis.seen = 0;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 8\n");
    let graph = fixture.dependency_graph();
    let selected = source_unit(&graph, "selected")
        .synchronous_execution
        .as_ref()
        .expect("map execution report");
    assert!(!selected.complete, "{selected:#?}");
    let again = source_unit(&graph, "again")
        .synchronous_execution
        .as_ref()
        .expect("later factory report");
    assert!(
        !again.local_record_factory_rejections.is_empty(),
        "{again:#?}"
    );
}
