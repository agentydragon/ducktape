use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

const SOURCE: &str = "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n";

fn options_with_source<'a>(
    source: &'a str,
    providers: &'a [(&'a str, &'a str)],
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("wrapped", &[Member::new("wrapped")]),
            logical_module("later", &[Member::new("later")]),
        ],
    )
    .with_extra_chunks(providers)
    .with_unassigned_mode(unassigned_mode_inline())
}

fn options<'a>(providers: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    options_with_source(SOURCE, providers)
}

fn assert_unproved_provider_keeps_initializer_order(fixture: &Fixture) {
    let graph = fixture.dependency_graph();
    let owner = |binding: &str| {
        graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .expect("fixture binding has a source owner")
    };
    let wrapped = owner("wrapped");
    let later = owner("later");
    assert!(wrapped.statement_ordinal < later.statement_ordinal);
    let assessment = fixture.candidate_relocation_assessment();
    let order = assessment["emitted_order"].as_array().unwrap();
    let position = |ordinal: usize| {
        order
            .iter()
            .position(|value| value.as_u64() == Some(ordinal as u64))
            .expect("candidate order contains the source owner")
    };
    assert!(
        position(wrapped.statement_ordinal.0) < position(later.statement_ordinal.0),
        "an unproved factory must stay before the callback's captured initializer: {assessment}"
    );
    assert!(assessment["swapped_pairs"].as_u64().unwrap() > 0);
    let execution = serde_json::to_value(wrapped.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], false, "{execution}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "legacy_execution",
        "preserving source order does not prove the provider body: {execution}"
    );
}

fn assert_deferred_callback_preserved(opts: FixtureOpts<'_>, case: &str) {
    let fixture = run_fixture(opts);
    assert_preserved_entry_output_with_host_setup(&fixture, "", "42\n", case);
    assert_unproved_provider_keeps_initializer_order(&fixture);
}

fn assert_eager_callback_tdz_preserved(opts: FixtureOpts<'_>, case: &str) {
    let fixture = run_fixture(opts);
    save_relocation_reports(&fixture.report_root, &fixture.chunk_id, case);
    assert_preserved_entry_error(
        &fixture,
        "ReferenceError: Cannot access 'later' before initialization",
    );
    assert_unproved_provider_keeps_initializer_order(&fixture);
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

#[test]
fn unproved_providers_preserve_callback_initializer_order() {
    let providers = [
        (
            "mutable-reassigned-initializer",
            "export let factory = callback => ({ invoke: callback }); factory = callback => ({ invoke: callback });",
        ),
        (
            "provider-capture",
            "const captured = 7; export const factory = callback => ({ invoke: () => [callback(), captured][0] });",
        ),
        (
            "side-effect-import",
            "import './app.js'; export const factory = callback => ({ invoke: callback });",
        ),
        ("re-export", "export { factory } from './leaf.js';"),
        (
            "object-method-alias",
            "const api = { factory(callback) { return { invoke: callback }; } }; export const factory = api.factory;",
        ),
    ];
    for (case, provider) in providers {
        let chunks = [
            ("static/peer", provider),
            (
                "static/leaf",
                "export const factory = callback => ({ invoke: callback });",
            ),
        ];
        let opts = options(&chunks);
        assert_deferred_callback_preserved(opts, case);
    }
}

#[test]
fn namespace_import_preserves_callback_initializer_order() {
    let opts = FixtureOpts::new(
            "import * as api from './peer.js'; const wrapped = api.factory(() => later); const later = 42; console.log(wrapped.invoke());",
            vec![
                logical_module("wrapped", &[Member::new("wrapped")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const factory = callback => ({ invoke: callback });")])
        .with_unassigned_mode(unassigned_mode_inline());
    assert_deferred_callback_preserved(opts, "namespace-import");
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

#[test]
fn mutable_named_import_that_calls_back_during_its_final_factory_keeps_tdz() {
    let provider = "export let factory = callback => ({ invoke: callback }); factory = callback => { callback(); return { invoke: callback }; };";
    assert_eager_callback_tdz_preserved(
        options(&[("static/peer", provider)]),
        "mutable-named-eager-callback",
    );
}

#[test]
fn namespace_import_that_calls_back_during_its_factory_keeps_tdz() {
    let source = "import * as api from './peer.js'; const wrapped = api.factory(() => later); const later = 42; console.log(wrapped.invoke());";
    let provider = "export function factory(callback) { callback(); return { invoke: callback }; }";
    assert_eager_callback_tdz_preserved(
        options_with_source(source, &[("static/peer", provider)]),
        "namespace-eager-callback",
    );
}
