//! Report-only effect translation validation: array literal spread through the
//! kernel's `IterateSpread` rule, with the adapter's native iterator receipt.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn assert_commuting_relocation(fixture: &Fixture, case: &str) {
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{case}: {report:#}");
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
}

#[test]
fn callable_array_spreads_keep_receipts_across_an_independent_inversion() {
    for (body, expected) in [
        ("return [...['a', 'b'], 'c'];", "7 a,b,c\n"),
        (
            "const items = ['a', 'b']; return ['base', ...items];",
            "7 base,a,b\n",
        ),
        (
            "const first = ['a'], second = ['b']; return [...first, ...second, 'c'];",
            "7 a,b,c\n",
        ),
        (
            "const items = ['a']; const copy = [...items]; return [...copy, 'b'];",
            "7 a,b\n",
        ),
        ("const items = []; return [...items];", "7 \n"),
    ] {
        let source = format!(
            "function merge() {{ {body} }}\nconst independent = 7;\nvar result = merge();\nconsole.log(independent, result.join(','));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "merge",
                &[Member::new("merge"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        assert_commuting_relocation(&fixture, body);
    }
}

#[test]
fn root_array_spread_of_module_arrays_keeps_its_receipt_across_an_inversion() {
    let source = "const first = ['a'];\nconst second = ['b'];\nconst independent = 7;\nconst scopes = ['base', ...first, ...second];\nconsole.log(independent, scopes.join(','));\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "scopes",
            &[
                Member::new("first"),
                Member::new("second"),
                Member::new("scopes"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 base,a,b\n");
    assert_inline_operation_receipt(&fixture, "scopes", 0, "root spread");
    assert_commuting_relocation(&fixture, "root spread");
}

#[test]
fn spread_reads_the_source_contents_at_its_initialization_prefix() {
    let source = "const first = ['a'];\nconst copy = [...first];\nconst pushed = first.push('b');\nconsole.log(copy.join(','), first.join(','), pushed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("source", &[Member::new("first"), Member::new("copy")]),
            logical_module("update", &[Member::new("pushed")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "a a,b 2\n");
    let graph = fixture.dependency_graph();
    let copy = source_unit_for_binding(&graph, "copy");
    let pushed = source_unit_for_binding(&graph, "pushed");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == pushed
                && edge.target == copy
                && edge.edge_kind == analysis_model::DepKind::Sequenced
        }),
        "the copy's ArrayContents read must stay before the later source write: {:#?}",
        graph.edges
    );
}

#[test]
fn array_hole_and_spread_mix_keeps_its_receipt() {
    let source = "function merge() { const items = ['a']; return [, ...items, 'c']; }\nconst independent = 7;\nvar result = merge();\nconsole.log(independent, result.length, result[1], result[2]);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "merge",
            &[Member::new("merge"), Member::new("result")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 3 a c\n");
    assert_inline_operation_receipt(&fixture, "result", 1, "hole and spread");
    assert_commuting_relocation(&fixture, "hole and spread");
}

/// A pushed primitive array is still primitive-only, but the prefix records
/// only that its contents are unknown; the spread refuses with its typed
/// reason until a primitive-contents receipt survives `push`.
#[test]
fn spread_of_a_pushed_array_is_pending_a_primitive_contents_receipt() {
    let source = "function build() { const items = ['a']; items.push('b'); return [...items, 'c']; }\nconst independent = 7;\nvar result = build();\nconsole.log(independent, result.join(','));\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "build",
            &[Member::new("build"), Member::new("result")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 a,b,c\n");
    assert_effect_coverage_pending(
        &fixture,
        "result",
        "array_spread",
        "primitive-only contents through Array.prototype.push",
    );
}

/// An imported array without the authored lifetime premise may carry any
/// iterator; the kernel refuses the spread with its typed reason instead of
/// guessing, and the legacy gate stays authoritative for the layout.
#[test]
fn spread_of_an_imported_array_without_a_premise_refuses_with_a_typed_reason() {
    let source = "import { left } from './provider.js';\nconst independent = 7;\nconst merged = [...left, 'c'];\nconsole.log(independent, merged.join(','));\n";
    let peers = [("static/provider", "export const left = ['a', 'b'];\n")];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("merged", &[Member::new("merged")])],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "7 a,b,c\n");
    let execution = binding_execution_report(&fixture, "merged");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "{execution:#}"
    );
    assert!(
        execution["precision_losses"]
            .as_array()
            .expect("precision losses")
            .iter()
            .any(|loss| {
                loss["reason"]["code"] == "unsupported_operation"
                    && loss["reason"]["operation"] == "array_spread"
            }),
        "{execution:#}"
    );
}
