mod chunk_constraining_module_edges_tests {
    //! Regression coverage for [`chunk_constraining_module_edges`]'s
    //! filter rule. The canonical edge set must match what the
    //! emitter actually emits as ESM `import` directives — namely
    //! all cross-module non-rebind non-LazyUse edges, including
    //! cross-module at-init promoted edges.
    use std::collections::BTreeSet;

    use crate::module_assignment::ModuleAssignment;
    use crate::*;
    use analysis::ids::ModuleId;
    use analysis_model::DepKind;
    use source_identity::SourceUnitId;

    use analysis::AnalysisInputs;
    use analyze::analyze_chunk;

    use crate::DependencyGraph;

    fn parse_facts(source: &str) -> Vec<analysis::StatementFacts> {
        let module = raw_js_test_support::parse(source);
        analyze_chunk(&module, &AnalysisInputs::default(), None).facts
    }

    fn parse_and_build(source: &str) -> DependencyGraph {
        build_dependency_graph_with(&parse_facts(source), Default::default(), &[])
            .unwrap()
            .0
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
            analysis::EntryDestination::from_module_id(ModuleId(0)),
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
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        assert!(canonical.edges.is_empty());
    }

    #[test]
    fn chunk_source_import_order_keeps_lazy_only_members_and_isolated_nodes() {
        let source = "const a = () => c(); const b = a; function c() { return b; }";
        let dependency_graph = parse_and_build(source);
        let mut assignment = ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        assignment.set(SourceUnitId(1), ModuleId(1));
        assignment.set(SourceUnitId(2), ModuleId(2));
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        let linker_position = position_lookup(&chunk_linker_order_from_pairs(canonical.pairs()));
        let order = chunk_source_import_order_from_adjacency(
            &linker_position,
            &canonical.i_successors,
            &BTreeSet::from([ModuleId(5)]),
        );
        // The a/b/c import cycle has one eager read, b -> a. Visit b
        // before a to rescue that read, then the lazy-only c and isolated 5.
        assert_eq!(order, [ModuleId(1), ModuleId(0), ModuleId(2), ModuleId(5)]);
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

    use crate::report::{
        AtomicGraphReport, DependencyEdgeReport, DependencyGraphReport, EdgeRoleReport,
        ModuleQuotientReport, SourceUnitReport,
    };
    use analysis::analysis_inputs::ValueInputRef;
    use analysis::purity::Purity;
    use analysis::reports::schema::{NativeIntegrityGuard, NativeIntegrityUse};
    use source_identity::SourceUnitId;

    use analysis::StatementKind;

    use crate::{DependencyEdgeId, DependencyGraphInput, EdgeRole};
    use analysis_model::DepKind;

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
            import_order: Vec::new(),
            ownership_subset: None,
            program_graph: "../program_graph.json.gz".into(),
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
            let input =
                DependencyGraphInput::from_report(report_with_edge("unit:0", wire_role)).unwrap();
            let graph = input.graph();
            assert_eq!(graph.num_edges(), 1, "{case}");
            assert_eq!(
                graph.edge(DependencyEdgeId(0)).reason.role(),
                expected,
                "{case}"
            );
        }
    }

    #[test]
    fn loaded_graph_shares_topology_and_retains_report_metadata() {
        let mut report = report_with_edge(
            "provider",
            Some(EdgeRoleReport::PromotedAtInit {
                callee_source_unit: "callee".to_string(),
            }),
        );
        report.nodes[0].id = "provider".to_string();
        report.nodes[2].id = "callee".to_string();
        report.nodes.swap(0, 2);
        report.nodes[1]
            .declared_bindings
            .push(crate::BindingReport {
                binding: "localName".into(),
                export_name: "readableName".into(),
            });
        let input = DependencyGraphInput::from_report(report).unwrap();
        let first = input.shared_graph();
        let second = input.shared_graph();
        assert!(std::sync::Arc::ptr_eq(&first, &second));
        assert!(std::ptr::eq(input.graph(), &*first));
        assert_eq!(input.source_unit_id("unit:1"), Some(SourceUnitId(1)));
        assert_eq!(input.source_unit_id("provider"), Some(SourceUnitId(2)));
        assert_eq!(input.source_unit_id("callee"), Some(SourceUnitId(0)));
        assert_eq!(input.source_unit_id("missing"), None);
        assert_eq!(first.edge(DependencyEdgeId(0)).from, SourceUnitId(1));
        assert_eq!(first.edge(DependencyEdgeId(0)).to, SourceUnitId(2));
        assert_eq!(
            first.edge(DependencyEdgeId(0)).reason.role(),
            EdgeRole::PromotedAtInit {
                callee_source_unit: SourceUnitId(0)
            },
        );
        assert_eq!(
            input.report().nodes[1].declared_bindings[0].export_name,
            "readableName"
        );
    }

    /// Strict mapping: an edge referencing an source unit id missing from
    /// the node table (malformed / version-skewed `dependency_graph.json.gz`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let err =
            DependencyGraphInput::from_report(report_with_edge("unit:999", None)).unwrap_err();
        assert_eq!(
            err,
            crate::GraphReportIdError::MissingEndpoint {
                endpoint: "unit:999".into(),
                edge_id: "dependency_edge:0".into(),
            }
        );
        assert!(err.to_string().contains("unit:999"), "{err}");
    }

    #[test]
    fn repeated_node_id_cannot_redirect_an_edge() {
        let mut report = report_with_edge("unit:0", None);
        report.nodes[0].id = "unit:0".into();
        report.nodes[1].id = "unit:0".into();
        let err = DependencyGraphInput::from_report(report).unwrap_err();
        assert_eq!(
            err,
            crate::GraphReportIdError::DuplicateNode {
                id: "unit:0".into()
            }
        );
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
            program_premises: BTreeSet::new(),
        };
        let mut report = report_with_edge("unit:0", None);
        report.nodes[1].heap_native_integrity_uses = vec![expected.clone()];

        let wire = serde_json::to_value(&report).unwrap();
        assert!(wire["nodes"][0].get("heap_native_integrity_uses").is_none());
        let parsed: DependencyGraphReport = serde_json::from_value(wire.clone()).unwrap();
        let input = DependencyGraphInput::from_report(parsed).unwrap();
        assert_eq!(
            input
                .graph()
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
        let old = DependencyGraphInput::from_report(old_report).unwrap();
        assert!(
            old.graph()
                .iter_nodes()
                .all(|node| node.heap_native_integrity_uses.is_empty())
        );
    }
}
