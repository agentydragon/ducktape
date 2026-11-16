use debundle_e2e_graph_support::effect_validation::binding_execution_report;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

const SOURCE: &str = "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n";

fn options<'a>(providers: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        SOURCE,
        vec![
            logical_module("wrapped", &[Member::new("wrapped")]),
            logical_module("later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(providers)
    .with_unassigned_mode(unassigned_mode_inline())
}

fn assert_unknown_reorder_refusal(
    opts: FixtureOpts<'_>,
    source_output: &str,
    emitted_output: &str,
) {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        source_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), emitted_output, "");
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
}

fn assert_available_factory_split(
    fixture: &Fixture,
    expected_stdout: &str,
    expected_authority: &str,
    case: &str,
    providers: &[&str],
    can_probe_exports: bool,
) {
    assert_preserved_entry_output(fixture, expected_stdout);
    if can_probe_exports {
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/wrapped.js",
            &["wrapped"],
            &["later"],
        );
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/later.js",
            &["later"],
            &["wrapped"],
        );
    } else {
        // A provider-to-app import cycle re-enters the app entry when Node
        // probes one module in isolation. Check placement statically; full
        // entry execution above still verifies source/emitted behavior.
        assert_module_source(
            &fixture.out_root,
            "static/app/modules/wrapped.js",
            &["wrapped"],
            &[],
        );
        assert_module_source(
            &fixture.out_root,
            "static/app/modules/later.js",
            &["later"],
            &[],
        );
    }

    let execution = binding_execution_report(fixture, "wrapped");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], expected_authority,
        "record the actual authority for {case}: {execution:#}"
    );
    assert_eq!(execution["complete"], true, "{execution:#}");
    if expected_authority == "operation_receipt" {
        let calls = execution["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .expect("provider call receipt");
        assert!(
            calls.iter().any(|call| {
                call["target"]["kind"] == "callable"
                    && providers
                        .iter()
                        .any(|module| call["target"]["source_module"] == *module)
            }),
            "the call receipt must identify the provider source body: {execution:#}"
        );
    }

    let graph = fixture.dependency_graph();
    let wrapped = source_unit_for_binding(&graph, "wrapped");
    let later = source_unit_for_binding(&graph, "later");
    assert!(
        !graph.edges.iter().any(|edge| edge.source == wrapped
            && edge.target == later
            && edge.constrains_init_order),
        "the callback's later read stays deferred until invoke(): {graph:#?}"
    );
}

#[test]
fn hoisted_provider_control() {
    let fixture = run_fixture(options(&[(
        "static/peer",
        "export function factory(callback) { return { invoke: callback }; }",
    )]));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn isolated_initializer_and_export_alias_are_available_after_splitting() {
    for provider in [
        "export const factory = callback => ({ invoke: callback });",
        "const factory = callback => ({ invoke: callback }); export { factory };",
        "const create = callback => ({ invoke: callback }); export { create as factory };",
    ] {
        let local = if provider.contains("const create") {
            "create"
        } else {
            "factory"
        };
        let fixture = run_fixture(
            options(&[("static/peer", provider)]).with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("provider", &[Member::new(local)])],
            )]),
        );
        assert_preserved_entry_output(&fixture, "42\n");
    }
}

/// Provider variants keep the authority actually reported for each binding.
/// A provider that imports the app keeps its original loader scope, which
/// prevents probing emitted modules alone.
#[test]
fn provider_initializers_preserve_runtime_and_report_actual_authority() {
    for (provider, expected_authority) in [
        (
            "const captured = 7; export const factory = callback => ({ invoke: () => [callback(), captured][0] });",
            "operation_receipt",
        ),
        (
            "import './app.js'; export const factory = callback => ({ invoke: callback });",
            "operation_receipt",
        ),
    ] {
        let fixture = run_fixture_with_graphs(options(&[
            ("static/peer", provider),
            (
                "static/leaf",
                "export const factory = callback => ({ invoke: callback });",
            ),
        ]));
        let can_probe_exports = !provider.contains("import './app.js'");
        assert_available_factory_split(
            &fixture,
            "42\n",
            expected_authority,
            provider,
            &["static/peer", "static/leaf"],
            can_probe_exports,
        );
    }
}

#[test]
fn mutating_provider_factory_keeps_runtime_and_reports_schedule_refusal() {
    let provider = "export let factory = callback => ({ invoke: callback }); factory = callback => ({ invoke: callback });";
    let peer = [("static/peer", provider)];
    assert_unknown_reorder_refusal(options(&peer), "42\n", "42\n");

    let colocated = run_fixture_with_graphs(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "factory_values",
                &[Member::new("wrapped"), Member::new("later")],
            )],
        )
        .with_extra_chunks(&peer)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "42\n");
    assert_module_source(
        &colocated.out_root,
        "static/app/modules/factory_values.js",
        &["const wrapped", "const later"],
        &[],
    );
    let execution = binding_execution_report(&colocated, "wrapped");
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "legacy_execution",
        "the provider's mutable exported binding retains conservative execution authority: {execution:#}"
    );
}

#[test]
fn method_extracted_provider_factory_keeps_runtime_and_reports_authority() {
    let provider = "const api = { factory(callback) { return { invoke: callback }; } }; export const factory = api.factory;";
    let peer = [("static/peer", provider)];
    let fixture = run_fixture_with_graphs(options(&peer));
    assert_available_factory_split(
        &fixture,
        "42\n",
        "legacy_execution",
        provider,
        &["static/peer"],
        true,
    );
}

#[test]
fn reexported_provider_factory_retains_source_body_authority() {
    let peer = [
        ("static/peer", "export { factory } from './leaf.js';"),
        (
            "static/leaf",
            "export const factory = callback => ({ invoke: callback });",
        ),
    ];
    let fixture = run_fixture_with_graphs(options(&peer));
    assert_available_factory_split(
        &fixture,
        "42\n",
        "operation_receipt",
        "re-exported factory",
        &["static/peer", "static/leaf"],
        true,
    );
}

/// A valid split waiting on namespace-member imports of a provider factory.
#[test]
fn namespace_import_of_a_factory_uses_provider_receipt() {
    let source = "import * as api from './peer.js'; const wrapped = api.factory(() => later); const later = 42; console.log(wrapped.invoke());";
    let provider = "export const factory = callback => ({ invoke: callback });";
    let peer_chunks = [("static/peer", provider)];
    let split_opts = FixtureOpts::new(
        source,
        vec![
            logical_module("wrapped", &[Member::new("wrapped")]),
            logical_module("later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(&peer_chunks)
    .with_unassigned_mode(unassigned_mode_inline());
    assert_unknown_reorder_refusal(split_opts, "42\n", "42\n");

    let colocated = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "factory_values",
                &[Member::new("wrapped"), Member::new("later")],
            )],
        )
        .with_extra_chunks(&peer_chunks)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "42\n");
    assert_module_source(
        &colocated.out_root,
        "static/app/modules/factory_values.js",
        &["const wrapped", "const later"],
        &[],
    );
    let execution = binding_execution_report(&colocated, "wrapped");
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "the namespace import retains provider-body authority: {execution:#}"
    );
    let calls = execution["binding_effect_authority"]["receipt"]["calls"]
        .as_array()
        .expect("provider call receipt");
    assert!(
        calls.iter().any(|call| {
            call["target"]["kind"] == "callable" && call["target"]["source_module"] == "static/peer"
        }),
        "the namespace call receipt must name its provider source body: {execution:#}"
    );
    let graph = colocated.dependency_graph();
    let wrapped = source_unit_for_binding(&graph, "wrapped");
    let later = source_unit_for_binding(&graph, "later");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == wrapped && edge.target == later && edge.constrains_init_order
        }),
        "the callback's `later` read remains deferred: {graph:#?}"
    );
}

#[test]
fn available_provider_preserves_an_early_callback_tdz() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js'; const wrapped = factory(() => later); const later = wrapped.invoke(); console.log(later);",
            vec![
                logical_module("wrapped", &[Member::new("wrapped")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const factory = callback => ({ invoke: callback });")])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_error(&fixture, "Cannot access 'later' before initialization");
}
