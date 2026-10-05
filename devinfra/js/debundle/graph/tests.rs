mod chunk_constraining_module_edges_tests {
    //! Regression coverage for [`chunk_constraining_module_edges`]'s
    //! filter rule. The canonical edge set must match what the
    //! emitter actually emits as ESM `import` directives — namely
    //! all cross-module non-rebind non-LazyUse edges, including
    //! cross-module at-init promoted edges.
    use std::collections::BTreeSet;

    use crate::graph::*;
    use crate::ids::{LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::{AnalysisHints, OwnerGraph, OwnerId, facts::analyze_chunk};

    fn module_id(index: usize) -> ModuleId {
        ModuleId(LogicalModuleIndex(index))
    }

    fn parse_facts(source: &str) -> Vec<crate::StatementFacts> {
        let module = raw_js_test_support::parse(source);
        analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts
    }

    fn parse_and_build(source: &str) -> OwnerGraph {
        build_owner_graph_with(&parse_facts(source), Default::default()).unwrap()
    }

    #[test]
    fn repeated_vars_retain_and_join_both_declaration_owners() {
        let graph = parse_and_build("var x = 1;\nvar x = 2;\n");
        assert_eq!(graph.num_nodes(), 2);
        for node in graph.iter_nodes() {
            assert!(node.declared.iter().any(|id| id.0 == "x"));
        }
        let co_declarations: BTreeSet<_> = graph
            .iter_edges()
            .filter(|edge| edge.reason.kind == DepKind::CoDeclaration)
            .map(|edge| (edge.from, edge.to))
            .collect();
        assert_eq!(
            co_declarations,
            BTreeSet::from([(OwnerId(0), OwnerId(1)), (OwnerId(1), OwnerId(0)),])
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

    /// An eager edge from `owner:1` to `target`, over owners 0..=2, carrying
    /// `role` on the wire.
    fn report_with_edge(target: &str, role: Option<EdgeRoleReport>) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1), node("owner:2", 2)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: target.to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role,
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
        }
    }

    /// Direct edges serialize with `role = None` and reconstruct as
    /// `EdgeRole::Direct`; promoted edges carry an
    /// `EdgeRoleReport::PromotedAtInit` and reconstruct as
    /// `EdgeRole::PromotedAtInit` with the resolved callee `OwnerId`.
    #[test]
    fn edge_role_round_trips_through_the_wire_shape() {
        for (case, wire_role, expected) in [
            ("direct via none", None, EdgeRole::Direct),
            (
                "promoted at init",
                Some(EdgeRoleReport::PromotedAtInit {
                    callee_owner: "owner:2".to_string(),
                }),
                EdgeRole::PromotedAtInit {
                    callee_owner: OwnerId(2),
                },
            ),
        ] {
            let graph = OwnerGraph::from_report(&report_with_edge("owner:0", wire_role)).unwrap();
            assert_eq!(graph.num_edges(), 1, "{case}");
            assert_eq!(graph.edge(OwnerEdgeId(0)).reason.role(), expected, "{case}");
        }
    }

    /// Strict mapping: an edge referencing an owner id missing from
    /// the node table (malformed / version-skewed `owner_graph.json`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let err = OwnerGraph::from_report(&report_with_edge("owner:999", None)).unwrap_err();
        assert_eq!(err.endpoint, "owner:999");
        assert_eq!(err.edge_id, "owner_edge:0");
        assert!(err.to_string().contains("owner:999"), "{err}");
    }
}
