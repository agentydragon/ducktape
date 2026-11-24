use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, staged};
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
    .with_import_order(&["later", "wrapped"])
}

/// The xfail form for a split the effect verdict already accepts and only the
/// legacy gate's ordering veto refuses.
fn assert_legacy_ordering_veto(opts: FixtureOpts<'_>, expected_stdout: &str) {
    let assessment = assert_pending_split_preserves_output(
        opts,
        "accepted_within_checked_scope",
        expected_stdout,
    );
    assert_eq!(
        assessment["legacy_gate_ordering_veto"], true,
        "{assessment:#}"
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
fn passive_initializer_and_export_alias_are_available_after_splitting() {
    for provider in [
        "export const factory = callback => ({ invoke: callback });",
        "const factory = callback => ({ invoke: callback }); export { factory };",
        "const create = callback => ({ invoke: callback }); export { create as factory };",
        "export const factory = function (callback) { return { invoke: callback }; };",
    ] {
        let local = if provider.contains("const create") {
            "create"
        } else {
            "factory"
        };
        let (staged, _fixture) = staged(
            options(&[("static/peer", provider)]).with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("provider", &[Member::new(local)])],
            )]),
        );
        assert_node_output(&staged.root().join("snapshot/static/app.js"), "42\n", "");
        let direct = staged.run(&[]);
        let saved = staged.run_saved(&[]);
        for invocation in [&direct, &saved] {
            assert!(
                invocation.result.status.success(),
                "{}",
                invocation.result.stderr
            );
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                "42\n",
                "",
            );
        }
        assert_same_invocation_trees(&direct, &saved);
    }
}

#[test]
fn passive_provider_preserves_a_deferred_primitive_capture() {
    for (body, expected) in [
        ("[callback(), captured][0]", "42\n"),
        ("callback() + captured", "49\n"),
    ] {
        let provider = format!(
            "const captured = 7; export const factory = callback => ({{ invoke: () => {body} }});"
        );
        let fixture = run_fixture(options(&[("static/peer", &provider)]));
        assert_preserved_entry_output(&fixture, expected);
    }
}

/// Valid splits waiting on the provider-initialization proof: each provider
/// binding is initialized before the importer calls it, but its initializer
/// is not a proved-available hoisted or passive export. The effect verdict
/// accepts the order; the legacy gate's ordering veto refuses it.
#[test]
fn unproved_provider_initializers_stay_pending() {
    for provider in [
        "export let factory = callback => ({ invoke: callback }); factory = callback => ({ invoke: callback });",
        "let captured = 7; export const factory = callback => ({ invoke: () => [callback(), captured][0] }); captured = 8;",
        "import './app.js'; export const factory = callback => ({ invoke: callback });",
        "export { factory } from './leaf.js';",
        "const api = { factory(callback) { return { invoke: callback }; } }; export const factory = api.factory;",
    ] {
        assert_legacy_ordering_veto(
            options(&[
                ("static/peer", provider),
                (
                    "static/leaf",
                    "export const factory = callback => ({ invoke: callback });",
                ),
            ]),
            "42\n",
        );
    }
}

/// A valid split waiting on namespace-member imports of a provider factory.
#[test]
fn namespace_import_of_a_factory_stays_pending() {
    assert_legacy_ordering_veto(
        FixtureOpts::new(
            "import * as api from './peer.js'; const wrapped = api.factory(() => later); const later = 42; console.log(wrapped.invoke());",
            vec![
                logical_module("wrapped", &[Member::new("wrapped")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const factory = callback => ({ invoke: callback });")])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["later", "wrapped"]),
        "42\n",
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
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["later", "wrapped"]),
    );
    assert_preserved_entry_error(&fixture, "Cannot access 'later' before initialization");
}
