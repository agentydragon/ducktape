mod tests {
    use std::collections::{BTreeSet, HashMap};

    use crate::facts::{compute_shadowed_globals, top_level_item_views};
    use crate::purity::{ChunkCodeGraph, Purity, classify_expr_purity};
    use crate::*;
    use swc_common::{FileName, sync::Lrc};
    use swc_ecma_ast::*;
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn parse(source: &str) -> Module {
        let cm: Lrc<swc_common::SourceMap> = Default::default();
        let fm = cm.new_source_file(
            FileName::Custom("test.js".into()).into(),
            source.to_string(),
        );
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer).parse_module().unwrap()
    }

    #[test]
    fn function_body_reads_are_lazy() {
        let module = parse("function f() { return X; } const Y = 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert_eq!(facts.len(), 2);
        // f() declares "f"; its body reference to X is lazy.
        assert_eq!(
            facts[0].declared,
            ["f"].iter().map(|s| s.to_string()).collect()
        );
        assert!(!facts[0].reads_at_init.contains("X"));
        assert_eq!(facts[0].kind, StatementKind::FnDecl);
        // Y declares "Y"; init is `1` (no reads).
        assert_eq!(
            facts[1].declared,
            ["Y"].iter().map(|s| s.to_string()).collect()
        );
        assert!(facts[1].reads_at_init.is_empty());
    }

    #[test]
    fn class_extends_clause_reads_at_init() {
        let module = parse("class B extends A { run() { return X; } }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert_eq!(facts.len(), 1);
        // extends A is eager; method body reference to X is lazy.
        assert!(facts[0].reads_at_init.contains("A"));
        assert!(!facts[0].reads_at_init.contains("X"));
    }

    #[test]
    fn computed_key_reads_at_init() {
        let module = parse("const M = { [k.foo]: 1 };");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        // The key expression `k.foo` reads `k` at-init.
        assert!(facts[0].reads_at_init.contains("k"));
    }

    #[test]
    fn class_static_init_reads_at_init() {
        let module = parse("class C { static x = Y; }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert!(facts[0].reads_at_init.contains("Y"));
    }

    #[test]
    fn class_instance_init_is_lazy() {
        let module = parse("class C { x = Y; }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        // Instance field initializer evaluates per-instance, not at
        // class-decl time.
        assert!(!facts[0].reads_at_init.contains("Y"));
    }

    fn logical(idx: usize) -> ModuleId {
        ModuleId::Logical(LogicalModuleIndex(idx))
    }

    fn render(id: ModuleId) -> String {
        match id {
            ModuleId::Logical(LogicalModuleIndex(idx)) => format!("mod_{idx}"),
            ModuleId::ResidualEntry => "<residual>".to_string(),
        }
    }

    fn member_bindings(members: &[BindingReport]) -> Vec<String> {
        members
            .iter()
            .map(|member| member.binding.clone())
            .collect()
    }

    #[test]
    fn binding_table_interns_stable_chunk_local_ids() {
        let mut table = BindingTable::default();
        let alpha = table.intern("alpha".to_string());
        let beta = table.intern("beta".to_string());
        let alpha_again = table.intern("alpha".to_string());

        assert_eq!(alpha, alpha_again);
        assert_ne!(alpha, beta);
        assert_eq!(table.get("alpha"), Some(alpha));
        assert_eq!(table.name(beta).map(String::as_str), Some("beta"));
        assert_eq!(table.len(), 2);
    }

    #[test]
    fn cycle_detected_between_two_modules() {
        // mod_a owns A; A's init reads B (owned by mod_b).
        // mod_b owns B; B's init reads A (owned by mod_a).
        let module = parse("const A = B + 1; const B = A + 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = HashMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        let owner_graph = build_owner_graph(&facts);
        let partition = Partition::from_binding_assignment(&owner_graph, &binding_assignment);
        let graph = build_module_dep_graph(&owner_graph, &partition);
        let report = validate_schedule(&graph, &render);
        assert_eq!(report.cycles.len(), 1);
        assert_eq!(report.cycles[0].modules.len(), 2);
    }

    #[test]
    fn dag_has_no_cycles() {
        let module = parse("const A = 1; const B = A + 1; const C = B + A;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = HashMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        binding_assignment.insert("C".to_string(), logical(2));
        let owner_graph = build_owner_graph(&facts);
        let partition = Partition::from_binding_assignment(&owner_graph, &binding_assignment);
        let graph = build_module_dep_graph(&owner_graph, &partition);
        let report = validate_schedule(&graph, &render);
        assert!(
            report.cycles.is_empty(),
            "expected no cycles, got {:?}",
            report.cycles
        );
    }

    /// Pin the cut behavior for the canonical mixed cycle: 2-module
    /// SCC with one lazy forward-edge and one at-init back-edge.
    /// The cut should contain exactly the at-init back-edge — lazy
    /// edges aren't realizability-constraining and removing one
    /// can't fix the cycle.
    #[test]
    fn cut_excludes_lazy_edges_in_mixed_cycle() {
        // mod_0 owns A and readB; readB body returns B (lazy read).
        // mod_1 owns B; B = A + 1 (at-init read of A).
        // R-edge: mod_1 → mod_0 (kind = at-init, binding = A).
        // L-edge: mod_0 → mod_1 (kind = lazy, binding = B).
        let module = parse("const A = 1; function readB() { return B; } const B = A + 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = HashMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("readB".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        let owner_graph = build_owner_graph(&facts);
        let partition = Partition::from_binding_assignment(&owner_graph, &binding_assignment);
        let graph = build_module_dep_graph(&owner_graph, &partition);
        let report = validate_schedule(&graph, &render);
        assert_eq!(
            report.cycles.len(),
            1,
            "expected one cycle, got {:?}",
            report.cycles,
        );
        let cycle = &report.cycles[0];
        assert!(
            cycle.evidence.iter().any(|e| e.kind == EdgeKind::LazyRead),
            "evidence should include the lazy edge, got {:?}",
            cycle.evidence,
        );
        assert!(
            !cycle.cut.iter().any(|e| e.kind == EdgeKind::LazyRead),
            "cut must not include lazy reasons, got {:?}",
            cycle.cut,
        );
        assert_eq!(
            cycle.cut.len(),
            1,
            "min cut for a single mixed cycle is one edge, got {:?}",
            cycle.cut,
        );
        let entry = &cycle.cut[0];
        assert_eq!(entry.from, "mod_1");
        assert_eq!(entry.to, "mod_0");
        assert_eq!(entry.binding.as_deref(), Some("A"));
        assert_eq!(entry.kind, EdgeKind::AtInitRead);
    }

    /// Pure-S cycle: cut consists of side-effect reasons; no
    /// lazy or at-init reasons should appear.
    #[test]
    fn cut_emits_side_effect_edges_for_s_only_cycle() {
        // Three side-effecting `globalThis.tag = ...` writes
        // interleaved across mod_0 (ord 0, 2) and mod_1 (ord 1).
        // S-edges: mod_0 → mod_1 (ord 0 < ord 1) and
        // mod_1 → mod_0 (ord 1 < ord 2). Cycle.
        let module = parse(
            r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
        );
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = HashMap::new();
        binding_assignment.insert("a1".to_string(), logical(0));
        binding_assignment.insert("a2".to_string(), logical(0));
        binding_assignment.insert("b1".to_string(), logical(1));
        let owner_graph = build_owner_graph(&facts);
        let partition = Partition::from_binding_assignment(&owner_graph, &binding_assignment);
        let graph = build_module_dep_graph(&owner_graph, &partition);
        let report = validate_schedule(&graph, &render);
        assert_eq!(report.cycles.len(), 1);
        let cycle = &report.cycles[0];
        assert!(
            !cycle.cut.is_empty(),
            "cut should be non-empty for an unrealizable cycle, got {:?}",
            cycle.cut,
        );
        assert!(
            cycle
                .cut
                .iter()
                .all(|e| e.kind == EdgeKind::SideEffectOrder),
            "S-only cycle cut should be all side-effect reasons, got {:?}",
            cycle.cut,
        );
    }

    /// Lazy-only cycle: realizability gate accepts it, so no
    /// CycleReport is emitted and there's no cut to compute.
    #[test]
    fn cut_is_absent_for_lazy_only_cycle() {
        // mod_0 owns helperA, A; mod_1 owns helperB, B. Both
        // helpers reference the other module's binding lazily;
        // no cross-module at-init or side-effect edges.
        let module = parse(
            "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
        );
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = HashMap::new();
        binding_assignment.insert("helperA".to_string(), logical(0));
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("helperB".to_string(), logical(1));
        binding_assignment.insert("B".to_string(), logical(1));
        let owner_graph = build_owner_graph(&facts);
        let partition = Partition::from_binding_assignment(&owner_graph, &binding_assignment);
        let graph = build_module_dep_graph(&owner_graph, &partition);
        let report = validate_schedule(&graph, &render);
        assert!(
            report.cycles.is_empty(),
            "lazy-only cycle is realizable; the gate must accept and emit no cycle (got {:?})",
            report.cycles,
        );
    }

    #[test]
    fn cross_destination_lazy_write_is_rejected() {
        let schedule = schedule_for(
            "let A = 0; function B() { A = 1; }",
            &[("A", logical(0)), ("B", ModuleId::ResidualEntry)],
        );

        let report = schedule.validate();
        assert_eq!(
            report.cross_destination_assignments.len(),
            1,
            "expected residual B's assignment to A to be rejected: {report:?}",
        );
        let assignment = &report.cross_destination_assignments[0];
        assert_eq!(assignment.binding, "A");
        assert_eq!(assignment.assigner_module, "<residual_entry>");
        assert_eq!(assignment.binding_module, "mod_0");
        assert_eq!(assignment.kind, EdgeKind::LazyWrite);
    }

    #[test]
    fn same_destination_lazy_write_is_allowed() {
        let schedule = schedule_for(
            "let A = 0; function B() { A = 1; }",
            &[("A", logical(0)), ("B", logical(0))],
        );

        let report = schedule.validate();
        assert!(
            report.cross_destination_assignments.is_empty(),
            "same-destination rebinding writes should stay local to the emitted module: {report:?}",
        );
    }

    fn schedule_for(source: &str, ownership: &[(&str, ModuleId)]) -> Schedule {
        let module = parse(source);
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut bindings = HashMap::new();
        let mut max_idx = 0usize;
        for (name, id) in ownership {
            bindings.insert(name.to_string(), BindingKind::Owned { owner: *id });
            if let ModuleId::Logical(LogicalModuleIndex(i)) = id {
                max_idx = max_idx.max(*i);
            }
        }
        let logical_modules: Vec<LogicalModule> = (0..=max_idx)
            .map(|i| LogicalModule {
                id: format!("mod_{i}"),
                target_file: format!("mod_{i}.js"),
                residual: false,
                rename_map: HashMap::new(),
                anonymous_statement_ordinals: Vec::new(),
            })
            .collect();
        Schedule::build(
            "test_chunk".to_string(),
            facts,
            bindings,
            logical_modules,
            HashMap::new(),
        )
    }

    fn schedule_with_residual_module(
        source: &str,
        residual_bindings: &[&str],
        logical_bindings: &[&str],
    ) -> Schedule {
        let module = parse(source);
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let residual = logical(0);
        let logical = logical(1);
        let mut bindings = HashMap::new();
        for name in residual_bindings {
            bindings.insert(name.to_string(), BindingKind::Owned { owner: residual });
        }
        for name in logical_bindings {
            bindings.insert(name.to_string(), BindingKind::Owned { owner: logical });
        }
        let logical_modules = vec![
            LogicalModule {
                id: "residual".to_string(),
                target_file: "residual/unhandled.js".to_string(),
                residual: true,
                rename_map: HashMap::new(),
                anonymous_statement_ordinals: Vec::new(),
            },
            LogicalModule {
                id: "mod_1".to_string(),
                target_file: "mod_1.js".to_string(),
                residual: false,
                rename_map: HashMap::new(),
                anonymous_statement_ordinals: Vec::new(),
            },
        ];
        Schedule::build(
            "test_chunk".to_string(),
            facts,
            bindings,
            logical_modules,
            HashMap::new(),
        )
    }

    #[test]
    fn owner_graph_retains_reads_to_unassigned_declared_bindings() {
        let schedule = schedule_for("const A = X + 1; const X = 42;", &[("A", logical(0))]);

        assert!(
            schedule.owner_graph.edges.iter().any(|edge| {
                edge.from == OwnerId(0)
                    && edge.to == OwnerId(1)
                    && edge.reason.kind == EdgeKind::AtInitRead
                    && edge.reason.statement_ordinal == StatementOrdinal(0)
                    && schedule.binding_name(edge.reason.binding.unwrap()) == "X"
            }),
            "owner graph should retain the unassigned declared provider edge",
        );
        assert!(
            schedule
                .dep_graph
                .graph
                .contains_edge(logical(0), ModuleId::ResidualEntry),
            "the quotient should expose the logical-module -> residual read",
        );

        let report = schedule.owner_graph_report();
        let residual_owner = report
            .nodes
            .iter()
            .find(|node| node.id == "owner:1")
            .expect("X owner should be reported");
        assert_eq!(residual_owner.destination.id, "residual");
    }

    #[test]
    fn peelability_reports_symbols_currently_peelable_from_residual() {
        let schedule = schedule_with_residual_module(
            "const Leaf = 1; const ResidualUse = Leaf + 1; const Existing = ResidualUse + 1;",
            &["Leaf", "ResidualUse"],
            &["Existing"],
        );

        let report = schedule.owner_graph_report();
        assert_eq!(report.peelability.residual_destinations.len(), 1);
        assert_eq!(
            report.peelability.residual_destinations[0].label,
            "residual"
        );
        let leaf_horizon = report
            .peelability
            .residual_owner_horizon
            .iter()
            .find(|owner| member_bindings(&owner.members) == vec!["Leaf".to_string()])
            .expect("Leaf horizon should be reported");
        assert_eq!(leaf_horizon.status, ResidualOwnerPeelStatus::Direct);
        assert_eq!(leaf_horizon.peel_set_ids.len(), 1);
        assert!(leaf_horizon.companion_options.is_empty());
        assert_eq!(leaf_horizon.statement_ordinal, StatementOrdinal(0));
        assert_eq!(leaf_horizon.statement_kind, StatementKind::VarDecl);
        assert_eq!(leaf_horizon.current_destination.label, "residual");
        assert!(
            report
                .peelability
                .minimal_peel_sets
                .iter()
                .any(
                    |set| member_bindings(&set.members) == vec!["Leaf".to_string()]
                        && set.owner_ids.len() == 1
                ),
            "Leaf should appear as a singleton peel set: {:#?}",
            report.peelability,
        );
    }

    #[test]
    fn peelability_allows_symbol_with_lazy_only_residual_dependency() {
        let schedule = schedule_for("function Leaf() { return Dep; } const Dep = 1;", &[]);

        let report = schedule.owner_graph_report();
        let leaf_horizon = report
            .peelability
            .residual_owner_horizon
            .iter()
            .find(|owner| member_bindings(&owner.members) == vec!["Leaf".to_string()])
            .expect("Leaf horizon should be reported");
        assert_eq!(leaf_horizon.status, ResidualOwnerPeelStatus::Direct);
        assert!(
            leaf_horizon.companion_options.is_empty(),
            "Leaf needs no companion when its residual edge is lazy-only: {:#?}",
            report.peelability,
        );
        assert!(
            report.peelability.minimal_peel_sets.iter().any(|closure| {
                member_bindings(&closure.members) == vec!["Leaf".to_string()]
                    && closure.owner_ids.len() == 1
            }),
            "Leaf should be peelable as a singleton when only lazy-reading residual Dep: {:#?}",
            report.peelability,
        );
    }

    #[test]
    fn peelability_blocks_residual_symbol_that_would_create_constraining_scc() {
        let schedule =
            schedule_with_residual_module("const A = B + 1; const B = A + 1;", &["A", "B"], &[]);

        let report = schedule.owner_graph_report();
        let a_horizon = report
            .peelability
            .residual_owner_horizon
            .iter()
            .find(|owner| member_bindings(&owner.members) == vec!["A".to_string()])
            .expect("A horizon should be reported");
        assert_eq!(a_horizon.status, ResidualOwnerPeelStatus::WithCompanions);
        assert!(
            a_horizon.companion_options.iter().any(|option| {
                member_bindings(&option.companion_members) == vec!["B".to_string()]
            }),
            "A should point at B as a required companion: {:#?}",
            report.peelability,
        );
        assert!(
            report
                .peelability
                .minimal_peel_sets
                .iter()
                .any(|closure| member_bindings(&closure.members)
                    == vec!["A".to_string(), "B".to_string()]),
            "pair closure summary should include A+B: {:#?}",
            report.peelability,
        );
    }

    #[test]
    fn peelability_does_not_overclaim_pair_when_three_owner_cycle_remains() {
        let schedule = schedule_with_residual_module(
            "const A = B + 1; const B = C + 1; const C = A + 1;",
            &["A", "B", "C"],
            &[],
        );

        let report = schedule.owner_graph_report();
        assert!(
            report.peelability.minimal_peel_sets.is_empty(),
            "two-owner closures should not be reported when any pair remains cyclic: {:#?}",
            report.peelability,
        );
        assert!(
            report
                .peelability
                .residual_owner_horizon
                .iter()
                .all(|owner| owner.status == ResidualOwnerPeelStatus::Blocked),
            "three-owner at-init cycle should not expose direct or companion peels: {:#?}",
            report.peelability,
        );
        assert!(
            report
                .peelability
                .residual_owner_horizon
                .iter()
                .all(|owner| owner.companion_options.is_empty()),
            "no pair should be reported as peelable for a three-owner cycle: {:#?}",
            report.peelability,
        );
    }

    // --- Purity classifier ---------------------------------------------------

    fn classify(src: &str) -> Purity {
        // Wrap the expression in a const so we can parse a module.
        let module = parse(&format!("const _ = {src};"));
        let var = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected `const _ = ...;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(
            init,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
    }

    /// Run the classifier against `src` after computing the
    /// chunk-top-level shadowed-globals set from a wrapping
    /// module. Lets tests check the shadowing fallback.
    fn classify_with_module(prefix: &str, expr_src: &str) -> Purity {
        let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let var = match module.body.last().expect("non-empty body") {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(
            init,
            &shadowed,
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
    }

    /// Run the classifier against `src` with both shadowing and an
    /// explicit declared-pure binding set.
    fn classify_with_declared_pure(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
        let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let declared_pure: BTreeSet<String> = declared.iter().map(|s| (*s).to_string()).collect();
        let var = match module.body.last().expect("non-empty body") {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(init, &shadowed, &declared_pure, &ChunkCodeGraph::default())
    }

    #[test]
    fn classify_literal_kinds_are_pure() {
        assert!((classify("42")).is_pure());
        assert!((classify("\"hi\"")).is_pure());
        assert!((classify("true")).is_pure());
        assert!((classify("null")).is_pure());
        assert!((classify("/foo/g")).is_pure());
        assert!((classify("`literal`")).is_pure());
    }

    #[test]
    fn classify_ident_read_is_pure() {
        assert!((classify("FOO")).is_pure());
    }

    #[test]
    fn classify_pure_unary_and_binary() {
        assert!((classify("-1")).is_pure());
        assert!((classify("!FOO")).is_pure());
        assert!((classify("typeof FOO")).is_pure());
        assert!((classify("A + 1")).is_pure());
        assert!((classify("A && B")).is_pure());
        assert!((classify("A ? B : C")).is_pure());
    }

    #[test]
    fn classify_delete_is_impure() {
        assert!(!(classify("delete o.x")).is_pure());
    }

    #[test]
    fn classify_assignment_and_update_are_impure() {
        assert!(!(classify("(x = 1)")).is_pure());
        assert!(!(classify("x++")).is_pure());
    }

    #[test]
    fn classify_call_new_tagged_template_are_unknown() {
        assert!(!(classify("foo()")).is_pure());
        assert!(!(classify("new Foo()")).is_pure());
        assert!(!(classify("tag`hi ${x}`")).is_pure());
    }

    #[test]
    fn classify_member_access_is_unknown() {
        assert!(!(classify("o.x")).is_pure());
        assert!(!(classify("o[k]")).is_pure());
        assert!(!(classify("o?.x")).is_pure());
    }

    #[test]
    fn classify_object_literal_pure_when_props_pure() {
        assert!((classify("({ a: 1, b: 'x' })")).is_pure());
        assert!((classify("({ [k]: 1 })")).is_pure());
        // Computed key with member access — getter could fire.
        assert!(!(classify("({ [k.x]: 1 })")).is_pure());
        // Spread of an arbitrary expr — iterator could fire.
        assert!(!(classify("({ ...other })")).is_pure());
        // Method definitions are pure (defining, not calling).
        assert!((classify("({ m() { return io(); } })")).is_pure());
    }

    #[test]
    fn classify_array_literal_pure_when_elements_pure() {
        assert!((classify("[1, 2, 'x']")).is_pure());
        assert!((classify("[A, B]")).is_pure());
        assert!(!(classify("[1, foo()]")).is_pure());
        // Spread is `Unknown` even on an array literal.
        assert!(!(classify("[...other]")).is_pure());
    }

    #[test]
    fn classify_function_and_arrow_are_pure() {
        assert!((classify("function () { return io(); }")).is_pure());
        assert!((classify("() => io()")).is_pure());
    }

    #[test]
    fn classify_class_expr_pure_without_static_init() {
        assert!((classify("class { m() { return io(); } }")).is_pure());
        assert!((classify("class { static x = 1 }")).is_pure());
        assert!(!(classify("class { static x = io() }")).is_pure());
        assert!(!(classify("class { static {} }")).is_pure());
    }

    #[test]
    fn classify_template_with_pure_exprs_is_pure() {
        assert!((classify("`a${A}b${1+2}c`")).is_pure());
        assert!(!(classify("`a${foo()}`")).is_pure());
    }

    #[test]
    fn classify_sequence_takes_worst() {
        assert!((classify("(A, B, C)")).is_pure());
        assert!(!(classify("(A, foo(), C)")).is_pure());
        assert!(!(classify("(A, x = 1, C)")).is_pure());
    }

    // --- Whitelist: pure static property reads -------------------------------

    #[test]
    fn whitelist_static_props_are_pure() {
        // Math / Number / Symbol constants: pure internal-slot
        // reads, no coercion.
        assert!((classify("Math.PI")).is_pure());
        assert!((classify("Math.E")).is_pure());
        assert!((classify("Math.SQRT2")).is_pure());
        assert!((classify("Number.EPSILON")).is_pure());
        assert!((classify("Number.MAX_SAFE_INTEGER")).is_pure());
        assert!((classify("Symbol.iterator")).is_pure());
        assert!((classify("Symbol.toStringTag")).is_pure());
    }

    #[test]
    fn whitelist_misses_fall_back_to_unknown() {
        // Same receivers, properties that aren't on the whitelist:
        // could be a getter / a coercing call. Stays Unknown.
        assert!(!(classify("Math.unknownProp")).is_pure());
        assert!(!(classify("Number.unknownProp")).is_pure());
        assert!(!(classify("Symbol.unknownProp")).is_pure());
    }

    // --- Whitelist: pure calls -----------------------------------------------

    #[test]
    fn whitelist_static_calls_are_pure_regardless_of_arg() {
        // Type predicates do not coerce or read user props on the
        // argument, so any Pure-classified arg keeps the call Pure.
        assert!((classify("Array.isArray(x)")).is_pure());
        assert!((classify("Array.isArray([1, 2, 3])")).is_pure());
        assert!((classify("Number.isNaN(x)")).is_pure());
        assert!((classify("Number.isFinite(x)")).is_pure());
        assert!((classify("Number.isInteger(x)")).is_pure());
        assert!((classify("Number.isSafeInteger(x)")).is_pure());
    }

    #[test]
    fn whitelist_static_calls_unknown_arg_infects() {
        // An argument whose evaluation may itself fire user code
        // poisons the whole call: even though `Array.isArray` is
        // a pure operation, evaluating `io()` first is not.
        assert!(!(classify("Array.isArray(io())")).is_pure());
        assert!(!(classify("Number.isNaN(o.x)")).is_pure());
    }

    // --- PURE_STATIC_FUNCTION_REFS: read-vs-call distinction ---------------

    #[test]
    fn static_function_ref_object_aliases_are_pure() {
        // Bare member READS access own data properties of the
        // built-in `Object` per ECMA-262 §20.1.2 — no getter
        // fires, no observable side effect. Aliasing the function
        // value into a binding stays pure (the value isn't called).
        assert!((classify("Object.defineProperty")).is_pure());
        assert!((classify("Object.freeze")).is_pure());
        assert!((classify("Object.values")).is_pure());
        assert!((classify("Object.keys")).is_pure());
    }

    #[test]
    fn static_function_ref_object_calls_remain_unknown() {
        // The CALL form of each function-ref entry is unsafe (see
        // `PURE_STATIC_FUNCTION_REFS` doc-comment for why each is
        // excluded from `PURE_STATIC_CALLS`). The function-ref
        // entry only opens the read path; the call must stay
        // Unknown so the soundness contract holds.
        assert!(!(classify("Object.defineProperty(t, 'k', { value: 1 })")).is_pure());
        assert!(!(classify("Object.freeze({ x: 1 })")).is_pure());
        assert!(!(classify("Object.values(o)")).is_pure());
        assert!(!(classify("Object.keys(o)")).is_pure());
    }

    #[test]
    fn static_function_ref_object_shadowed_falls_back_to_unknown() {
        // `Object` joins WHITELIST_RECEIVERS in this PR; if the
        // chunk shadows it (via a top-level decl OR an import
        // specifier per A8), the function-ref read must fall back
        // to Unknown — `Object.X` then resolves through the
        // user-bound value.
        assert!(
            !(classify_with_module("const Object = userland;", "Object.defineProperty")).is_pure()
        );
        assert!(
            !(classify_with_module(
                r#"import { Object } from "./userland.js";"#,
                "Object.freeze"
            ))
            .is_pure()
        );
    }

    #[test]
    fn whitelist_global_callables_are_pure() {
        // Boolean(x) is `ToBoolean(x)`; per spec, no path fires
        // user code (objects → true unconditionally; primitives
        // are case-analysed structurally).
        assert!((classify("Boolean(x)")).is_pure());
        assert!((classify("Boolean(0)")).is_pure());
        assert!((classify("Boolean({})")).is_pure());
    }

    #[test]
    fn unsafe_global_callables_stay_unknown() {
        // ToNumber / ToString / ToPrimitive can call user
        // `valueOf` / `toString` / `[Symbol.toPrimitive]` on
        // object args; we don't track types, so these remain
        // Unknown to keep the whitelist sound.
        assert!(!(classify("Number(x)")).is_pure());
        assert!(!(classify("String(x)")).is_pure());
        assert!(!(classify("Symbol(x)")).is_pure());
        assert!(!(classify("parseInt(x, 10)")).is_pure());
        assert!(!(classify("parseFloat(x)")).is_pure());
        assert!(!(classify("isNaN(x)")).is_pure());
        assert!(!(classify("isFinite(x)")).is_pure());
    }

    #[test]
    fn unsafe_static_calls_stay_unknown() {
        // Anything that coerces / iterates / fires getters /
        // mutates / reads through proxies is *not* on the
        // whitelist. These all stay Unknown.
        for src in [
            "Array.from(x)",
            "Array.of(1, 2, 3)",
            "Math.abs(x)",
            "Math.max(1, 2)",
            "Math.floor(x)",
            "Math.round(x)",
            "Math.sqrt(x)",
            "Object.keys(x)",
            "Object.values(x)",
            "Object.entries(x)",
            "Object.freeze(x)",
            "Object.assign({}, x)",
            "Object.fromEntries(x)",
            "Object.getOwnPropertyDescriptor(x, 'k')",
            "Object.hasOwn(x, 'k')",
            "JSON.parse(x)",
            "JSON.stringify(x)",
            "Number.parseInt(x)",
            "Number.parseFloat(x)",
            "String.fromCharCode(65)",
            "String.fromCodePoint(65)",
            "Symbol.for('k')",
            "Symbol.keyFor(s)",
        ] {
            assert!(
                !(classify(src)).is_pure(),
                "expected {src} to stay Unknown (would fire user code)"
            );
        }
    }

    // --- Whitelist: shadowing fallback ---------------------------------------

    #[test]
    fn shadowed_receiver_disables_whitelist() {
        // A chunk-top-level binding for `Math` makes `Math.PI` no
        // longer reach the global; the whitelist must fall back
        // to Unknown.
        assert!(!(classify_with_module("const Math = userland;", "Math.PI")).is_pure());
        assert!(!(classify_with_module("function Math() {}", "Math.E")).is_pure());
        assert!(!(classify_with_module("const Array = X;", "Array.isArray(x)")).is_pure());
        assert!(!(classify_with_module("let Number = X;", "Number.isNaN(x)")).is_pure());
        assert!(!(classify_with_module("const Boolean = X;", "Boolean(x)")).is_pure());
    }

    #[test]
    fn unshadowed_receiver_keeps_whitelist() {
        // A chunk that declares an unrelated binding leaves the
        // whitelist active — only same-named shadowing disables.
        assert!((classify_with_module("const other = userland;", "Math.PI")).is_pure());
    }

    #[test]
    fn import_specifier_locals_shadow_whitelist() {
        // Import bindings are top-level lexical decls and shadow
        // the global the same way `const Math = …` does. The
        // classifier must reach the same Unknown fallback. (Soundness
        // matters: the imported value can be anything, so
        // `<imported>.<prop>` is a property read that may fire a
        // user-defined getter.)
        assert!(
            !(classify_with_module(r#"import { Math } from "./userland.js";"#, "Math.PI"))
                .is_pure()
        );
        assert!(
            !(classify_with_module(r#"import Boolean from "./userland.js";"#, "Boolean(x)"))
                .is_pure()
        );
        assert!(
            !(classify_with_module(
                r#"import * as Number from "./userland.js";"#,
                "Number.isNaN(x)"
            ))
            .is_pure()
        );
        assert!(
            !(classify_with_module(
                r#"import { something as Array } from "./userland.js";"#,
                "Array.isArray(x)"
            ))
            .is_pure()
        );
    }

    // --- Declared purity (spec annotation) ---------------------------------

    #[test]
    fn declared_pure_ident_call_classifies_pure() {
        // A spec member with `purity: "pure"` populates the
        // declared-pure set. A call whose callee is the bound
        // Ident classifies Pure regardless of the body content
        // (the validator does not re-verify; author trust). Args
        // are still evaluated normally — pure args here, so the
        // whole call is Pure.
        assert!(
            (classify_with_declared_pure("function f(x) { return x; }", "f(42)", &["f"])).is_pure()
        );
        assert!(
            (classify_with_declared_pure("function f(x) { return x; }", "f({ k: 'v' })", &["f"]))
                .is_pure()
        );
    }

    #[test]
    fn declared_pure_call_with_impure_arg_inherits_arg_purity() {
        // The declared-purity contract covers the function value;
        // arg evaluation is independent. An impure arg makes the
        // whole call Unknown.
        assert!(
            !(classify_with_declared_pure(
                "function f(x) { return x; } function io() { return 1; }",
                "f(io())",
                &["f"]
            ))
            .is_pure()
        );
    }

    #[test]
    fn declared_pure_overrides_global_shadowing() {
        // Author trust contract: a declared-pure annotation wins
        // over both the whitelist's shadowing fallback and the
        // body's actual contents. The validator does not
        // second-guess.
        assert!(
            (classify_with_declared_pure(
                r#"import { Boolean } from "./userland.js";"#,
                "Boolean(x)",
                &["Boolean"]
            ))
            .is_pure()
        );
    }

    #[test]
    fn declared_pure_does_not_bleed_to_unannotated_callees() {
        // Only the listed binding is treated pure. A call to a
        // sibling that wasn't annotated stays subject to the
        // normal classifier path (Unknown for opaque idents).
        assert!(
            !(classify_with_declared_pure(
                "function pure(x) { return x; } function impure(x) { return x; }",
                "impure(x)",
                &["pure"]
            ))
            .is_pure()
        );
    }

    // --- ChunkCodeGraph: function-body purity inference --------------------

    /// Build a `ChunkCodeGraph` for `src` and return whether the
    /// named function is classified as Pure. `None` means the
    /// function isn't tracked in the chunk's purity graph (only
    /// `const`-bound function/arrow initializers are cached).
    /// Tests the full pipeline: chunk parsing → function
    /// collection → fixed-point.
    fn fn_purity(src: &str, name: &str) -> Option<bool> {
        let module = parse(src);
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
        graph.function_purity(name).map(|p| p.is_pure())
    }

    #[test]
    fn fn_purity_pure_hof_wrapper() {
        // Body returns a fresh object literal whose values are a
        // bound parameter — no observable side effect.
        assert_eq!(
            fn_purity(
                r#"function wrap(f) { return { kind: "wrapped", impl: f }; }"#,
                "wrap"
            ),
            Some(true)
        );
    }

    #[test]
    fn fn_purity_impure_globalthis_write() {
        // Assignment to a member of `globalThis` is unambiguously
        // impure regardless of what's on the RHS.
        assert_eq!(
            fn_purity("function tag(x) { globalThis.tag = x; }", "tag"),
            Some(false)
        );
    }

    #[test]
    fn fn_purity_unknown_when_calling_console_log() {
        // `console.log(...)` is a member-call on a non-whitelisted
        // receiver — Unknown. Caller inherits.
        assert_eq!(
            fn_purity(
                r#"function logged(x) { console.log("init", x); return x; }"#,
                "logged"
            ),
            Some(false)
        );
    }

    #[test]
    fn fn_purity_propagates_transitive_impurity() {
        // `caller` only calls `tainted`. `tainted` writes
        // `globalThis.touched`, so it's Impure. Fixed-point
        // propagates: `caller` becomes Impure on iteration 2.
        let src = r#"
            function tainted() { globalThis.touched = true; return 1; }
            function caller() { return tainted(); }
        "#;
        assert_eq!(fn_purity(src, "tainted"), Some(false));
        assert_eq!(fn_purity(src, "caller"), Some(false));
    }

    #[test]
    fn fn_purity_mutual_recursion_converges_pure() {
        // `even` and `odd` only reference each other inside their
        // bodies. Optimistic init (Pure) holds through the
        // fixed-point — neither body has an impure operation.
        let src = r#"
            function even(n) { return n === 0 ? true : odd(n - 1); }
            function odd(n) { return n === 0 ? false : even(n - 1); }
        "#;
        assert_eq!(fn_purity(src, "even"), Some(true));
        assert_eq!(fn_purity(src, "odd"), Some(true));
    }

    #[test]
    fn fn_purity_arrow_const_init() {
        // `const f = (x) => …` — chunk-top function in a VarDecl
        // initializer. Concise-arrow body classifies the single
        // return expression.
        assert_eq!(
            fn_purity("const wrap = (x) => ({ val: x });", "wrap"),
            Some(true)
        );
    }

    #[test]
    fn fn_purity_call_inherits_chunk_local_function_purity() {
        // `f()` where `f` is a chunk-top function in the cache
        // resolves through `ChunkCodeGraph::function_purity`. With
        // `f` body Pure, the call is Pure.
        let module = parse("function f() { return 42; } const x = f();");
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
        let var = match body[1].as_module_item() {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected VarDecl, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init");
        assert!((classify_expr_purity(init, &shadowed, &BTreeSet::new(), &graph)).is_pure());
    }

    #[test]
    fn fn_purity_let_var_bound_arrows_are_not_cached() {
        // `let` and `var` bindings are reassignable. Caching their
        // body's purity would be unsound: a later `f = …` could
        // replace the value with something impure between graph
        // construction and the call site. Restrict graph entries
        // to `const`-bound function/arrow initializers.
        assert_eq!(
            fn_purity("let f = () => 1;", "f"),
            None,
            "`let`-bound arrow must not be in the function-purity graph"
        );
        assert_eq!(
            fn_purity("var f = function () { return 1; };", "f"),
            None,
            "`var`-bound function expr must not be in the function-purity graph"
        );
        // Sanity: `const` still works.
        assert_eq!(fn_purity("const f = () => 1;", "f"), Some(true));
    }

    #[test]
    fn fn_purity_throw_makes_function_impure_even_with_pure_arg() {
        // `throw e` alters control flow observably regardless of
        // whether `e` itself is pure. A function that always
        // throws must not classify as Pure.
        assert_eq!(
            fn_purity(r#"function f() { throw "boom"; }"#, "f"),
            Some(false)
        );
        // Conditional throw is still Impure (we don't reason
        // about reachability — soundness-first).
        assert_eq!(
            fn_purity(r#"function f(x) { if (x) throw "boom"; return x; }"#, "f"),
            Some(false)
        );
    }

    #[test]
    fn fn_purity_debugger_makes_function_impure() {
        // `debugger` pauses execution observably to a host
        // attached to the process — not Pure.
        assert_eq!(
            fn_purity("function f() { debugger; return 1; }", "f"),
            Some(false)
        );
    }

    // --- Call-graph topology: deep chains, isolated nodes ------------------

    #[test]
    fn fn_purity_deep_pure_chain_propagates_in_one_pass() {
        // `a → b → c → d → e`: a long chain of chunk-local calls,
        // each function pure on its own. SCC bottom-up classifies
        // `e` first (no callees), then `d`, ..., then `a` — each
        // function classified once. With the previous global
        // fixed-point this would still terminate but rewalk every
        // body each pass; with SCC-bottom-up each is touched once.
        let src = r#"
            function e() { return 0; }
            function d() { return e(); }
            function c() { return d(); }
            function b() { return c(); }
            function a() { return b(); }
        "#;
        for name in ["a", "b", "c", "d", "e"] {
            assert_eq!(
                fn_purity(src, name),
                Some(true),
                "expected {name} to classify Pure"
            );
        }
    }

    #[test]
    fn fn_purity_deep_chain_propagates_impurity_to_root() {
        // Same shape but `e` writes `globalThis`. SCC processes
        // `e` first → Impure; the worklist propagates Impure up
        // the chain (`d` calls `e` → Impure; `c` calls `d` →
        // Impure; ...; `a` → Impure). Each function still only
        // re-classified after a callee changes — bounded total
        // work even on long chains.
        let src = r#"
            function e() { globalThis.touched = true; return 0; }
            function d() { return e(); }
            function c() { return d(); }
            function b() { return c(); }
            function a() { return b(); }
        "#;
        for name in ["a", "b", "c", "d", "e"] {
            assert_eq!(
                fn_purity(src, name),
                Some(false),
                "expected {name} to inherit Impure from `e`"
            );
        }
    }

    #[test]
    fn fn_purity_independent_functions_isolated_in_call_graph() {
        // No edges between `a` / `b` / `c`. Each is its own SCC;
        // classification of each is independent. `a` Impure must
        // not affect `b` or `c`.
        let src = r#"
            function a() { globalThis.touched = true; }
            function b() { return 1; }
            function c() { return 2; }
        "#;
        assert_eq!(fn_purity(src, "a"), Some(false));
        assert_eq!(fn_purity(src, "b"), Some(true));
        assert_eq!(fn_purity(src, "c"), Some(true));
    }

    #[test]
    fn fn_purity_mutual_recursion_with_external_impure_callee() {
        // Mutual recursion `a <-> b` (one SCC) + `a` also calls
        // `c` (separate SCC, Impure). `c` is processed first
        // (sink); `c` Impure. SCC {a, b}: optimistic Pure init,
        // worklist sees `a` calls `c` (Impure) → `a` becomes
        // Impure → `b` (which calls `a`) gets pushed to worklist
        // → `b` becomes Impure.
        let src = r#"
            function c() { globalThis.touched = true; return 0; }
            function a(n) { return n === 0 ? c() : b(n - 1); }
            function b(n) { return n === 0 ? 0 : a(n - 1); }
        "#;
        assert_eq!(fn_purity(src, "c"), Some(false));
        assert_eq!(fn_purity(src, "a"), Some(false));
        assert_eq!(fn_purity(src, "b"), Some(false));
    }

    // --- has_side_effect refinement ------------------------------------------

    fn has_side_effect_for(src: &str) -> Vec<bool> {
        let module = parse(src);
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| !f.purity.is_pure())
            .collect()
    }

    #[test]
    fn pure_const_decl_is_not_side_effecting() {
        assert_eq!(has_side_effect_for("const X = 42;"), vec![false]);
        assert_eq!(has_side_effect_for("const X = { a: 1 };"), vec![false]);
        assert_eq!(has_side_effect_for("const X = [1, 2, 3];"), vec![false]);
        assert_eq!(has_side_effect_for("const X = OTHER;"), vec![false]);
        assert_eq!(has_side_effect_for("const X = A + B;"), vec![false]);
    }

    #[test]
    fn impure_const_decl_is_side_effecting() {
        assert_eq!(has_side_effect_for("const X = compute();"), vec![true]);
        assert_eq!(has_side_effect_for("const X = new Foo();"), vec![true]);
        assert_eq!(has_side_effect_for("const X = (y = 1, y);"), vec![true]);
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
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| f.kind)
            .collect()
    }

    fn declared_per_statement(source: &str) -> Vec<Vec<String>> {
        let module = parse(source);
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| f.declared.into_iter().collect::<Vec<_>>())
            .collect()
    }

    #[test]
    fn split_two_declarator_const() {
        assert_eq!(
            statement_kinds("const A = 1, B = 2;"),
            vec![StatementKind::VarDecl, StatementKind::VarDecl]
        );
        assert_eq!(
            declared_per_statement("const A = 1, B = 2;"),
            vec![vec!["A".to_string()], vec!["B".to_string()]]
        );
    }

    #[test]
    fn split_three_declarator_let() {
        assert_eq!(
            declared_per_statement("let A = 1, B = 2, C = 3;"),
            vec![
                vec!["A".to_string()],
                vec!["B".to_string()],
                vec!["C".to_string()],
            ]
        );
    }

    #[test]
    fn split_export_const_with_comma_list() {
        // `export const A = 1, B = 2;` splits into two ExportDecls,
        // each declaring one name. Kind stays VarDecl (per
        // classify_item, ExportDecl-of-Var classifies as VarDecl).
        assert_eq!(
            statement_kinds("export const A = 1, B = 2;"),
            vec![StatementKind::VarDecl, StatementKind::VarDecl]
        );
        assert_eq!(
            declared_per_statement("export const A = 1, B = 2;"),
            vec![vec!["A".to_string()], vec!["B".to_string()]]
        );
    }

    #[test]
    fn single_declarator_var_decl_is_unchanged() {
        assert_eq!(statement_kinds("var A;"), vec![StatementKind::VarDecl]);
        assert_eq!(
            declared_per_statement("var A;"),
            vec![vec!["A".to_string()]]
        );
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

    // --- Comma-list owner attribution in owner graph quotient ---------------

    #[test]
    fn split_comma_list_attributes_reads_per_declarator() {
        // `const A = 1, B = X;` — A → mod_0, B → mod_1, X → mod_1.
        // Pre-split, `stmt_owner` would pick A's owner (mod_0)
        // for the whole comma-list and attribute `B`'s read of X
        // to mod_0, creating an R-edge mod_0 → mod_1 even though
        // the actual emitted module for B is mod_1. Post-split,
        // each declarator is its own statement: A's row owns
        // nothing readwise (literal init), B's row owns the read
        // of X but its home is mod_1 — so no edge (B reads X
        // within its own module).
        let schedule = schedule_for(
            "const A = 1, B = X; const X = 42;",
            &[("A", logical(0)), ("B", logical(1)), ("X", logical(1))],
        );
        // No cross-module read edges should exist: A's init is
        // pure, B reads X (same module).
        let mod_0 = ModuleId::Logical(LogicalModuleIndex(0));
        let mod_1 = ModuleId::Logical(LogicalModuleIndex(1));
        assert!(
            !schedule.dep_graph.graph.contains_edge(mod_0, mod_1),
            "no edge mod_0 → mod_1 expected, got: {:?}",
            schedule.dep_graph.graph.edge_weight(mod_0, mod_1),
        );
        assert!(
            !schedule.dep_graph.graph.contains_edge(mod_1, mod_0),
            "no edge mod_1 → mod_0 expected, got: {:?}",
            schedule.dep_graph.graph.edge_weight(mod_1, mod_0),
        );
    }

    #[test]
    fn split_comma_list_surfaces_real_cross_declarator_cycle() {
        // `const A = X, B = 1;` — A → mod_a, B → mod_b, X → mod_b.
        // mod_a's `A` reads X from mod_b → R-edge mod_a → mod_b.
        // Now also `const Y = A;` in mod_b reads A from mod_a:
        // → R-edge mod_b → mod_a. Cycle.
        //
        // Pre-split, the comma-list `const A = X, B = 1;` would
        // attribute the read of X to mod_a (A is declared first,
        // owner mod_a). So the edge is mod_a → mod_b. mod_b's
        // `Y = A` adds mod_b → mod_a. Cycle detected (correctly,
        // by accident). Post-split, A's row attributes the read
        // to mod_a, B's row to mod_b — same edges, same cycle.
        // This case demonstrates the split doesn't *miss* real
        // cycles either: the bug bit when multiple declarators
        // had differently-owned reads on the same line.
        let schedule = schedule_for(
            "const A = X, B = 1; const X = 42; const Y = A;",
            &[
                ("A", logical(0)),
                ("B", logical(1)),
                ("X", logical(1)),
                ("Y", logical(1)),
            ],
        );
        let report = schedule.validate();
        assert!(
            !report.cycles.is_empty(),
            "expected a real cycle to be reported"
        );
    }

    // --- linker_order in ScheduleReport --------------------------------------

    #[test]
    fn validate_surfaces_linker_order_for_acyclic_spec() {
        // mod_0 reads B from mod_1 at-init → mod_1 must precede
        // mod_0 in the linker's evaluation order.
        let schedule = schedule_for(
            "const A = B + 1; const B = 42;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let order = &report.linker_order;
        let pos = |name: &str| -> usize {
            order
                .iter()
                .position(|m| m == name)
                .unwrap_or_else(|| panic!("module {name} not in {order:?}"))
        };
        assert!(
            pos("mod_1") < pos("mod_0"),
            "mod_1 must precede mod_0 in linker_order; got {order:?}",
        );
    }

    #[test]
    fn validate_returns_empty_linker_order_for_cyclic_spec() {
        // mod_0 reads B (mod_1); mod_1 reads A (mod_0). Cycle.
        let schedule = schedule_for(
            "const A = B + 1; const B = A + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        assert!(!report.cycles.is_empty(), "expected a cycle in {report:?}",);
        assert!(
            report.linker_order.is_empty(),
            "linker_order must be empty when the dep graph is cyclic; got {:?}",
            report.linker_order,
        );
    }

    #[test]
    fn schedule_report_serializes_linker_order_as_snake_case() {
        let schedule = schedule_for(
            "const A = 1; const B = A + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let json = serde_json::to_string(&report).expect("serialize ScheduleReport");
        assert!(
            json.contains(r#""linker_order""#),
            "ScheduleReport must serialize linker_order as `linker_order`; got: {json}",
        );
    }
}
