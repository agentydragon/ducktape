use std::collections::{BTreeMap, BTreeSet};

use crate::test_support::{
    analyze_facts, analyze_facts_with_hints, build_dependency_graph, hints_with_decorate_helper,
    test_id,
};
use crate::*;
use analysis_model::DepKind;
use analysis_vocabulary::inputs::{AnalysisAssumptions, AnalysisInputs};
use raw_js_test_support::parse;
use source_identity::{SourceUnitId, SourceUnitOrdinal};

#[test]
fn constructor_summary_excludes_uninvoked_methods() {
    let module = parse(
        "const a = 1; const b = 2; class Foo { constructor() { this.value = a; } unused() { return b; } } const result = new Foo().value;",
    );
    let facts = analyze_facts(&module);
    assert!(facts[2].reads.lazy.contains(&test_id("b")));
    let graph = build_dependency_graph(&facts).expect("dependency graph");
    assert!(graph.iter_edges().any(|edge| {
        edge.from == SourceUnitId(3)
            && edge.to == SourceUnitId(0)
            && matches!(edge.reason.role(), EdgeRole::PromotedAtInit { .. })
    }));
    assert!(!graph.iter_edges().any(|edge| {
        edge.from == SourceUnitId(3)
            && edge.to == SourceUnitId(1)
            && matches!(edge.reason.role(), EdgeRole::PromotedAtInit { .. })
    }));
}

#[test]
fn constructor_summary_includes_methods_it_may_call() {
    let module = parse(
        "const b = 2; class Foo { constructor() { this.used(); } used() { return b; } } const result = new Foo();",
    );
    let facts = analyze_facts(&module);
    let graph = build_dependency_graph(&facts).expect("dependency graph");
    assert!(graph.iter_edges().any(|edge| {
        edge.from == SourceUnitId(2)
            && edge.to == SourceUnitId(0)
            && matches!(edge.reason.role(), EdgeRole::PromotedAtInit { .. })
    }));
}

#[test]
fn no_sync_callback_member_hint_suppresses_argument_fallback_roots() {
    let module = parse(
        "const registry = { registerProvider() {} }; \
         const data = 'ready'; \
         const provider = { name: 'p', read() { return data; } }; \
         registry.registerProvider(provider);",
    );
    let default_facts = analyze_facts(&module);
    assert!(
        default_facts[3]
            .at_init_unresolved_sources
            .contains(&test_id("provider"))
    );

    let hints = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            no_sync_callback_members: BTreeMap::from([(
                "registry".to_string(),
                BTreeSet::from(["registerProvider".to_string()]),
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert!(
        facts[3].reads.eager.contains(&test_id("provider")),
        "the provider argument is still evaluated at init"
    );
    assert!(
        facts[3]
            .at_init_unresolved_sources
            .contains(&test_id("registry")),
        "the unresolved member receiver still participates in fallback"
    );
    assert!(
        !facts[3]
            .at_init_unresolved_sources
            .contains(&test_id("provider")),
        "audited registration calls should not treat provider arguments \
         as synchronously invoked fallback roots"
    );

    let graph = build_dependency_graph(&facts).unwrap();
    assert!(
        graph.iter_edges().all(|edge| {
            !(edge.from == SourceUnitId(3)
                && edge.to == SourceUnitId(1)
                && edge.reason.kind == DepKind::EagerUse)
        }),
        "provider method body reads must not be promoted through \
         audited registration calls: {:#?}",
        graph.iter_edges().collect::<Vec<_>>(),
    );
}

#[test]
fn no_sync_callback_member_hint_suppresses_first_order_argument_fallback_roots() {
    let module = parse(
        "const registry = { registerProvider() {} }; \
         const data = 'ready'; \
         const provider = { name: 'p', read() { return data; } }; \
         function boot() { registry.registerProvider(provider); } \
         boot();",
    );
    let hints = AnalysisInputs {
        assumptions: AnalysisAssumptions {
            no_sync_callback_members: BTreeMap::from([(
                "registry".to_string(),
                BTreeSet::from(["registerProvider".to_string()]),
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    };
    let facts = analyze_facts_with_hints(&module, &hints);
    assert!(
        facts[3]
            .reads
            .first_order_lazy
            .contains(&test_id("provider")),
        "the provider argument is still evaluated when boot runs"
    );
    assert!(
        !facts[3]
            .first_order_unresolved_sources
            .contains(&test_id("provider")),
        "audited registration calls inside a first-order body should not \
         seed provider arguments as fallback roots"
    );

    let graph = build_dependency_graph(&facts).unwrap();
    assert!(
        graph.iter_edges().all(|edge| {
            !(edge.from == SourceUnitId(4)
                && edge.to == SourceUnitId(1)
                && edge.reason.kind == DepKind::EagerUse)
        }),
        "provider method body reads must not be promoted through boot(): {:#?}",
        graph.iter_edges().collect::<Vec<_>>(),
    );
}

#[test]
fn annotated_decorate_helper_keeps_target_local_effect_and_array_frontier() {
    let module = parse(
        r#"console.log("boot");
function Ro(decorators, target, key, flags) {}
const Z = {};
class C {}
Ro([Z.shallow], C.prototype, "x", 2);
console.log("tail");"#,
    );
    let facts = analyze_facts_with_hints(&module, &hints_with_decorate_helper("Ro"));
    assert_eq!(facts[4].local_effects, BTreeSet::from([test_id("C")]));
    assert!(facts[4].purity.is_pure());

    let graph = build_dependency_graph(&facts).unwrap();
    let local_effects: Vec<_> = graph
        .iter_edges()
        .filter(|edge| edge.reason.kind == DepKind::LocalEffect)
        .collect();
    assert_eq!(local_effects.len(), 1);
    assert_eq!(local_effects[0].from, SourceUnitId(4));
    assert_eq!(local_effects[0].to, SourceUnitId(3));
    assert_eq!(
        local_effects[0]
            .reason
            .binding
            .as_ref()
            .map(|id| id.0.as_ref()),
        Some("C"),
    );
    let sequenced_edges: Vec<_> = graph
        .iter_edges()
        .filter(|edge| edge.reason.kind == DepKind::Sequenced)
        .map(|edge| (edge.from.0, edge.to.0))
        .collect();
    assert_eq!(
        sequenced_edges,
        vec![(5, 0), (5, 4)],
        "the call's temporary array keeps the heap-frontier edge from tail (5) to the helper call (4), while the helper remains outside the unrelated boot/tail global sequence: {:#?}",
        graph.iter_edges().collect::<Vec<_>>(),
    );
}

/// One binding declared by several top-level statements is supported only when every site is a
/// `var`; any other mix is refused, naming the binding and the first two sites.
#[test]
fn repeated_declaration_is_refused_unless_every_site_is_var() {
    build_dependency_graph(&analyze_facts(&parse("var pick = 1;\nvar pick = 2;"))).unwrap();
    for source in [
        "function pick() {}\nfunction pick() {}",
        "var pick = 1;\nfunction pick() {}",
    ] {
        assert_eq!(
            build_dependency_graph(&analyze_facts(&parse(source))).unwrap_err(),
            DependencyGraphBuildError::DuplicateTopLevelDeclaration(DuplicateTopLevelDeclaration {
                binding: "pick".into(),
                first: SourceUnitOrdinal(0),
                second: SourceUnitOrdinal(1),
            },),
            "{source}"
        );
    }
}

fn atomic_groups_for(source: &str) -> Vec<AtomicGroup> {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let dependency_graph = build_dependency_graph(&facts).unwrap();
    compute_atomic_groups(&dependency_graph)
}

fn atomic_group_sizes(atomic_groups: &[AtomicGroup]) -> Vec<usize> {
    let mut sizes: Vec<usize> = atomic_groups.iter().map(|u| u.members.len()).collect();
    sizes.sort_unstable();
    sizes
}

fn assert_partitions_all_source_units(atomic_groups: &[AtomicGroup], total_source_units: usize) {
    let summed: usize = atomic_groups.iter().map(|u| u.members.len()).sum();
    assert_eq!(
        summed, total_source_units,
        "atomic groups must cover every source unit exactly once; got atomic_groups {atomic_groups:?}",
    );
    let mut seen = BTreeSet::new();
    for atomic_group in atomic_groups {
        for source_unit in &atomic_group.members {
            assert!(
                seen.insert(*source_unit),
                "source unit {source_unit:?} appears in more than one atomic group",
            );
        }
    }
}

#[test]
fn atomic_groups_singletons_for_independent_source_units() {
    // No edges → each source unit is its own atomic group.
    let atomic_groups = atomic_groups_for("const A = 1; const B = 2; const C = 3;");
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_eager_use_chain_stays_split() {
    // A → B → C via EagerUse: directed-only edges, no cycle, so
    // each source unit remains its own atomic group.
    let atomic_groups = atomic_groups_for("const C = 3; const B = C + 1; const A = B + 1;");
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_lazy_use_cycle_stays_split() {
    // Two functions that reference each other lazily plus their
    // bindings — no constraining edges, so all four source units are
    // independent atomic groups.
    let atomic_groups = atomic_groups_for(
        "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
    );
    assert_partitions_all_source_units(&atomic_groups, 4);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1, 1]);
}

#[test]
fn atomic_groups_sequenced_chain_stays_split() {
    // Three side-effecting top-level statements form a directed
    // Sequenced chain. A directed source-order edge alone is
    // satisfiable by linker order — no co-location forced — so
    // every source unit stays in its own atomic group. Co-location
    // would only kick in if some non-Sequenced edge ran in the
    // reverse direction.
    let atomic_groups = atomic_groups_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
    );
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_sequenced_plus_reverse_eager_merges() {
    // A Sequenced source-order edge in one direction plus an
    // EagerUse read in the reverse direction forms an SCC in
    // `G_atomic` and forces co-location.
    // `const A = 1;` is pure (no side effect, no Sequenced edge);
    // `const x = (globalThis.tag = "x", A);` is side-effecting AND
    // eagerly reads `A`. The eager read draws `x → A`; the
    // Sequenced edge from the next side-effect (`const y = ...`)
    // gives `y → x`. Eager `y → A` adds `y → A` too. So {x, y}
    // ends up merged only if some edge reverses through A. Use a
    // shape that produces a real cycle:
    // `let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);`
    // — top-level Sequenced + EagerRebind force {A, stmt_1, stmt_2}
    // into one atomic group (Rebind bidirectional + Sequenced directed
    // form a cycle).
    let atomic_groups = atomic_groups_for(
        r#"let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);"#,
    );
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![3]);
}

#[test]
fn atomic_groups_lazy_rebind_merges() {
    // `let A = 0; function B() { A = 1; }` produces a LazyRebind
    // edge from B → A. LazyRebind is bidirectional in `G_atomic`,
    // so A and B collapse into one atomic group.
    let atomic_groups = atomic_groups_for("let A = 0; function B() { A = 1; }");
    assert_partitions_all_source_units(&atomic_groups, 2);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![2]);
}
