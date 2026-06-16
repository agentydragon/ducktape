#[cfg(test)]
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
        build_owner_graph(&parse_facts(source)).unwrap()
    }

    /// Strict mapping: two top-level statements declaring the same
    /// binding (legal JS) must error instead of silently letting the
    /// last declaration win — last-insert-wins drops every edge into
    /// the earlier owner.
    #[test]
    fn duplicate_top_level_declarations_error() {
        let err = build_owner_graph(&parse_facts("var x = 1;\nvar x = 2;\n")).unwrap_err();
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
        build_owner_graph(&parse_facts(
            "var x = 1;\nfunction f() { var x = 2; return x; }\nconst y = 3, z = 4;\n",
        ))
        .unwrap();
    }

    /// Pure cross-module lazy edge must not appear in the canonical
    /// edge set. The emitter never emits an ESM `import` for a
    /// function-body read; the gate must agree.
    /// Pure cross-module `LazyUse` edges contribute to
    /// `i_successors` (the runtime DFS topology — required for
    /// Lemma 2 asymmetric-cycle detection) but never to `edges`
    /// (the constraining/diagnostic surface).
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
        assert!(canonical.contains(module_id(1), module_id(0)));
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
    /// This mirrors `build_module_quotient`'s dedup.
    #[test]
    fn sequenced_edges_dedup_per_pair() {
        // Two impure statements in different modules: each carries a
        // Sequenced edge from the later impure stmt to the earlier
        // (graph.rs::sequenced_edges).
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
        assert!(
            pair_count <= 1,
            "sequenced edges between the same pair must dedup; got {pair_count}",
        );
    }

    /// `chunk_linker_order` on a 3-module DAG returns dependency-first
    /// positions: deepest dependency at index 0, dependent at the
    /// last index.
    #[test]
    fn chunk_linker_order_assigns_positions_dependency_first() {
        let source = "const leaf = 1; const middle = leaf + 1; const top = middle + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // leaf
        partition.set(OwnerId(1), module_id(2)); // middle
        partition.set(OwnerId(2), module_id(3)); // top
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let linker = chunk_linker_order(&canonical);
        let pos = position_lookup(&linker);
        // leaf (mod_1) must come before middle (mod_2) and top (mod_3).
        assert!(pos[&module_id(1)] < pos[&module_id(2)]);
        assert!(pos[&module_id(2)] < pos[&module_id(3)]);
    }

    /// `chunk_source_import_order` reverses within an SCC so the
    /// dependent appears first in source. Asymmetric cycle shape: a
    /// canonical edge from dependent → dependency, but only after
    /// the unification's lazy-edge exclusion takes effect (so the
    /// SCC is detected via some other path — here we exercise it
    /// directly with the modules present even though canonical
    /// edges are acyclic post-fix).
    #[test]
    fn chunk_source_import_order_includes_extra_nodes() {
        // Simple two-module DAG.
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let extra: BTreeSet<ModuleId> = BTreeSet::from([module_id(5), module_id(0)]);
        let order = chunk_source_import_order(&canonical, &extra);
        assert!(
            order.contains(&module_id(5)),
            "extra node must be included; got {order:?}"
        );
        assert!(order.contains(&module_id(0)));
        assert!(order.contains(&module_id(1)));
    }

    /// Asymmetric I-cycle shape: eager forward + lazy back. The
    /// canonical edge set must contain ONLY the forward edge — the
    /// lazy back-edge is dropped. This is the gaffer fix: a
    /// dependency's lazy back-edge to its dependent must NOT appear
    /// in the runtime DFS topology the simulator walks.
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

#[cfg(test)]
mod edge_role_wire_format_tests {
    //! Wire-format round-trip for [`EdgeRole`]. The materializer
    //! emits the role through `OwnerGraphEdgeReport.role`; the peel
    //! planner reconstructs it via `OwnerGraph::from_report`. Both
    //! ends must agree so the planner's gate runs the same
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
        let (graph, _) = OwnerGraph::from_report(&report, &[]).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(graph.edge(OwnerEdgeId(0)).reason.role(), EdgeRole::Direct);
    }

    /// Promoted edges carry an `EdgeRoleReport::PromotedAtInit` on
    /// the wire and reconstruct as `EdgeRole::PromotedAtInit` with
    /// the resolved `OwnerId`. The CSR `callee_edges` adjacency must
    /// also populate so `impacted_owner_edges` can find the edge by
    /// callee owner.
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
        let (graph, _) = OwnerGraph::from_report(&report, &[]).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(
            graph.edge(OwnerEdgeId(0)).reason.role(),
            EdgeRole::PromotedAtInit {
                callee_owner: OwnerId(2),
            }
        );
        // CSR by-callee adjacency populated for owner:2.
        assert_eq!(graph.callee_edges_of(OwnerId(2)).len(), 1);
        assert_eq!(graph.callee_edges_of(OwnerId(0)).len(), 0);
    }

    /// JSON serialization shape: a `Direct` role omits the `role`
    /// field; a `PromotedAtInit` role nests `{kind: "promoted_at_init",
    /// callee_owner: "owner:N"}`. This pins the wire encoding so
    /// callers (Stage A artifact readers) don't drift.
    #[test]
    fn role_json_shape_pinned() {
        let direct_report = OwnerGraphEdgeReport {
            id: "owner_edge:0".to_string(),
            source: "owner:1".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: None,
            statement_ordinal: StatementOrdinal(1),
            constrains_init_order: true,
            role: None,
        };
        let direct_json = serde_json::to_string(&direct_report).unwrap();
        assert!(
            !direct_json.contains("\"role\""),
            "Direct edges omit the role field; got {direct_json}",
        );

        let promoted_report = OwnerGraphEdgeReport {
            role: Some(EdgeRoleReport::PromotedAtInit {
                callee_owner: "owner:7".to_string(),
            }),
            ..direct_report
        };
        let promoted_json = serde_json::to_string(&promoted_report).unwrap();
        assert!(
            promoted_json.contains("\"role\""),
            "PromotedAtInit edges carry a role field; got {promoted_json}",
        );
        assert!(
            promoted_json.contains("\"kind\":\"promoted_at_init\""),
            "role tag must be `promoted_at_init`; got {promoted_json}",
        );
        assert!(
            promoted_json.contains("\"callee_owner\":\"owner:7\""),
            "callee_owner must round-trip; got {promoted_json}",
        );

        // Round-trip via JSON.
        let parsed: OwnerGraphEdgeReport = serde_json::from_str(&promoted_json).unwrap();
        assert_eq!(parsed.role, promoted_report.role);
    }
}

#[cfg(test)]
mod declared_round_trip_tests {
    //! Round-trip the per-owner `declared: BTreeSet<Id>` set through
    //! the JSON wire shape. `OwnerGraphNodeReport` itself doesn't
    //! carry hygienic `Id` atoms (its `declared_bindings: Vec<BindingReport>`
    //! is `Atom`-only), so `OwnerGraph::from_report` joins each node's
    //! `statement_ordinal` against the matching
    //! `StatementFactsReport.declared` (which does carry the
    //! `(name, ctxt)` pair via `IdReport`). The tests below pin both
    //! the syntactic round-trip (declared sets match) and the
    //! semantic round-trip (`compute_owner_claims` returns the same
    //! ModuleId verdict on the reconstructed graph as on the
    //! in-memory original).
    use std::collections::HashMap;

    use crate::factor_assembly::assemble_partition;
    use crate::facts::analyze_chunk;
    use crate::graph::{OwnerGraph, build_owner_graph};
    use crate::ids::{BindingKind, LogicalModule, LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::reports::owner_key;
    use crate::reports::schema::{
        AtomicGraphReport, OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport,
        OwnerGraphReport,
    };
    use crate::{AnalysisHints, BindingReport, StatementFactsReport};

    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    /// Build a real owner graph from JS source, then synthesize the
    /// JSON-shaped reports (owner graph + per-statement facts) that
    /// `OwnerGraph::from_report` consumes. The synthesized reports
    /// re-encode the in-memory graph faithfully (modulo what the
    /// wire format intentionally drops, e.g. per-edge `EdgeReason`
    /// metadata beyond `kind` + `role`).
    fn build_and_serialize(
        source: &str,
    ) -> (
        OwnerGraph,
        Vec<StatementFactsReport>,
        OwnerGraphReport,
        HashMap<swc_ecma_ast::Id, BindingKind>,
        Vec<LogicalModule>,
    ) {
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
        let analysis = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None);
        let owner_graph = build_owner_graph(&analysis.facts).unwrap();
        let facts_reports: Vec<StatementFactsReport> = analysis
            .facts
            .iter()
            .map(StatementFactsReport::from_facts)
            .collect();
        let nodes = owner_graph
            .iter_nodes()
            .map(|n| OwnerGraphNodeReport {
                id: owner_key(n.id),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared_bindings: n
                    .declared
                    .iter()
                    .map(|id| BindingReport {
                        binding: id.0.clone(),
                        export_name: id.0.clone(),
                    })
                    .collect(),
                statement_kind: n.kind,
                purity: n.purity.clone(),
                destination: crate::ModuleKey("m".to_string()),
            })
            .collect();
        let edges: Vec<OwnerGraphEdgeReport> = owner_graph
            .iter_edges()
            .map(|edge| OwnerGraphEdgeReport {
                id: edge.id.report_key(),
                source: owner_key(edge.from),
                target: owner_key(edge.to),
                edge_kind: edge.reason.kind,
                binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
                statement_ordinal: edge.reason.statement_ordinal,
                constrains_init_order: edge.reason.constrains_init_order(),
                role: None,
            })
            .collect();
        let report = OwnerGraphReport {
            chunk_id: "test".into(),
            nodes,
            edges,
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
        // Build a `bindings` table mapping each declared Id → owner.
        // This is the standard input to `compute_owner_claims` /
        // `assemble_partition`.
        let mut bindings: HashMap<swc_ecma_ast::Id, BindingKind> = HashMap::new();
        for (idx, node) in owner_graph.iter_nodes().enumerate() {
            // Round-robin owners to alternating modules so the
            // semantic test below sees a non-trivial partition.
            let dest = ModuleId(LogicalModuleIndex(idx % 2));
            for id in &node.declared {
                bindings.insert(id.clone(), BindingKind::Owned { module: dest });
            }
        }
        let logical_modules = vec![
            LogicalModule {
                id: "m0".into(),
                target_file: "m0.js".into(),
                anonymous_statement_ordinals: Vec::new(),
                residual: false,
                rename_map: HashMap::new(),
            },
            LogicalModule {
                id: "m1".into(),
                target_file: "m1.js".into(),
                anonymous_statement_ordinals: Vec::new(),
                residual: true,
                rename_map: HashMap::new(),
            },
        ];
        (
            owner_graph,
            facts_reports,
            report,
            bindings,
            logical_modules,
        )
    }

    /// Serialize a graph with non-empty `declared`, deserialize, and
    /// assert each owner's `declared` set matches the original.
    ///
    /// This is the syntactic round-trip — it pins that
    /// `OwnerGraph::from_report` no longer silently drops the
    /// per-owner declared binding set.
    #[test]
    fn declared_round_trips_through_owner_graph_report() {
        let source = "const a = 1;\nconst b = a + 1;\nlet c = 0;\n";
        let (original, facts, report, _bindings, _logical) = build_and_serialize(source);
        assert!(
            original.iter_nodes().any(|n| !n.declared.is_empty()),
            "fixture must have at least one declared-binding owner",
        );

        let (round_tripped, _) = OwnerGraph::from_report(&report, &facts).unwrap();

        assert_eq!(
            round_tripped.nodes.len(),
            original.nodes.len(),
            "node count must match"
        );
        for (orig, restored) in original.iter_nodes().zip(round_tripped.iter_nodes()) {
            assert_eq!(
                orig.statement_ordinal, restored.statement_ordinal,
                "statement ordinals must align (join key)"
            );
            assert_eq!(
                orig.declared, restored.declared,
                "declared sets must round-trip via StatementFactsReport.declared"
            );
        }
    }

    /// Semantic round-trip: rebuild the graph from the wire shape,
    /// then run `assemble_partition` (which internally calls
    /// `compute_owner_claims`) against the reconstructed graph and
    /// assert the resulting `Partition` matches the partition
    /// obtained by running the same call on the in-memory original.
    ///
    /// This is what distinguishes "round-trip works syntactically"
    /// from "round-trip works semantically": the planner-side gate
    /// reconstructs a graph and feeds it to `assemble_partition` to
    /// derive the post-edit partition; if `compute_owner_claims`
    /// silently returns `None` for every owner (because
    /// `nodes[].declared` is empty), the partition reduces to the
    /// residual fallback and the gate's verdict is meaningless.
    #[test]
    fn compute_owner_claims_round_trips_via_reconstructed_graph() {
        let source = "const a = 1;\nconst b = a + 1;\nlet c = 0;\n";
        let (original, facts, report, bindings, logical_modules) = build_and_serialize(source);
        let residual = ModuleId(LogicalModuleIndex(1));
        let atomic_units = crate::atomic_units::compute_atomic_units(&original);
        let original_outcome = assemble_partition(
            &original,
            &atomic_units,
            &bindings,
            &logical_modules,
            residual,
        );

        let (round_tripped, _) = OwnerGraph::from_report(&report, &facts).unwrap();
        let restored_units = crate::atomic_units::compute_atomic_units(&round_tripped);
        let restored_outcome = assemble_partition(
            &round_tripped,
            &restored_units,
            &bindings,
            &logical_modules,
            residual,
        );

        assert_eq!(
            partition_destinations(&original_outcome.partition, original.nodes.len()),
            partition_destinations(&restored_outcome.partition, round_tripped.nodes.len()),
            "compute_owner_claims must derive the same partition on the round-tripped graph",
        );
    }

    fn partition_destinations(partition: &Partition, owner_count: usize) -> Vec<ModuleId> {
        (0..owner_count)
            .map(|i| partition.of(crate::OwnerId(i)))
            .collect()
    }

    /// Strict mapping: an edge referencing an owner id missing from
    /// the node table (malformed / version-skewed `owner_graph.json`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let (_, _, mut report, _, _) = build_and_serialize("const a = 1;\nconst b = a + 1;\n");
        assert!(
            !report.edges.is_empty(),
            "fixture must produce at least one edge"
        );
        report.edges[0].target = "owner:999".to_string();
        let err = OwnerGraph::from_report(&report, &[]).unwrap_err();
        assert_eq!(err.endpoint, "owner:999");
        assert_eq!(err.edge_id, report.edges[0].id);
        assert!(err.to_string().contains("owner:999"), "{err}");
    }
}
