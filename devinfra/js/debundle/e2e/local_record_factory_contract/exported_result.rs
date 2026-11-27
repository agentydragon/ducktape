//! Exported factory results stay deferred until importer use and keep rebinding and mutation visible.

use super::{execution, members};
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;
use graph::DependencyGraphReport;

const EXPORTED_RECORD_PROVIDER: &str = r#"class Holder {
  constructor(record) { this.record = record; this.missing = record.missing; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 42;
})({});
const command = create({ execute: () => later });
const later = dynamic;
export { command };
"#;

const ACYCLIC_COMMAND_IMPORTER: &str = r#"import { command } from "./provider.js";
export const observed = command.run();
console.log(observed);
"#;

fn exported_record_factory_modules(callback_statements: &[&str]) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "callbacks/wrapped",
            &members("create", &["Holder", "create", "command"]),
            callback_statements,
        ),
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

/// Runs the provider `source` split into `modules` behind [`ACYCLIC_COMMAND_IMPORTER`].
fn run_exported_record_factory(
    run: fn(FixtureOpts<'_>) -> Fixture,
    source: &str,
    modules: Vec<LogicalModuleEntry>,
) -> Fixture {
    let provider_paths: Vec<String> = modules.iter().map(|(module, _)| module.clone()).collect();
    let provider_priority: Vec<&str> = provider_paths.iter().map(String::as_str).collect();
    run(FixtureOpts::new(ACYCLIC_COMMAND_IMPORTER, vec![])
        .with_chunk_id("static/consumer")
        .with_import_order(&["residual"])
        .with_extra_chunks(&[("static/provider", source)])
        .with_extra_chunk_logical_modules(&[("static/provider", modules)])
        .with_chunk_import_order("static/provider", &provider_priority))
}

#[test]
fn exported_factory_result_stays_deferred_until_acyclic_importer_use() {
    let fixture = run_exported_record_factory(
        run_fixture_with_graphs,
        EXPORTED_RECORD_PROVIDER,
        exported_record_factory_modules(&[]),
    );
    assert_preserved_entry_output(&fixture, "42\n");

    let graph: DependencyGraphReport = read_gzip_json(
        &fixture
            .report_root
            .join("static/provider/dependency_graph.json.gz"),
    );
    let execution = execution(&graph, "command");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(execution.local_record_factory_uses.len(), 1);
}

#[test]
fn exported_factory_result_rejects_a_rebound_factory() {
    let rebound_provider = EXPORTED_RECORD_PROVIDER.replace(
        "const command = create({ execute: () => later });",
        "create = function (record) { return new Holder(record); };\nconst command = create({ execute: () => later });",
    );
    let rebound_chunks = [("static/consumer", ACYCLIC_COMMAND_IMPORTER)];
    let rejected = run_rejection_fixture_with_graphs(
        FixtureOpts::new(&rebound_provider, exported_record_factory_modules(&[]))
            .with_chunk_id("static/provider")
            .with_extra_chunks(&rebound_chunks),
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        conflict["causes"]
            .as_array()
            .is_some_and(|causes| causes.iter().any(|cause| cause == "eager_rebind"))
            && conflict["claims"].as_array().is_some_and(|claims| {
                claims
                    .iter()
                    .any(|claim| claim["module"] == "callbacks/wrapped")
            })
    }));
    let graph = rejected.dependency_graph();
    assert!(
        execution(&graph, "command")
            .local_record_factory_uses
            .is_empty()
    );
}

#[test]
fn exported_factory_result_preserves_later_mutation() {
    let mutated_provider = EXPORTED_RECORD_PROVIDER.replace(
        "const later = dynamic;",
        "command.record.execute = () => later + 1;\nconst later = dynamic;",
    );
    let consumer_chunks = [("static/consumer", ACYCLIC_COMMAND_IMPORTER)];
    let mutation = ["command.record.execute = () => later + 1;"];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &mutated_provider,
            exported_record_factory_modules(&mutation),
        )
        .with_chunk_import_order(
            "static/provider",
            &["callbacks/wrapped", "values/dynamic", "values/later"],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&consumer_chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    let fixture = run_exported_record_factory(
        run_fixture,
        &mutated_provider,
        vec![logical_module_with_anon(
            "callbacks/wrapped",
            &members(
                "create",
                &["Holder", "create", "prefix", "dynamic", "command", "later"],
            ),
            &mutation,
        )],
    );
    assert_preserved_entry_output(&fixture, "43\n");
}
