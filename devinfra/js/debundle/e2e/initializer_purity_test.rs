//! Initializer-scoped purity must address the initializer expression only,
//! while unresolved callback reads remain eager dependencies.

use debundle_e2e_support::*;

fn opaque_factory_source() -> &'static str {
    r#"const seed = "ready";
const A = globalThis.factory(() => seed + "a");
const B = globalThis.factory(() => seed + "b");
const C = globalThis.factory(() => seed + "c");
console.log(A, B, C);
export { seed, A, B, C };
"#
}

fn split_factory_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("mod_a", &[Member::new("A"), Member::new("C")]),
        logical_module("mod_b", &[Member::new("seed"), Member::new("B")]),
    ]
}

#[test]
fn opaque_callback_factory_without_initializer_override_has_s_cycle() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(opaque_factory_source(), split_factory_modules()),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn initializer_override_keeps_callback_read_dependency() {
    let fixture = run_fixture(FixtureOpts::new(
        opaque_factory_source(),
        vec![
            logical_module(
                "mod_a",
                &[
                    Member::source_alpha_target(
                        "A",
                        "A",
                        "const A = globalThis.factory(() => seed + \"a\");",
                    )
                    .with_initializer_purity(),
                    Member::source_alpha_target(
                        "C",
                        "C",
                        "const C = globalThis.factory(() => seed + \"c\");",
                    )
                    .with_initializer_purity(),
                ],
            ),
            logical_module(
                "mod_b",
                &[
                    Member::source_alpha_target(
                        "B",
                        "B",
                        "const B = globalThis.factory(() => seed + \"b\");",
                    )
                    .with_initializer_purity(),
                    Member::new("seed"),
                ],
            ),
        ],
    ));

    let graph = fixture.dependency_graph();
    let caller_source_unit = source_unit_for_binding(&graph, "A");
    let seed_source_unit = source_unit_for_binding(&graph, "seed");
    let promoted_seed_edges: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.source == caller_source_unit
                && edge.target == seed_source_unit
                && edge.edge_kind == analysis::DepKind::EagerUse
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
