mod chunk_constraining_module_edges_tests {
    //! Regression coverage for [`chunk_constraining_module_edges`]'s
    //! filter rule. The canonical edge set must match what the
    //! emitter actually emits as ESM `import` directives — namely
    //! all cross-module non-rebind non-LazyUse edges, including
    //! cross-module at-init promoted edges.
    use std::collections::BTreeSet;

    use crate::graph::*;
    use crate::ids::ModuleId;
    use crate::module_assignment::ModuleAssignment;
    use crate::{AnalysisInputs, DependencyGraph, SourceUnitId, facts::analyze_chunk};

    fn parse_facts(source: &str) -> Vec<crate::StatementFacts> {
        let module = raw_js_test_support::parse(source);
        analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None).facts
    }

    fn parse_and_build(source: &str) -> DependencyGraph {
        build_dependency_graph_with(&parse_facts(source), Default::default(), &[]).unwrap()
    }

    #[test]
    fn raw_builder_rejects_unresolved_argument_contracts() {
        let options: DependencyGraphOptions = serde_json::from_str(
            r#"{"argument_inputs":[{"source_match":{"match":"function keys(input) {}","bindings":["keys"]},"parameter_index":0,"exact_arity":1,"audited_source_sha256":"abc"}]}"#,
        )
        .unwrap();
        let error =
            build_dependency_graph_with(&parse_facts("const x = 1;"), options, &[]).unwrap_err();
        assert_eq!(
            error,
            DependencyGraphBuildError::UnresolvedArgumentInputs { count: 1 }
        );
    }

    #[test]
    fn repeated_vars_retain_and_join_both_declaration_source_units() {
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
            BTreeSet::from([
                (SourceUnitId(0), SourceUnitId(1)),
                (SourceUnitId(1), SourceUnitId(0)),
            ])
        );
    }

    /// Cross-module eager_use edge appears in the canonical set.
    #[test]
    fn eager_cross_module_edge_included() {
        let source = "const a = 1; const b = a + 1;";
        let dependency_graph = parse_and_build(source);
        let mut assignment = ModuleAssignment::new(
            &dependency_graph,
            crate::EntryDestination::from_module_id(ModuleId(0)),
        );
        assignment.set(SourceUnitId(1), ModuleId(1));
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        assert_eq!(
            pairs,
            BTreeSet::from([(ModuleId(1), ModuleId(0))]),
            "eager cross-module read `b = a + 1` must contribute mod_1 → mod_0"
        );
    }

    /// Same-module edges (intra-module reads) never appear in the
    /// canonical set — they don't correspond to any ESM import.
    #[test]
    fn same_module_edges_excluded() {
        let source = "const a = 1; const b = a + 1;";
        let dependency_graph = parse_and_build(source);
        // Both source units in module 0 → no cross-module edges.
        let assignment = ModuleAssignment::new(
            &dependency_graph,
            crate::EntryDestination::from_module_id(ModuleId(0)),
        );
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        assert!(canonical.edges.is_empty());
    }

    /// `chunk_source_import_order_from_adjacency` returns every module of
    /// the canonical set plus the `extra_nodes`, which have no canonical
    /// edges.
    #[test]
    fn chunk_source_import_order_includes_extra_nodes() {
        // Simple two-module DAG.
        let source = "const a = 1; const b = a + 1;";
        let dependency_graph = parse_and_build(source);
        let mut assignment = ModuleAssignment::new(
            &dependency_graph,
            crate::EntryDestination::from_module_id(ModuleId(0)),
        );
        assignment.set(SourceUnitId(1), ModuleId(1));
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        let extra: BTreeSet<ModuleId> = BTreeSet::from([ModuleId(5), ModuleId(0)]);
        let order = chunk_source_import_order_from_adjacency(
            canonical.pairs(),
            &canonical.i_successors,
            &extra,
        );
        assert!(
            order.contains(&ModuleId(5)),
            "extra node must be included; got {order:?}"
        );
        assert!(order.contains(&ModuleId(0)));
        assert!(order.contains(&ModuleId(1)));
    }
}

mod from_report_tests {
    //! `DependencyGraph::from_report` reconstruction of the wire shape. The
    //! materializer emits [`EdgeRole`] through
    //! `DependencyEdgeReport.role`; the peel planner reconstructs it
    //! here. Both ends must agree so the planner's gate runs the same
    //! cross-module-at-init filter the materializer's gate does.
    use std::collections::BTreeSet;

    use source_identity::SourceUnitOrdinal;

    use crate::analysis_inputs::ValueInputRef;
    use crate::purity::Purity;
    use crate::reports::schema::{
        AtomicGraphReport, DependencyEdgeReport, DependencyGraphReport, EdgeRoleReport,
        ModuleQuotientReport, NativeIntegrityGuard, NativeIntegrityUse, SourceUnitReport,
    };
    use crate::{
        DepKind, DependencyEdgeId, DependencyGraph, EdgeRole, SourceUnitId, StatementKind,
    };

    fn node(id: &str, ordinal: usize) -> SourceUnitReport {
        SourceUnitReport {
            id: id.to_string(),
            statement_ordinal: SourceUnitOrdinal(ordinal),
            source_location: None,
            declared_bindings: Vec::new(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::pure(),
            callback_factory_barriers: Vec::new(),
            heap_native_integrity_uses: Vec::new(),
            synchronous_execution: None,
            destination: crate::ModuleKey("residual".to_string()),
        }
    }

    /// An eager edge from `unit:1` to `target`, over source units 0..=2, carrying
    /// `role` on the wire.
    fn report_with_edge(target: &str, role: Option<EdgeRoleReport>) -> DependencyGraphReport {
        DependencyGraphReport {
            chunk_id: "chunk".into(),
            program_graph: "../program_graph.json".into(),
            nodes: vec![node("unit:0", 0), node("unit:1", 1), node("unit:2", 2)],
            purity_causes: Vec::new(),
            imported_callable_summaries: Vec::new(),
            edges: vec![DependencyEdgeReport {
                id: "dependency_edge:0".to_string(),
                source: "unit:1".to_string(),
                target: target.to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: SourceUnitOrdinal(1),
                constrains_init_order: true,
                role,
                sequenced_cause: None,
                commuting_premise: None,
                premise_verdict: None,
            }],
            commuting_statements: Vec::new(),
            quotient: ModuleQuotientReport {
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
    /// `EdgeRole::PromotedAtInit` with the resolved callee `SourceUnitId`.
    #[test]
    fn edge_role_round_trips_through_the_wire_shape() {
        for (case, wire_role, expected) in [
            ("direct via none", None, EdgeRole::Direct),
            (
                "promoted at init",
                Some(EdgeRoleReport::PromotedAtInit {
                    callee_source_unit: "unit:2".to_string(),
                }),
                EdgeRole::PromotedAtInit {
                    callee_source_unit: SourceUnitId(2),
                },
            ),
        ] {
            let graph =
                DependencyGraph::from_report(&report_with_edge("unit:0", wire_role)).unwrap();
            assert_eq!(graph.num_edges(), 1, "{case}");
            assert_eq!(
                graph.edge(DependencyEdgeId(0)).reason.role(),
                expected,
                "{case}"
            );
        }
    }

    /// Strict mapping: an edge referencing an source unit id missing from
    /// the node table (malformed / version-skewed `dependency_graph.json`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let err = DependencyGraph::from_report(&report_with_edge("unit:999", None)).unwrap_err();
        assert_eq!(err.endpoint, "unit:999");
        assert_eq!(err.edge_id, "dependency_edge:0");
        assert!(err.to_string().contains("unit:999"), "{err}");
    }

    #[test]
    fn heap_native_integrity_uses_round_trip_and_old_wire_defaults_empty() {
        let expected = NativeIntegrityUse {
            guard: NativeIntegrityGuard::LocalClassConstruction,
            value_input_refs: BTreeSet::from([ValueInputRef {
                defining_chunk: "static/provider".to_string(),
                input_index: 2,
            }]),
            no_accessor_install_refs: BTreeSet::new(),
            application_object_write_refs: BTreeSet::new(),
        };
        let mut report = report_with_edge("unit:0", None);
        report.nodes[1].heap_native_integrity_uses = vec![expected.clone()];

        let wire = serde_json::to_value(&report).unwrap();
        assert!(wire["nodes"][0].get("heap_native_integrity_uses").is_none());
        let parsed: DependencyGraphReport = serde_json::from_value(wire.clone()).unwrap();
        let graph = DependencyGraph::from_report(&parsed).unwrap();
        assert_eq!(
            graph
                .iter_nodes()
                .nth(1)
                .unwrap()
                .heap_native_integrity_uses,
            vec![expected]
        );

        let mut old_wire = wire;
        for node in old_wire["nodes"].as_array_mut().unwrap() {
            node.as_object_mut()
                .unwrap()
                .remove("heap_native_integrity_uses");
        }
        let old_report: DependencyGraphReport = serde_json::from_value(old_wire).unwrap();
        let old_graph = DependencyGraph::from_report(&old_report).unwrap();
        assert!(
            old_graph
                .iter_nodes()
                .all(|node| node.heap_native_integrity_uses.is_empty())
        );
    }
}
