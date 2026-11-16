//! Initializer-scoped purity must address the initializer expression only,
//! while unresolved callback reads remain eager dependencies.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

const OPAQUE_FACTORY: &str = "globalThis.factory = callback => callback();\nexport {};\n";
const OPAQUE_FACTORY_CHUNKS: &[(&str, &str)] = &[("static/factory", OPAQUE_FACTORY)];
const OPAQUE_FACTORY_RUNTIME_FILES: &[(&str, &str)] = &[("static/app/factory.js", OPAQUE_FACTORY)];

fn assert_schedule_refusal_preserves_source_and_candidate(
    opts: FixtureOpts<'_>,
    expected_output: &str,
) {
    let rejected = run_rejection_fixture_with_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "{}",
        rejected.stderr
    );
    assert!(rejected.gate_rejected_marker().is_file());
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
}

fn opaque_factory_source() -> &'static str {
    r#"import './factory.js';
const seed = "ready";
const A = globalThis.factory(() => seed + "a");
const B = globalThis.factory(() => seed + "b");
const C = globalThis.factory(() => seed + "c");
console.log(A, B, C);
export { seed, A, B, C };
"#
}

fn factory_modules(with_initializer_purity: bool) -> Vec<LogicalModuleEntry> {
    let member = |name, declaration| {
        let member = Member::source_alpha_target(name, name, declaration);
        if with_initializer_purity {
            member.with_initializer_purity()
        } else {
            member
        }
    };
    let mod_a = [
        member("A", "const A = globalThis.factory(() => seed + \"a\");"),
        member("C", "const C = globalThis.factory(() => seed + \"c\");"),
    ];
    let mod_b = [
        member("B", "const B = globalThis.factory(() => seed + \"b\");"),
        Member::new("seed"),
    ];
    vec![
        logical_module("mod_a", &mod_a),
        logical_module("mod_b", &mod_b),
    ]
}

#[test]
fn opaque_callback_factory_without_initializer_override_has_s_cycle() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(opaque_factory_source(), factory_modules(false))
            .with_extra_chunks(OPAQUE_FACTORY_CHUNKS)
            .with_extra_files(OPAQUE_FACTORY_RUNTIME_FILES),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn initializer_override_keeps_callback_read_dependency() {
    assert_schedule_refusal_preserves_source_and_candidate(
        FixtureOpts::new(opaque_factory_source(), factory_modules(true))
            .with_extra_chunks(OPAQUE_FACTORY_CHUNKS)
            .with_extra_files(OPAQUE_FACTORY_RUNTIME_FILES),
        "readya readyb readyc\n",
    );

    let member = |name, declaration| {
        Member::source_alpha_target(name, name, declaration).with_initializer_purity()
    };
    let colocated = run_fixture_with_graphs(
        FixtureOpts::new(
            opaque_factory_source(),
            vec![logical_module(
                "factory_values",
                &[
                    Member::new("seed"),
                    member("A", "const A = globalThis.factory(() => seed + \"a\");"),
                    member("B", "const B = globalThis.factory(() => seed + \"b\");"),
                    member("C", "const C = globalThis.factory(() => seed + \"c\");"),
                ],
            )],
        )
        .with_extra_chunks(OPAQUE_FACTORY_CHUNKS)
        .with_extra_files(OPAQUE_FACTORY_RUNTIME_FILES),
    );
    assert_preserved_entry_output(&colocated, "readya readyb readyc\n");
    assert_module_source(
        &colocated.out_root,
        "static/app/modules/factory_values.js",
        &["const seed", "const A", "const B", "const C"],
        &[],
    );
    assert_generated_module_after_entry_script(
        &colocated,
        r#"const values = await import("./static/app/modules/factory_values.js");
console.log(JSON.stringify(Object.keys(values).sort()));
"#,
        "[\"A\",\"B\",\"C\",\"seed\"]\n",
    );

    let graph = colocated.dependency_graph();
    let caller_source_unit = source_unit_for_binding(&graph, "A");
    let seed_source_unit = source_unit_for_binding(&graph, "seed");
    let promoted_seed_edges: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.source == caller_source_unit
                && edge.target == seed_source_unit
                && edge.edge_kind == analysis_model::DepKind::EagerUse
                && edge.binding.as_deref() == Some("seed")
                && matches!(
                    edge.role,
                    Some(analysis::EdgeRoleReport::PromotedAtInit { .. })
                )
        })
        .collect();
    assert!(
        !promoted_seed_edges.is_empty(),
        "the initializer annotation must retain callback-body read ordering; \
         expected a promoted eager edge from the factory source_unit to `seed`, got \
         {promoted_seed_edges:#?}\n\nFull dependency graph: {graph:#?}",
    );
}

#[test]
fn chunk_rename_initializer_purity_preserves_residual_reads_and_sibling_effects() {
    let options = || {
        let mut renames = chunk_rename("readableStore", "store");
        renames
            .annotations
            .entry("readableStore".to_owned())
            .or_default()
            .initializer_purity = InitializerPurity::Pure;
        FixtureOpts::new(
            r#"import './factory.js';
const seed = "ready";
const store = globalThis.factory(() => seed + "store");
const selected = globalThis.factory(() => seed + "selected");
const side = console.log("side");
console.log(store, selected);
export { store, selected };
"#,
            vec![logical_module(
                "selected",
                &[Member::new("seed"), Member::new("selected")],
            )],
        )
        .with_chunk_renames(renames)
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(OPAQUE_FACTORY_CHUNKS)
        .with_extra_files(OPAQUE_FACTORY_RUNTIME_FILES)
    };
    let mut unannotated = options();
    unannotated
        .chunk_renames
        .as_mut()
        .unwrap()
        .annotations
        .clear();
    let rejected = run_rejection_fixture(unannotated);
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );

    assert_schedule_refusal_preserves_source_and_candidate(
        options(),
        "side\nreadystore readyselected\n",
    );

    let mut original_phase = options();
    original_phase.logical_modules.clear();
    let fixture = run_fixture_with_graphs(original_phase);
    assert_preserved_entry_output(&fixture, "side\nreadystore readyselected\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            "const readableStore",
            "const seed",
            "const selected",
            "const side",
        ],
        &[],
    );
}

#[test]
fn chunk_rename_initializer_purity_does_not_cover_a_sibling_declarator() {
    let mut renames = chunk_rename("readableStore", "store");
    renames
        .annotations
        .entry("readableStore".to_owned())
        .or_default()
        .initializer_purity = InitializerPurity::Pure;
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            r#"import './factory.js';
const store = globalThis.factory(() => 1), side = console.log("first");
const selected = console.log("middle");
const last = console.log("last");
export { store, side, selected, last };
"#,
            vec![logical_module("selected", &[Member::new("selected")])],
        )
        .with_chunk_renames(renames)
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(OPAQUE_FACTORY_CHUNKS)
        .with_extra_files(OPAQUE_FACTORY_RUNTIME_FILES),
    );
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn initializer_override_does_not_make_later_calls_pure() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const value = globalThis.factory(() => 1);
const A = value();
const B = value();
const C = value();
export { value, A, B, C };
"#,
            vec![
                logical_module("mod_a", &[Member::new("A"), Member::new("C")]),
                logical_module("mod_b", &[Member::new("B")]),
                logical_module(
                    "mod_value",
                    &[Member::new("value").with_initializer_purity()],
                ),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn initializer_override_does_not_make_later_construction_pure() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const Constructor = globalThis.factory(() => class {});
const A = new Constructor();
const B = new Constructor();
const C = new Constructor();
export { Constructor, A, B, C };
"#,
            vec![
                logical_module("mod_a", &[Member::new("A"), Member::new("C")]),
                logical_module("mod_b", &[Member::new("B")]),
                logical_module(
                    "mod_constructor",
                    &[Member::new("Constructor").with_initializer_purity()],
                ),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn initializer_override_leaves_sibling_declarator_effectful() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const A = globalThis.factory(() => 1), sideA = globalThis.record("a");
const B = globalThis.record("b");
const C = globalThis.record("c");
export { A, sideA, B, C };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("A").with_initializer_purity(),
                        Member::new("sideA"),
                        Member::new("C"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn initializer_override_rejects_non_simple_or_ambiguous_targets() {
    let cases = [
        (
            "function target() {}\nexport { target };\n",
            Member::new("target").with_initializer_purity(),
        ),
        (
            "const { target } = {};\nexport { target };\n",
            Member::new("target").with_initializer_purity(),
        ),
        (
            "var target;\nexport { target };\n",
            Member::new("target").with_initializer_purity(),
        ),
        (
            "var target = 1;\nvar target = 2;\nexport { target };\n",
            Member::new("target").with_initializer_purity(),
        ),
        (
            "import { target } from \"external-package\";\nexport { target };\n",
            Member::renamed_with_kind("target", "target", Some("import_specifier"))
                .with_initializer_purity(),
        ),
    ];

    for (source, member) in cases {
        expect_rejection_containing_all(
            FixtureOpts::new(source, vec![logical_module("target_mod", &[member])]),
            &["initializer_purity", "target"],
        );
    }
}
