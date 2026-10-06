//! Focused atomic-unit, factor-assembly, purity and source-location tests.
//! Acceptance, rejection and runtime ordering are exercised through the CLI
//! in e2e/realizability_test.rs and the at_init_* and lemma_* suites.

use std::collections::{BTreeSet, HashMap};

use super::{analyze_facts, build_owner_graph, parse, parse_with_source_map, test_id};
use crate::*;
use analysis::*;

/// Test-only convenience for constructing `ModuleId` values from a
/// raw logical index (the `logical` free function is not part of the
/// crate's public API).
fn logical(idx: usize) -> ModuleId {
    ModuleId::logical(idx)
}

/// logical module always sits at the highest index appended by
/// `factorization_for` / `factorization_with_residual_module`. Tests use this
/// instead of the historical `ModuleId::ResidualEntry` literal.
fn residual() -> ModuleId {
    ModuleId::logical(usize::MAX)
}

/// Canonical-path renderer for test partitions: the residual
/// sentinel renders as `residual`, explicit modules as `mod_<idx>`.
fn render(id: ModuleId) -> spec::ModulePath {
    let LogicalModuleIndex(idx) = id.0;
    let raw = if idx == usize::MAX {
        "residual".to_string()
    } else {
        format!("mod_{idx}")
    };
    spec::ModulePath::parse(&raw, "").unwrap()
}

fn member_bindings(members: &[BindingReport]) -> Vec<String> {
    members
        .iter()
        .map(|member| member.binding.to_string())
        .collect()
}

// --- Lazy rebind atomic-unit constraints --------------------------------

/// LazyRebind atomic-unit split: declarer and assigner of a
/// mutable binding must materialize together. `factor_assembly`
/// records this as an `atomic_unit_conflicts` entry on the
/// factorization; the materializer bails on any non-empty list.
#[test]
fn cross_destination_lazy_write_is_rejected() {
    let factorization = factorization_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", residual())],
    );
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected one atomic-unit conflict (A and B share a LazyRebind atomic unit but the spec splits them): {report:?}",
    );
    let conflict = &report.atomic_unit_conflicts[0];
    // Residual is now the synthesized logical module at index 1
    // (the explicit `mod_0` is at index 0).
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

/// Sorted distinct destination modules across a conflict's claims —
/// the typed equivalent of the prior string-rendered
/// `conflicting_modules` field on `AtomicUnitConflictReport`.
fn distinct_claim_modules(conflict: &AtomicUnitConflict) -> Vec<ModuleId> {
    let mut modules: Vec<ModuleId> = conflict
        .claims
        .iter()
        .map(|c| c.module)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    modules.sort();
    modules
}

#[test]
fn same_destination_lazy_write_is_allowed() {
    let factorization = factorization_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", logical(0))],
    );

    let report = factorization.validate();
    assert!(
        report.atomic_unit_conflicts.is_empty(),
        "same-destination rebinding writes should stay local to the emitted module: {report:?}",
    );
}

// --- Factorization helpers -----------------------------------------------

/// Resolve a sentinel `residual()` ModuleId to the real residual
/// logical-module index. Helper for `factorization_for`: in the new
/// no-variant world the residual is just a logical module, so
/// tests that used to write `ModuleId::ResidualEntry` now write
/// `residual()` (a `usize::MAX`-indexed sentinel) and let the
/// builder remap it.
fn resolve_test_module_id(id: ModuleId, residual_idx: usize) -> ModuleId {
    if id.0.0 == usize::MAX {
        ModuleId::logical(residual_idx)
    } else {
        id
    }
}

fn factorization_from_facts(
    facts: &[StatementFacts],
    bindings: HashMap<swc_ecma_ast::Id, BindingKind>,
    logical_modules: Vec<PlannedModule>,
    default_destination: ModuleId,
) -> ChunkFactorization {
    ChunkFactorization::build_with(
        "test_chunk".to_string(),
        compute_owner_graph_and_units_with(facts, OwnerGraphOptions::default())
            .expect("chunk facts declare a binding twice"),
        bindings,
        logical_modules,
        HashMap::new(),
        default_destination,
    )
}

fn factorization_for(source: &str, ownership: &[(&str, ModuleId)]) -> ChunkFactorization {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let mut max_idx: Option<usize> = None;
    for (_, id) in ownership {
        let LogicalModuleIndex(i) = id.0;
        if i == usize::MAX {
            continue;
        }
        max_idx = Some(max_idx.map_or(i, |m| m.max(i)));
    }
    let explicit_count = max_idx.map_or(0, |i| i + 1);
    let residual_idx = explicit_count;
    let mut bindings = HashMap::new();
    for (name, id) in ownership {
        let resolved = resolve_test_module_id(*id, residual_idx);
        bindings.insert(test_id(name), BindingKind::Owned { module: resolved });
    }
    let mut logical_modules: Vec<PlannedModule> = (0..explicit_count)
        .map(|i| PlannedModule {
            id: format!("mod_{i}"),
            target_file: format!("mod_{i}.js"),
            residual: false,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        })
        .collect();
    logical_modules.push(PlannedModule {
        id: "residual".to_string(),
        target_file: "residual/unhandled.js".to_string(),
        residual: true,
        rename_map: HashMap::new(),
        anonymous_statement_ordinals: Vec::new(),
    });
    factorization_from_facts(
        &facts,
        bindings,
        logical_modules,
        ModuleId::logical(residual_idx),
    )
}

fn factorization_with_residual_module(
    source: &str,
    residual_bindings: &[&str],
    logical_bindings: &[&str],
) -> ChunkFactorization {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let residual = logical(0);
    let logical = logical(1);
    let mut bindings = HashMap::new();
    for name in residual_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: residual });
    }
    for name in logical_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: logical });
    }
    let logical_modules = vec![
        PlannedModule {
            id: "residual".to_string(),
            target_file: "residual/unhandled.js".to_string(),
            residual: true,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        },
        PlannedModule {
            id: "mod_1".to_string(),
            target_file: "mod_1.js".to_string(),
            residual: false,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        },
    ];
    factorization_from_facts(&facts, bindings, logical_modules, residual)
}

// --- Owner graph quotient ------------------------------------------------

#[test]
fn owner_graph_report_emits_atomic_graph() {
    let factorization = factorization_with_residual_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1; const Existing = ResidualUse + 1;",
        &["Leaf", "ResidualUse"],
        &["Existing"],
    );

    let report = factorization.owner_graph_report();
    assert_eq!(report.atomic_graph.nodes.len(), 3);
    assert!(
        report
            .atomic_graph
            .nodes
            .iter()
            .any(
                |unit| member_bindings(&unit.members) == vec!["Leaf".to_string()]
                    && unit
                        .destinations
                        .iter()
                        .any(|destination| report.is_residual(destination))
            ),
        "Leaf should appear as a residual atomic unit: {:#?}",
        report.atomic_graph,
    );
    let json = serde_json::to_string(&report).expect("serialize OwnerGraphReport");
    assert!(json.contains(r#""atomic_graph""#));
}

/// `cli/gate.rs` reads the callee owner off the serialized `owner_graph.json` edge to
/// drop a promoted edge when callee and caller land in different modules.
#[test]
fn owner_graph_report_names_the_callee_owner_on_a_promoted_at_init_edge() {
    let factorization = factorization_with_residual_module(
        "const seed = 7; const result = callee(seed); function callee(x) { return dep + x; } const dep = 42;",
        &["seed", "result", "callee", "dep"],
        &[],
    );

    let report = factorization.owner_graph_report();
    let owner_of = |binding: &str| {
        report
            .nodes
            .iter()
            .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
            .unwrap_or_else(|| panic!("no owner declares `{binding}`: {:#?}", report.nodes))
            .id
            .clone()
    };
    let promoted = report
        .edges
        .iter()
        .find(|edge| {
            edge.source == owner_of("result")
                && edge.target == owner_of("dep")
                && edge.edge_kind == DepKind::EagerUse
        })
        .unwrap_or_else(|| {
            panic!(
                "no promoted edge from `result` to `dep`: {:#?}",
                report.edges
            )
        });
    assert_eq!(
        promoted.role,
        Some(EdgeRoleReport::PromotedAtInit {
            callee_owner: owner_of("callee")
        }),
    );
}

#[test]
fn atomic_graph_collapses_constraining_eager_cycle() {
    let factorization =
        factorization_with_residual_module("const A = B + 1; const B = A + 1;", &["A", "B"], &[]);

    let report = factorization.owner_graph_report();
    let cycle_unit = report
        .atomic_graph
        .nodes
        .iter()
        .find(|unit| member_bindings(&unit.members) == vec!["A".to_string(), "B".to_string()])
        .expect("A/B eager cycle should be one atomic unit");
    assert_eq!(cycle_unit.owner_ids.len(), 2);
    assert!(cycle_unit.causes.contains(&DepKind::EagerUse));
}

#[test]
fn atomic_graph_preserves_direction_between_atomic_units() {
    let factorization = factorization_with_residual_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1;",
        &["Leaf", "ResidualUse"],
        &[],
    );

    let report = factorization.owner_graph_report();
    assert_eq!(report.atomic_graph.nodes.len(), 2);
    assert_eq!(report.atomic_graph.edges.len(), 1);
    let edge = &report.atomic_graph.edges[0];
    assert_ne!(edge.source, edge.target);
    assert_eq!(edge.edge_kinds, vec![DepKind::EagerUse]);
    assert!(edge.constrains_init_order);
}

// --- has_side_effect refinement ------------------------------------------

fn has_side_effect_for(src: &str) -> Vec<bool> {
    let module = parse(src);
    analyze_facts(&module)
        .into_iter()
        .map(|f| !f.purity.is_pure())
        .collect()
}

#[test]
fn function_decl_is_not_side_effecting() {
    assert_eq!(
        has_side_effect_for("function f() { return io(); }"),
        vec![false]
    );
}

#[test]
fn class_decl_pure_without_static_init() {
    assert_eq!(
        has_side_effect_for("class C { m() { return io(); } }"),
        vec![false]
    );
    assert_eq!(
        has_side_effect_for("class C { static x = 1; }"),
        vec![false]
    );
    assert_eq!(
        has_side_effect_for("class C { static x = io(); }"),
        vec![true]
    );
    assert_eq!(has_side_effect_for("class C { static {} }"), vec![true]);
}

#[test]
fn bare_expression_classified_by_purity() {
    // Plain ident-read expression statement: pure.
    assert_eq!(has_side_effect_for("X;"), vec![false]);
    // Function call expression statement: side-effecting.
    assert_eq!(has_side_effect_for("io();"), vec![true]);
}

#[test]
fn multi_declarator_var_decl_is_side_effecting_if_any_init_is() {
    // After the comma-list pre-split, a multi-declarator
    // var-decl becomes one row per declarator. So a
    // mixed-purity comma-list produces both a Pure row and
    // an Impure row, not a single conservative row.
    assert_eq!(
        has_side_effect_for("const A = 1, B = compute();"),
        vec![false, true]
    );
    assert_eq!(
        has_side_effect_for("const A = 1, B = 2, C = 3;"),
        vec![false, false, false]
    );
}

// --- Comma-list splitter -------------------------------------------------

fn statement_kinds(source: &str) -> Vec<StatementKind> {
    let module = parse(source);
    analyze_facts(&module).into_iter().map(|f| f.kind).collect()
}

#[test]
fn non_var_decl_statements_are_not_split() {
    // function / class declarations have no comma-list shape.
    // Mixed source: const + function + class + bare expression.
    assert_eq!(
        statement_kinds("const A = 1; function f() {} class C {} 'side-effecting-string';"),
        vec![
            StatementKind::VarDecl,
            StatementKind::FnDecl,
            StatementKind::ClassDecl,
            StatementKind::SideEffect,
        ]
    );
}

// --- Comma-list source locations -----------------------------------------

#[test]
fn split_comma_list_assigns_per_declarator_source_ranges() {
    // Each post-split single-declarator owner must report just its
    // declarator's line range, not the parent statement's (for
    // `export const`, not the `ExportDecl` wrapper's). The proposer's
    // `size_lines_estimate` and the lane workers' `body_extraction`
    // per-owner snippets both rely on this.
    for source in [
        "const A = 1,\n      B = 2,\n      C = 3;\n",
        "export const A = 1,\n             B = 2,\n             C = 3;\n",
    ] {
        let (module, cm) = parse_with_source_map(source);
        let analysis = analyze_chunk(
            &module,
            &AnalysisHints::default(),
            Some("test.js"),
            |span| {
                (span != swc_common::DUMMY_SP).then(|| {
                    (
                        cm.lookup_char_pos(span.lo()).line,
                        cm.lookup_char_pos(span.hi()).line,
                        cm.lookup_char_pos(span.lo()).col_display + 1,
                    )
                })
            },
        );
        let lines: Vec<(usize, usize)> = analysis
            .facts
            .iter()
            .map(|f| {
                let loc = f
                    .source_location
                    .as_ref()
                    .expect("source_location should be populated");
                (loc.start_line, loc.end_line)
            })
            .collect();
        assert_eq!(
            lines,
            vec![(1, 1), (2, 2), (3, 3)],
            "each declarator should report only its own line for {source:?}",
        );
    }
}

// --- linker_order in FactorizationReport --------------------------------------

#[test]
fn validate_surfaces_linker_order_for_acyclic_spec() {
    // mod_0 reads B from mod_1 at-init → mod_1 must precede
    // mod_0 in the linker's evaluation order.
    let factorization = factorization_for(
        "const A = B + 1; const B = 42;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    let order = &report.linker_order;
    let pos = |name: &str| -> usize {
        order
            .iter()
            .position(|m| m.as_str() == name)
            .unwrap_or_else(|| panic!("module {name} not in {order:?}"))
    };
    assert!(
        pos("mod_1") < pos("mod_0"),
        "mod_1 must precede mod_0 in linker_order; got {order:?}",
    );
}

#[test]
fn validate_returns_empty_linker_order_for_cyclic_spec() {
    // Genuine cross-module constraining cycle: `A = B + 1` and
    // `B = A + 1` both read at-init. After the relaxed-predicate
    // routing of the validator (docs/design.md "Realizability
    // primitive"), the case has to actually produce a cycle in
    // the constraining-edge subgraph — mutual at-init reads do.
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    assert!(!report.cycles.is_empty(), "expected a cycle in {report:?}",);
    assert!(
        report.linker_order.is_empty(),
        "linker_order must be empty when the dep graph is cyclic; got {:?}",
        report.linker_order,
    );
}

// --- Atomic units ---------------------------------------------------------

fn atomic_units_for(source: &str) -> Vec<AtomicUnit> {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let owner_graph = build_owner_graph(&facts).unwrap();
    compute_atomic_units(&owner_graph)
}

fn unit_sizes(units: &[AtomicUnit]) -> Vec<usize> {
    let mut sizes: Vec<usize> = units.iter().map(|u| u.members.len()).collect();
    sizes.sort_unstable();
    sizes
}

fn assert_partitions_all_owners(units: &[AtomicUnit], total_owners: usize) {
    let summed: usize = units.iter().map(|u| u.members.len()).sum();
    assert_eq!(
        summed, total_owners,
        "atomic units must cover every owner exactly once; got units {units:?}",
    );
    let mut seen = BTreeSet::new();
    for unit in units {
        for owner in &unit.members {
            assert!(
                seen.insert(*owner),
                "owner {owner:?} appears in more than one atomic unit",
            );
        }
    }
}

#[test]
fn atomic_units_singletons_for_independent_owners() {
    // No edges → each owner is its own atomic unit.
    let units = atomic_units_for("const A = 1; const B = 2; const C = 3;");
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_eager_use_chain_stays_split() {
    // A → B → C via EagerUse: directed-only edges, no cycle, so
    // each owner remains its own unit.
    let units = atomic_units_for("const C = 3; const B = C + 1; const A = B + 1;");
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_lazy_use_cycle_stays_split() {
    // Two functions that reference each other lazily plus their
    // bindings — no constraining edges, so all four owners are
    // independent units.
    let units = atomic_units_for(
        "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
    );
    assert_partitions_all_owners(&units, 4);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1, 1]);
}

#[test]
fn atomic_units_sequenced_chain_stays_split() {
    // Three side-effecting top-level statements form a directed
    // Sequenced chain. A directed source-order edge alone is
    // satisfiable by linker order — no co-location forced — so
    // every owner stays in its own atomic unit. Co-location
    // would only kick in if some non-Sequenced edge ran in the
    // reverse direction.
    let units = atomic_units_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
    );
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_sequenced_plus_reverse_eager_merges() {
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
    // into one unit (Rebind bidirectional + Sequenced directed
    // form a cycle).
    let units = atomic_units_for(
        r#"let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);"#,
    );
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![3]);
}

#[test]
fn atomic_units_lazy_rebind_merges() {
    // `let A = 0; function B() { A = 1; }` produces a LazyRebind
    // edge from B → A. LazyRebind is bidirectional in `G_atomic`,
    // so A and B collapse into one unit.
    let units = atomic_units_for("let A = 0; function B() { A = 1; }");
    assert_partitions_all_owners(&units, 2);
    assert_eq!(unit_sizes(&units), vec![2]);
}

// --- Factor assembly -----------------------------------------------------

fn partition_summary(factorization: &ChunkFactorization) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = factorization
        .analysis
        .owner_graph()
        .iter_nodes()
        .map(|node| {
            let declared: Vec<String> = node.declared.iter().map(|id| id.0.to_string()).collect();
            let key = if declared.is_empty() {
                format!("stmt_{}", node.statement_ordinal.0)
            } else {
                declared.join(",")
            };
            let dest = factorization.partition.of(node.id);
            // Residual modules render as `<residual>` so this
            // summary stays stable across residual-index changes;
            // explicit modules use their `mod_<idx>` label.
            let LogicalModuleIndex(idx) = dest.0;
            let label = match factorization
                .analysis
                .logical_module(LogicalModuleIndex(idx))
            {
                Some(m) if m.residual => "<residual>".to_string(),
                _ => render(dest).to_string(),
            };
            (key, label)
        })
        .collect();
    out.sort();
    out
}

#[test]
fn factor_assembly_unclaimed_owners_default_to_residual() {
    let factorization = factorization_for("const A = 1; const B = 2;", &[]);
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "<residual>".to_string()),
            ("B".to_string(), "<residual>".to_string()),
        ],
    );
}

#[test]
fn factor_assembly_single_claim_with_unclaimed_unit_members_is_a_conflict() {
    // `const A = B + 1; const B = A + 1;` is one EagerUse cycle —
    // a single atomic unit. Claiming A for mod_0 leaves B
    // defaulting to residual entry, which splits the unit
    // across {mod_0, <residual_entry>} — unrealizable. The spec
    // author needs to either also assign B (or leave both
    // unassigned), or remove the constraining edge that fused
    // them in the first place. `debundle coverage` may flag the
    // split unit as an advisory edit, but factor_assembly refuses
    // to silently move B for the user.
    let factorization =
        factorization_for("const A = B + 1; const B = A + 1;", &[("A", logical(0))]);
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected the half-claimed EagerUse cycle to surface as an atomic-unit conflict: {report:?}",
    );
    // Residual is the synthesized logical module appended after
    // the explicit `mod_0`.
    assert_eq!(
        distinct_claim_modules(&report.atomic_unit_conflicts[0]),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

#[test]
fn factor_assembly_concordant_claims_within_unit_are_fine() {
    // Both members of the same atomic unit claimed for the same
    // module — that's the spec author being explicit, not a
    // conflict.
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(0))],
    );
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_0".to_string()),
        ],
    );
}

#[test]
fn factor_assembly_records_conflict_on_split_eager_use_cycle() {
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected the A↔B EagerUse cycle to surface as an atomic-unit conflict: {report:?}",
    );
    let conflict = &report.atomic_unit_conflicts[0];
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

#[test]
fn factor_assembly_records_no_conflict_for_sequenced_only_chain() {
    // Three side-effect statements, source-ordered. Directed
    // Sequenced edges alone form a chain in `G_atomic`, not an
    // SCC, so every owner is its own atomic unit and the spec
    // can split them across modules without violating
    // co-location. The validator may still flag a module-level
    // cycle when the spec creates one through reverse claims,
    // but factor_assembly itself does not panic / record a
    // conflict here.
    let factorization = factorization_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
        &[("a1", logical(0)), ("b1", logical(1)), ("a2", logical(0))],
    );
    let report = factorization.validate();
    assert!(
        report.atomic_unit_conflicts.is_empty(),
        "Sequenced-only chains never force co-location: {report:?}",
    );
}

#[test]
fn factor_assembly_independent_owners_keep_independent_claims() {
    // Three eager-use chain: A → B → C, no cycle, three atomic
    // units. Each owner's claim takes effect independently.
    let factorization = factorization_for(
        "const C = 3; const B = C + 1; const A = B + 1;",
        &[("A", logical(0)), ("B", logical(1)), ("C", logical(0))],
    );
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_1".to_string()),
            ("C".to_string(), "mod_0".to_string()),
        ],
    );
}
