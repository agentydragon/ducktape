mod chunk_constraining_module_edges_tests {
    //! Regression coverage for [`chunk_constraining_module_edges`]'s
    //! filter rule. The canonical edge set must match what the
    //! emitter actually emits as ESM `import` directives — namely
    //! all cross-module non-rebind non-LazyUse edges, including
    //! cross-module at-init promoted edges.
    use std::collections::BTreeSet;

    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    use crate::graph::*;
    use crate::ids::{LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::{AnalysisHints, OwnerGraph, OwnerId, StatementOrdinal, facts::analyze_chunk};

    fn module_id(index: usize) -> ModuleId {
        ModuleId(LogicalModuleIndex(index))
    }

    fn parse_facts(source: &str) -> Vec<crate::StatementFacts> {
        let cm: Lrc<SourceMap> = Default::default();
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
        let module = Parser::new_from(lexer)
            .parse_module()
            .expect("parse module");
        analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts
    }

    fn parse_and_build(source: &str) -> OwnerGraph {
        build_owner_graph_with(&parse_facts(source), Default::default()).unwrap()
    }

    /// Strict mapping: two top-level statements declaring the same
    /// binding (legal JS) must error instead of silently letting the
    /// last declaration win — last-insert-wins drops every edge into
    /// the earlier owner.
    #[test]
    fn duplicate_top_level_declarations_error() {
        let err =
            build_owner_graph_with(&parse_facts("var x = 1;\nvar x = 2;\n"), Default::default())
                .unwrap_err();
        assert_eq!(err.binding.as_ref(), "x");
        assert_eq!(err.first, StatementOrdinal(0));
        assert_eq!(err.second, StatementOrdinal(1));
        assert!(
            err.to_string().contains("duplicate top-level declaration"),
            "{err}"
        );
    }

    /// Same name in distinct scopes is hygienically distinct — no
    /// duplicate. Comma-split declarators of *different* names are
    /// also fine.
    #[test]
    fn distinct_bindings_with_shared_name_are_not_duplicates() {
        build_owner_graph_with(
            &parse_facts(
                "var x = 1;\nfunction f() { var x = 2; return x; }\nconst y = 3, z = 4;\n",
            ),
            Default::default(),
        )
        .unwrap();
    }

    /// Pure cross-module `LazyUse` edges contribute to
    /// `i_successors` (the runtime DFS topology — required for
    /// Lemma 2 asymmetric-cycle detection) but never to `edges`
    /// (the constraining/diagnostic surface): the emitter never emits
    /// an ESM `import` for a function-body read.
    #[test]
    fn lazy_only_cross_module_edge_in_i_successors_not_edges() {
        let source = "const a = 1; function f() { return a; }";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        // `f` reads `a` from a function body → LazyUse f → a. The
        // constraining `edges` surface stays empty because lazy
        // reads don't constrain init order.
        assert!(
            canonical.edges.is_empty(),
            "lazy edges must NOT enter constraining `edges`; got {:#?}",
            canonical.edges
        );
        // But the simulator's DFS topology (`i_successors`)
        // includes the lazy back-edge — Pass 2's asymmetric-cycle
        // rescue needs it.
        assert!(
            !canonical.i_successors.is_empty(),
            "lazy edges must contribute to `i_successors`; empty: {:#?}",
            canonical.i_successors
        );
    }

    /// Cross-module eager_use edge appears in the canonical set.
    #[test]
    fn eager_cross_module_edge_included() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        assert_eq!(
            pairs,
            BTreeSet::from([(module_id(1), module_id(0))]),
            "eager cross-module read `b = a + 1` must contribute mod_1 → mod_0"
        );
    }

    /// Same-module edges (intra-module reads) never appear in the
    /// canonical set — they don't correspond to any ESM import.
    #[test]
    fn same_module_edges_excluded() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        // Both owners in module 0 → no cross-module edges.
        let partition = Partition::new(&owner_graph, module_id(0));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        assert!(canonical.edges.is_empty());
    }

    /// Sequenced edges between the same module pair are deduped (one
    /// representative owner edge per pair) so that having N sequenced
    /// reasons between two modules doesn't over-weight the I-graph.
    #[test]
    fn sequenced_edges_dedup_per_pair() {
        // Each impure statement carries a Sequenced edge to the
        // previous impure statement (`emit_s_chain`).
        let source = "console.log(\"a\"); console.log(\"b\"); console.log(\"c\");";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        partition.set(OwnerId(2), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        // mod_1 contains owners 1 and 2; the only cross-module
        // sequenced edge is from mod_1 to mod_0 (owners 1, 2 both
        // sequenced after owner 0). We expect exactly ONE pair, even
        // though two owners contribute.
        let pair_count: usize = canonical
            .pairs()
            .filter(|&(from, to)| from == module_id(1) && to == module_id(0))
            .count();
        assert_eq!(
            pair_count, 1,
            "sequenced edges between the same pair must dedup to one"
        );
    }

    /// `chunk_linker_order_from_pairs` on a 3-module DAG returns
    /// dependency-first positions: deepest dependency at index 0,
    /// dependent at the last index.
    #[test]
    fn chunk_linker_order_assigns_positions_dependency_first() {
        let source = "const leaf = 1; const middle = leaf + 1; const top = middle + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // leaf
        partition.set(OwnerId(1), module_id(2)); // middle
        partition.set(OwnerId(2), module_id(3)); // top
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let linker = chunk_linker_order_from_pairs(canonical.pairs());
        let pos = position_lookup(&linker);
        // leaf (mod_1) must come before middle (mod_2) and top (mod_3).
        assert!(pos[&module_id(1)] < pos[&module_id(2)]);
        assert!(pos[&module_id(2)] < pos[&module_id(3)]);
    }

    /// `chunk_source_import_order_from_adjacency` returns every module of
    /// the canonical set plus the `extra_nodes`, which have no canonical
    /// edges.
    #[test]
    fn chunk_source_import_order_includes_extra_nodes() {
        // Simple two-module DAG.
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let extra: BTreeSet<ModuleId> = BTreeSet::from([module_id(5), module_id(0)]);
        let order = chunk_source_import_order_from_adjacency(
            canonical.pairs(),
            &canonical.i_successors,
            &extra,
        );
        assert!(
            order.contains(&module_id(5)),
            "extra node must be included; got {order:?}"
        );
        assert!(order.contains(&module_id(0)));
        assert!(order.contains(&module_id(1)));
    }

    /// Asymmetric I-cycle shape: eager forward + lazy back. The
    /// constraining `edges` contain ONLY the forward edge; the lazy
    /// back-edge lives only in `i_successors`.
    #[test]
    fn asymmetric_cycle_canonical_set_excludes_lazy_back_edge() {
        let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target + \"-derived\";";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // schemas_target -> mod_schemas
        partition.set(OwnerId(1), module_id(1)); // lazy_back     -> mod_schemas
        partition.set(OwnerId(2), module_id(2)); // ids_val       -> mod_ids
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        assert!(
            pairs.contains(&(module_id(2), module_id(1))),
            "forward eager edge ids → schemas must be present; got {pairs:?}"
        );
        assert!(
            !pairs.contains(&(module_id(1), module_id(2))),
            "lazy back-edge schemas → ids must NOT be present; got {pairs:?}"
        );
    }
}

mod from_report_tests {
    //! `OwnerGraph::from_report` reconstruction of the wire shape. The
    //! materializer emits [`EdgeRole`] through
    //! `OwnerGraphEdgeReport.role`; the peel planner reconstructs it
    //! here. Both ends must agree so the planner's gate runs the same
    //! cross-module-at-init filter the materializer's gate does.
    use crate::purity::Purity;
    use crate::reports::schema::{
        AtomicGraphReport, EdgeRoleReport, OwnerGraphEdgeReport, OwnerGraphNodeReport,
        OwnerGraphQuotientReport, OwnerGraphReport,
    };
    use crate::{
        DepKind, EdgeRole, OwnerEdgeId, OwnerGraph, OwnerId, StatementKind, StatementOrdinal,
    };

    fn node(id: &str, ordinal: usize) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal),
            source_location: None,
            declared_bindings: Vec::new(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: crate::ModuleKey("residual".to_string()),
        }
    }

    /// Direct edges serialize with `role = None`; on the way back in
    /// they reconstruct as `EdgeRole::Direct`.
    #[test]
    fn direct_role_round_trips_via_none() {
        let report = OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:0".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role: None,
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        let graph = OwnerGraph::from_report(&report).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(graph.edge(OwnerEdgeId(0)).reason.role(), EdgeRole::Direct);
    }

    /// Promoted edges carry an `EdgeRoleReport::PromotedAtInit` on
    /// the wire and reconstruct as `EdgeRole::PromotedAtInit` with
    /// the resolved `OwnerId`.
    #[test]
    fn promoted_at_init_role_round_trips_with_callee_owner() {
        let report = OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1), node("owner:2", 2)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:0".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role: Some(EdgeRoleReport::PromotedAtInit {
                    callee_owner: "owner:2".to_string(),
                }),
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        let graph = OwnerGraph::from_report(&report).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(
            graph.edge(OwnerEdgeId(0)).reason.role(),
            EdgeRole::PromotedAtInit {
                callee_owner: OwnerId(2),
            }
        );
    }

    /// Strict mapping: an edge referencing an owner id missing from
    /// the node table (malformed / version-skewed `owner_graph.json`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let report = OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:999".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role: None,
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        let err = OwnerGraph::from_report(&report).unwrap_err();
        assert_eq!(err.endpoint, "owner:999");
        assert_eq!(err.edge_id, "owner_edge:0");
        assert!(err.to_string().contains("owner:999"), "{err}");
    }
}
