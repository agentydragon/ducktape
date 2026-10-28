//! Observed consumer evidence from the real `modules propose` command.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};

fn proposal_for_binding<'a>(report: &'a Value, binding: &str) -> &'a Value {
    report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|proposal| {
            proposal["binding_ids"]
                .as_array()
                .is_some_and(|ids| ids.contains(&json!(binding)))
        })
        .unwrap_or_else(|| panic!("proposal for {binding:?} missing from {report:#}"))
}

fn source_unit_id_for_binding(fixture: &GraphFixture, binding: &str) -> String {
    fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding.as_str() == binding)
        })
        .unwrap_or_else(|| panic!("source unit for {binding:?} missing"))
        .id
}

fn source_unit_id_for_line(fixture: &GraphFixture, line: usize) -> String {
    fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.source_location
                .as_ref()
                .is_some_and(|location| location.start_line == line)
        })
        .unwrap_or_else(|| panic!("source unit on line {line} missing"))
        .id
}

fn current_module_for_source_unit(fixture: &GraphFixture, source_unit_id: &str) -> String {
    let graph = fixture.dependency_graph();
    let source_unit = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_id)
        .unwrap();
    graph
        .module(&source_unit.destination)
        .unwrap()
        .path
        .to_string()
}

fn proposals(fixture: &GraphFixture) -> Value {
    proposals_with_cap(fixture, 1)
}

fn proposals_with_cap(fixture: &GraphFixture, size_cap_lines: usize) -> Value {
    let size_cap_lines = size_cap_lines.to_string();
    fixture.json(&["modules", "propose", "--size-cap-lines", &size_cap_lines])
}

fn assert_single_unit_proposal(proposal: &Value, source_unit_id: &str) {
    assert_eq!(proposal["source_unit_ids"], json!([source_unit_id]));
    assert_eq!(proposal["status"], "peelable_now");
}

#[test]
fn reports_the_sole_value_reader_without_changing_the_proposal() {
    let fixture = GraphFixture::new(
        "const primitive = 7;\nconst onlyConsumer = primitive + 1;\nconsole.log(onlyConsumer);\n",
        &[],
    );
    let report = proposals(&fixture);
    let primitive_id = source_unit_id_for_binding(&fixture, "primitive");
    let consumer_id = source_unit_id_for_binding(&fixture, "onlyConsumer");
    let proposal = proposal_for_binding(&report, "primitive");
    assert_single_unit_proposal(proposal, &primitive_id);

    let reads = &proposal["observed_consumers"]["value_reads"];
    assert_eq!(reads["total_source_units"], 1);
    assert_eq!(reads["source_units"][0]["source_unit_id"], consumer_id);
    assert_eq!(
        reads["source_units"][0]["current_module"],
        current_module_for_source_unit(&fixture, &consumer_id)
    );
    assert_eq!(reads["total_modules"], 1);
    assert_eq!(reads["total_edges"], 1);
    assert_eq!(reads["edges"][0]["source"], consumer_id);
    assert_eq!(reads["edges"][0]["target"], primitive_id);
    assert_eq!(reads["edges"][0]["edge_kind"], "eager_use");
    assert_eq!(
        proposal["observed_consumers"]["scheduling_or_promoted_context"]["total_edges"],
        0
    );
    fixture.assert_runs("8\n");
}

#[test]
fn excludes_an_existing_extension_member_from_external_consumers() {
    let fixture = GraphFixture::rejected(
        "const primitive = 7;\nconst onlyConsumer = primitive + 1;\nconsole.log(onlyConsumer);\n",
        &[(
            "features/consumer.yaml",
            "members: [{selector: {binding: {name: onlyConsumer}}}]\n",
        )],
    );
    let report = proposals_with_cap(&fixture, 10);
    let primitive_id = source_unit_id_for_binding(&fixture, "primitive");
    let proposal = proposal_for_binding(&report, "primitive");

    assert!(
        proposal["source_unit_ids"]
            .as_array()
            .is_some_and(|ids| ids.contains(&json!(primitive_id))),
        "extension proposal omitted its primitive target: {proposal:#}"
    );
    assert_eq!(proposal["extends_module_id"], "features/consumer");
    assert_eq!(
        proposal["observed_consumers"]["value_reads"]["total_source_units"],
        0
    );
}

#[test]
fn distinguishes_a_shared_primitive_from_a_sole_reader() {
    let fixture = GraphFixture::new(
        "const primitive = 3;\nconst first = primitive + 1;\nconst second = primitive + 2;\nconsole.log(first, second);\n",
        &[],
    );
    let report = proposals(&fixture);
    let primitive_id = source_unit_id_for_binding(&fixture, "primitive");
    let first_id = source_unit_id_for_binding(&fixture, "first");
    let second_id = source_unit_id_for_binding(&fixture, "second");
    let proposal = proposal_for_binding(&report, "primitive");
    assert!(
        proposal["source_unit_ids"]
            .as_array()
            .is_some_and(|ids| ids.contains(&json!(primitive_id))),
        "the current proposal no longer includes the primitive source unit: {proposal:#}"
    );

    let reads = &proposal["observed_consumers"]["value_reads"];
    assert_eq!(reads["total_source_units"], 2);
    assert_eq!(
        reads["source_units"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["source_unit_id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [first_id.as_str(), second_id.as_str()]
    );
    assert_eq!(reads["total_edges"], 2);
    fixture.assert_runs("4 5\n");
}

#[test]
fn source_adjacency_without_a_dependency_is_not_a_consumer() {
    let fixture = GraphFixture::new(
        "const primitive = 1;\nconst unrelated = 2;\nconsole.log(unrelated);\n",
        &[],
    );
    let report = proposals(&fixture);
    let primitive_id = source_unit_id_for_binding(&fixture, "primitive");
    let proposal = proposal_for_binding(&report, "primitive");
    assert_single_unit_proposal(proposal, &primitive_id);

    let reads = &proposal["observed_consumers"]["value_reads"];
    assert_eq!(reads["total_source_units"], 0);
    assert_eq!(reads["total_modules"], 0);
    assert_eq!(reads["total_edges"], 0);
    assert_eq!(reads["source_units"], json!([]));
    assert_eq!(reads["edges"], json!([]));
    fixture.assert_runs("2\n");
}

#[test]
fn cross_owner_write_is_not_reported_for_added_merge_units() {
    let fixture = GraphFixture::rejected(
        "let primitive = 0;\nfunction update() { primitive = 1; }\nupdate();\nconsole.log(primitive);\n",
        &[
            (
                "owners/primitive.yaml",
                "members: [{selector: {binding: {name: primitive}}}]\n",
            ),
            (
                "owners/update.yaml",
                "members: [{selector: {binding: {name: update}}}]\n",
            ),
        ],
    );
    let report = proposals_with_cap(&fixture, 10);
    let primitive_id = source_unit_id_for_binding(&fixture, "primitive");
    let update_id = source_unit_id_for_binding(&fixture, "update");
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|proposal| {
            proposal["merge_into"]
                .as_array()
                .is_some_and(|modules| modules.contains(&json!("owners/primitive")))
        })
        .unwrap_or_else(|| panic!("merge proposal for primitive is missing: {report:#}"));
    assert_eq!(
        proposal["merge_into"],
        json!(["owners/primitive", "owners/update"])
    );
    assert_eq!(proposal["extends_module_id"], "owners/primitive");
    assert_eq!(
        current_module_for_source_unit(&fixture, &update_id),
        "owners/update"
    );
    assert_ne!(
        current_module_for_source_unit(&fixture, &primitive_id),
        current_module_for_source_unit(&fixture, &update_id)
    );

    let graph = fixture.dependency_graph();
    let write = graph
        .edges
        .iter()
        .find(|edge| edge.source == update_id && edge.target == primitive_id)
        .unwrap_or_else(|| panic!("cross-owner write edge missing: {graph:#?}"));
    assert!(matches!(
        write.edge_kind,
        analysis::DepKind::EagerRebind
            | analysis::DepKind::LazyRebind
            | analysis::DepKind::DeferredRebind
    ));
    assert_eq!(
        proposal["observed_consumers"]["writes"]["total_edges"], 0,
        "the proposal's own class writer must not appear as external evidence"
    );
}

#[test]
fn reports_scheduling_context_separately_from_value_reads_and_writes() {
    let fixture = GraphFixture::new("console.log('first');\nconsole.log('second');\n", &[]);
    let report = proposals(&fixture);
    let first_id = source_unit_id_for_line(&fixture, 1);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|proposal| {
            proposal["source_unit_ids"]
                .as_array()
                .is_some_and(|ids| ids.contains(&json!(first_id)))
        })
        .unwrap_or_else(|| panic!("proposal for first side effect missing: {report:#}"));

    let observed = &proposal["observed_consumers"];
    assert_eq!(observed["value_reads"]["total_edges"], 0);
    assert_eq!(observed["writes"]["total_edges"], 0);
    let context = &observed["scheduling_or_promoted_context"];
    assert_eq!(context["total_source_units"], 1);
    assert_eq!(context["total_edges"], 1);
    assert_eq!(
        context["edges"][0]["source"],
        source_unit_id_for_line(&fixture, 2)
    );
    assert_eq!(context["edges"][0]["target"], first_id);
    assert_eq!(context["edges"][0]["edge_kind"], "sequenced");
    fixture.assert_runs("first\nsecond\n");
}

#[test]
fn bounds_consumer_metadata_and_reports_omitted_totals() {
    let mut source = String::from("const primitive = 0;\n");
    let mut module_files = Vec::new();
    for index in 0..14 {
        source.push_str(&format!("const reader{index} = primitive + 1;\n"));
        module_files.push((
            format!("readers/reader{index}.yaml"),
            format!("members: [{{selector: {{binding: {{name: reader{index}}}}}}}]\n"),
        ));
    }
    let module_files: Vec<_> = module_files
        .iter()
        .map(|(path, yaml)| (path.as_str(), yaml.as_str()))
        .collect();
    let fixture = GraphFixture::rejected(&source, &module_files);
    let report = proposals(&fixture);
    let proposal = proposal_for_binding(&report, "primitive");
    let reads = &proposal["observed_consumers"]["value_reads"];

    assert_eq!(reads["total_source_units"], 14);
    assert_eq!(reads["omitted_source_units"], 6);
    assert_eq!(reads["source_units"].as_array().unwrap().len(), 8);
    assert_eq!(reads["total_modules"], 14);
    assert_eq!(reads["omitted_modules"], 6);
    assert_eq!(reads["modules"].as_array().unwrap().len(), 8);
    assert_eq!(reads["total_edges"], 14);
    assert_eq!(reads["omitted_edges"], 2);
    assert_eq!(reads["edges"].as_array().unwrap().len(), 12);
}
