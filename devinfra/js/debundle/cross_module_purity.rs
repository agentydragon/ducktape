//! Program-level cross-module function-purity oracle.
//!
//! The per-chunk purity classifier ([`crate::purity::ChunkCodeGraph`])
//! treats a call to an imported binding as `unknown_call` — it cannot see
//! into the other module's body. On a real app bundle that is the dominant
//! impurity (every `memo(fn)` / `forwardRef(fn)` factory wrap), and via the
//! dataflow-aware S-chain it over-merges atomic factor groups.
//!
//! This oracle closes that gap. Given every module's entry body plus its
//! resolved imports and exports, it computes, per module, the purity verdict
//! for each imported function binding — the map fed to that module's
//! [`ChunkCodeGraph::build_full`] (`imported_purities`).
//!
//! ## Algorithm — bottom-up effect closure
//!
//! Canonical body classification emits direct effects, proof dependencies,
//! and exact obligations to inferred local/imported functions. A single typed
//! worklist across all participating modules propagates finite effects through
//! those obligations. Pure is projected only after closure, so recursive pure
//! components remain pure without optimistic verdicts.
//!
//! Soundness is conservative by construction: an import whose target module or
//! export is absent (external/vendor module not analyzed, a non-function
//! export, an unresolved re-export) simply gets no entry, so the call stays
//! `unknown_call` exactly as before. Pinned assertions remain trusted axioms.

use analysis_model::PrimKinds;
use std::cell::OnceCell;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use js_ast::SourceLineIndex;
use swc_common::{BytePos, Span, Spanned};
use swc_ecma_ast::{Decl, Expr, Lit, ModuleDecl, ModuleItem, Pat, Stmt};

use crate::analysis_inputs::{
    BundleGlobalNames, CallableSummary, CallbackFactoryEvidence, CallbackObjectArguments,
    DeferredRecordMember, DescriptorCopyFactorySummary, ForeignCallSummary,
    ImportedPrimitiveArrayOperandEvidence, ImportedPrimitiveArraySpreadInputEvidence,
    ImportedPrimitiveArraySpreadInputRef, NamespaceWrapperSummary, NativeInputKind,
    NativeIntegrity, NativeIntrinsic, PrimitiveExportCell, ProofDependencies,
    ResolvedBindingValueInput, ResolvedImportedPrimitiveArraySpreadInput,
    ResolvedNativeOperationInput, ReturnedRecordSummary, SelectedBindingValueInput,
    SelectedNativeOperationInput,
};
use crate::execution_evidence::SourceLocation;

use crate::facts::abstract_domain::value::Value;
use crate::facts::{ChunkValueFlow, TopLevelItemView, top_level_item_views};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::purity::{ChunkCodeGraph, Purity, PurityEffectRules, PurityMode};

/// Source chunk name used as a key in program-summary maps; report
/// [`crate::reports::schema::ModuleKey`] identifies a destination module.
pub type SourceModuleKey = String;

/// An import of a single named/default binding, already resolved to the
/// module that defines it and the export read. Namespace imports
/// (`import * as ns`) are intentionally not represented: their call sites are
/// member calls (`ns.foo()`), not the bare-`Ident` callee the imported-purity
/// arm handles.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedImport {
    /// The module that defines the imported binding.
    pub module: SourceModuleKey,
    /// The export name read from that module.
    pub export: String,
    /// Original source specifier for the import binding. Static read
    /// capabilities match this to the corresponding ordinary ESM request.
    pub source_specifier: String,
}

/// A stable export after its imports resolved: its own types and the exports
/// it copies.
#[derive(Debug, Clone)]
pub struct ExportValueSource {
    pub kinds: PrimKinds,
    pub copies: Vec<ResolvedImport>,
    /// Present only for a local export binding. Re-export aliases have no
    /// cell of their own and retain the cells from their target instead.
    pub cell: Option<PrimitiveExportCell>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportPrimitiveValue {
    pub kinds: PrimKinds,
    pub source_cells: BTreeSet<PrimitiveExportCell>,
}

/// Close stable export values over copied imports while preserving all local
/// module-cell provenance along re-export and copy chains.
pub fn resolve_export_primitives(
    sources: BTreeMap<(SourceModuleKey, String), ExportValueSource>,
) -> BTreeMap<(SourceModuleKey, String), ExportPrimitiveValue> {
    let rules = ExportPrimitiveRules(sources.into_iter().collect());
    let count = rules.0.len();
    IterativeInference::new(rules)
        .solve_values(0..count)
        .into_iter()
        .map(|(export, fact)| {
            (
                export,
                ExportPrimitiveValue {
                    kinds: fact.kinds,
                    source_cells: fact.source_cells,
                },
            )
        })
        .collect()
}

#[derive(Clone)]
struct ExportPrimitiveFact {
    kinds: PrimKinds,
    source_cells: BTreeSet<PrimitiveExportCell>,
}

struct ExportPrimitiveRules(Vec<((SourceModuleKey, String), ExportValueSource)>);

impl IterativeRules for ExportPrimitiveRules {
    type Key = (SourceModuleKey, String);
    type Fact = ExportPrimitiveFact;
    type Rule = usize;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (export, source) = &self.0[*rule];
        let mut fact = ExportPrimitiveFact {
            kinds: source.kinds,
            source_cells: source.cell.iter().cloned().collect(),
        };
        for copy in &source.copies {
            let Some(copied) = context.read((copy.module.clone(), copy.export.clone())) else {
                return;
            };
            fact.kinds = fact.kinds.union(copied.kinds);
            fact.source_cells
                .extend(copied.source_cells.iter().cloned());
        }
        context.propose(
            export.clone(),
            ExportPrimitiveFact {
                kinds: fact.kinds,
                source_cells: fact.source_cells,
            },
        );
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let kinds = current.kinds.union(contribution.kinds);
        let changed = kinds != current.kinds
            || contribution
                .source_cells
                .iter()
                .any(|cell| !current.source_cells.contains(cell));
        current.kinds = kinds;
        current.source_cells.extend(contribution.source_cells);
        changed
    }
}

/// One module's purity-relevant surface for the oracle.
pub struct ModulePurityFacts<'a> {
    /// The module entry's top-level items (caller-owned parsed AST).
    pub body: &'a [ModuleItem],
    /// Local import binding name → the export it resolves to.
    pub imports: BTreeMap<String, ResolvedImport>,
    /// Export name → the local binding this module re-exports under it.
    pub exports: BTreeMap<String, String>,
    /// Origin of direct function-body reasons, if the caller retained the
    /// source line index. Imports reuse these locations unchanged.
    pub source: Option<(String, SourceLineIndex)>,
    /// Export name → the package a vendor swap replaces it with. Facts derived
    /// from this body still hold for those exports under the swap's
    /// equivalence promise (SPEC.md § Vendor swap equivalence); summaries
    /// name the package so an author can audit them against it.
    pub replacement_packages: BTreeMap<String, String>,
}

struct PreparedModulePurityFacts<'a> {
    views: Vec<TopLevelItemView<'a>>,
    shadowed: BTreeSet<&'static str>,
    set_iterable_proof: crate::facts::SetIterableProof,
    local_class_construction_proof: crate::facts::LocalClassConstructionProof,
    plain_data_bindings: Vec<String>,
    plain_data_dependencies: BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
    plain_data_inference_uses: BTreeMap<String, BTreeSet<crate::analysis_provenance::InferenceUse>>,
    plain_array_bindings: BTreeSet<String>,
    known_eager_rebindings: BTreeMap<swc_ecma_ast::Id, BTreeSet<u32>>,
    known_lazy_rebindings: BTreeSet<swc_ecma_ast::Id>,
    known_member_write_roots: BTreeMap<swc_ecma_ast::Id, BTreeSet<u32>>,
}

#[derive(Debug, Clone, Copy, Default)]
struct PreparationOptions {
    /// Retain exact mutation roots only for chunks named by an imported-array
    /// spread premise. The ordinary purity path does not need these facts.
    collect_imported_array_write_sites: bool,
}

fn var_decl_for_source_item(item: &ModuleItem) -> Option<&swc_ecma_ast::VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

fn selected_array_declarator<'a>(
    module: &'a ModulePurityFacts<'_>,
    declaration: &crate::analysis_inputs::SourceDeclaration,
    request: &str,
) -> Result<(&'a swc_ecma_ast::VarDeclarator, swc_ecma_ast::Id)> {
    let item = module
        .body
        .get(declaration.body_index)
        .ok_or_else(|| anyhow::anyhow!("{request} selects a missing module item"))?;
    let var = var_decl_for_source_item(item).ok_or_else(|| {
        anyhow::anyhow!("{request} must select a direct var/let/const declaration")
    })?;
    let mut matching_declarators =
        var.decls
            .iter()
            .enumerate()
            .filter_map(|(index, declarator)| {
                let Pat::Ident(binding) = &declarator.name else {
                    return None;
                };
                (binding.id.sym.as_ref() == declaration.binding).then_some((
                    index,
                    declarator,
                    binding.id.to_id(),
                ))
            });
    let Some((selected_index, declarator, binding_id)) = matching_declarators.next() else {
        bail!("{request} must select a simple identifier declarator for its source binding");
    };
    if matching_declarators.next().is_some() {
        bail!("{request} has duplicate simple declarators for its selected binding");
    }
    if var.decls.iter().enumerate().any(|(index, sibling)| {
        index != selected_index
            && binding_targets::binding_names(&sibling.name).any(|binding| binding == binding_id)
    }) {
        bail!("{request} has another declarator that defines its selected binding");
    }
    let Some(Expr::Array(_)) = declarator.init.as_deref().map(Expr::unwrap_parens) else {
        bail!("{request} must select a direct array literal initializer");
    };
    Ok((declarator, binding_id))
}

fn primitive_array_kinds(array: &swc_ecma_ast::ArrayLit, request: &str) -> Result<Vec<PrimKinds>> {
    array
        .elems
        .iter()
        .enumerate()
        .map(|(index, element)| {
            let Some(element) = element else {
                bail!("{request} producer array contains a hole at index {index}");
            };
            if element.spread.is_some() {
                bail!("{request} producer array must contain only primitive literals");
            }
            match element.expr.unwrap_parens() {
                Expr::Lit(Lit::Str(_)) => Ok(PrimKinds::STRING),
                Expr::Lit(Lit::Num(_)) => Ok(PrimKinds::NUMBER),
                Expr::Lit(Lit::Bool(_)) => Ok(PrimKinds::BOOLEAN),
                Expr::Lit(Lit::Null(_)) => Ok(PrimKinds::NULL),
                Expr::Lit(Lit::BigInt(_)) => Ok(PrimKinds::BIGINT),
                _ => bail!("{request} producer array must contain only primitive literals"),
            }
        })
        .collect()
}

fn source_location(module: &ModulePurityFacts<'_>, span: Span) -> Option<SourceLocation> {
    let (source_path, line_index) = module.source.as_ref()?;
    let (start_line, end_line, start_column) =
        line_index.line_range_and_start_column_for_span(span)?;
    Some(SourceLocation {
        source_path: source_path.clone(),
        start_line,
        end_line,
        start_column: Some(start_column),
    })
}

fn resolve_imported_primitive_array_spread_inputs(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &BTreeMap<SourceModuleKey, PreparedModulePurityFacts<'_>>,
    selected: &BTreeMap<
        SourceModuleKey,
        Vec<crate::analysis_inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
) -> Result<BTreeMap<SourceModuleKey, Vec<ResolvedImportedPrimitiveArraySpreadInput>>> {
    let mut resolved = BTreeMap::new();
    for (consumer, inputs) in selected {
        let consumer_facts = modules
            .get(consumer)
            .with_context(|| format!("imported array spread consumer {consumer} is unavailable"))?;
        let mut resolved_inputs = Vec::with_capacity(inputs.len());
        for (input_index, input) in inputs.iter().enumerate() {
            let request = format!(
                "chunk_analysis_options.{consumer}.imported_primitive_array_spread_inputs[{input_index}]"
            );
            let (consumer_declarator, _) =
                selected_array_declarator(consumer_facts, &input.consumer_declaration, &request)?;
            let Expr::Array(consumer_array) = consumer_declarator
                .init
                .as_deref()
                .expect("selected_array_declarator checked its initializer")
                .unwrap_parens()
            else {
                unreachable!("selected_array_declarator checked array initializer")
            };
            let array_operands = consumer_array
                .elems
                .iter()
                .map(|element| {
                    let Some(element) = element else {
                        bail!("{request} consumer initializer contains an array hole");
                    };
                    let Some(token) = element.spread else {
                        bail!("{request} consumer initializer must be spread-only");
                    };
                    let Expr::Ident(binding) = element.expr.unwrap_parens() else {
                        bail!("{request} every spread operand must be a direct import identifier");
                    };
                    Ok((token, binding.to_id(), binding.sym.to_string()))
                })
                .collect::<Result<Vec<_>>>()?;
            let consumer_spread_position = consumer_declarator.span.lo.0;
            if array_operands.len() != input.operands.len() {
                bail!("{request} operand selector count does not match the selected spread array");
            }
            let mut operand_evidence = Vec::with_capacity(input.operands.len());
            let mut token_spans = Vec::with_capacity(input.operands.len());
            let consumer_prepared = prepared
                .get(consumer)
                .with_context(|| format!("{request} consumer {consumer} was not prepared"))?;
            for (position, (operand, (token, consumer_id, actual_binding))) in
                input.operands.iter().zip(array_operands).enumerate()
            {
                if consumer_prepared
                    .known_eager_rebindings
                    .get(&consumer_id)
                    .is_some_and(|positions| {
                        positions
                            .iter()
                            .any(|write| *write < consumer_spread_position)
                    })
                {
                    bail!(
                        "{request} imported array binding {} is contradicted by a known source rebind before its selected spread",
                        actual_binding
                    );
                }
                if consumer_prepared
                    .known_lazy_rebindings
                    .contains(&consumer_id)
                {
                    bail!(
                        "{request} unsupported: imported array binding {} has a known lazy rebind whose invocation order through the selected spread is not established",
                        actual_binding
                    );
                }
                if consumer_prepared
                    .known_member_write_roots
                    .contains_key(&consumer_id)
                {
                    bail!(
                        "{request} unsupported: imported array binding {} has a known indexed/member write whose execution ordering through the selected spread is not established",
                        actual_binding
                    );
                }
                if actual_binding != operand.consumer_binding {
                    bail!(
                        "{request} operand {} no longer resolves to its selected import binding",
                        operand.selector_local
                    );
                }
                let import = consumer_facts
                    .imports
                    .get(&actual_binding)
                    .with_context(|| {
                        format!(
                            "{request} operand {} is not a direct named/default import",
                            operand.selector_local
                        )
                    })?;
                if import.module != operand.provider_chunk {
                    bail!(
                        "{request} operand {} imports from {}, but its selected provider chunk is {}",
                        operand.selector_local,
                        import.module,
                        operand.provider_chunk
                    );
                }
                let provider_facts = modules.get(&import.module).with_context(|| {
                    format!(
                        "{request} selected provider {} is unavailable",
                        import.module
                    )
                })?;
                let provider_binding =
                    provider_facts
                        .exports
                        .get(&import.export)
                        .with_context(|| {
                            format!(
                                "{request} provider export {}:{} has no direct source binding",
                                import.module, import.export
                            )
                        })?;
                if provider_binding != &operand.provider_declaration.binding {
                    bail!(
                        "{request} provider export {}:{} does not resolve to the selected producer declaration",
                        import.module,
                        import.export
                    );
                }
                let (producer_declarator, producer_id) = selected_array_declarator(
                    provider_facts,
                    &operand.provider_declaration,
                    &format!("{request}.operands[{position}].provider_source_match"),
                )?;
                let provider_prepared = prepared.get(&import.module).with_context(|| {
                    format!(
                        "{request} selected provider {} was not prepared",
                        import.module
                    )
                })?;
                if provider_prepared
                    .known_eager_rebindings
                    .contains_key(&producer_id)
                    || provider_prepared
                        .known_lazy_rebindings
                        .contains(&producer_id)
                {
                    bail!(
                        "{request} unsupported: producer binding {} has a known source rebind in provider {}, but current cross-module facts do not establish its ordering through the selected spread",
                        provider_binding,
                        import.module
                    );
                }
                if provider_prepared
                    .known_member_write_roots
                    .contains_key(&producer_id)
                {
                    bail!(
                        "{request} unsupported: producer array {} has a known indexed/member write in provider {}, but current cross-module facts do not establish its ordering through the selected spread",
                        provider_binding,
                        import.module
                    );
                }
                let Expr::Array(producer_array) = producer_declarator
                    .init
                    .as_deref()
                    .expect("selected_array_declarator checked its initializer")
                    .unwrap_parens()
                else {
                    unreachable!("selected_array_declarator checked array initializer")
                };
                let primitive_slots = primitive_array_kinds(
                    producer_array,
                    &format!("{request}.operands[{position}].provider_source_match"),
                )?;
                token_spans.push(token);
                operand_evidence.push(ImportedPrimitiveArrayOperandEvidence {
                    selector_local: operand.selector_local.clone(),
                    consumer_binding: actual_binding,
                    provider_module: import.module.clone(),
                    provider_export: import.export.clone(),
                    provider_local: provider_binding.clone(),
                    source_specifier: import.source_specifier.clone(),
                    provider_source_match: operand.provider_source_match.clone(),
                    provider_source_sha256: operand.audited_provider_source_sha256.clone(),
                    primitive_slots,
                    provider_source_location: source_location(provider_facts, producer_array.span),
                });
            }
            let evidence = ImportedPrimitiveArraySpreadInputEvidence {
                reference: ImportedPrimitiveArraySpreadInputRef {
                    defining_chunk: consumer.clone(),
                    input_index,
                },
                spec_location: request,
                consumer_source_match: input.source_match.clone(),
                consumer_source_sha256: input.audited_source_sha256.clone(),
                selected_consumer_binding: input.consumer_declaration.binding.clone(),
                consumer_source_location: source_location(consumer_facts, consumer_array.span),
                operands: operand_evidence,
                lifetime: input.lifetime.clone(),
                note: input.note.clone(),
            };
            resolved_inputs.push(ResolvedImportedPrimitiveArraySpreadInput {
                evidence,
                consumer_initializer_span: consumer_array.span,
                spread_token_spans: token_spans,
            });
        }
        resolved.insert(consumer.clone(), resolved_inputs);
    }
    Ok(resolved)
}

impl<'a> ModulePurityFacts<'a> {
    /// `structural_flow` is this body's flow under the same inputs, when the
    /// summary pass already solved it.
    fn prepare(
        &self,
        inputs: &crate::analysis_inputs::AnalysisInputs,
        structural_flow: Option<ChunkValueFlow>,
        options: PreparationOptions,
    ) -> PreparedModulePurityFacts<'a> {
        let source = crate::facts::collect_chunk_source_facts_from_items(self.body, None, |_| None);
        let structural = match structural_flow {
            Some(flow) => crate::facts::complete_chunk_structural_with_flow(
                source,
                &inputs.assumptions.argument_inputs,
                flow,
            ),
            None => crate::facts::complete_chunk_structural_with_inputs(
                source,
                &inputs.assumptions.native_operation_inputs,
                &inputs.stable_set_spreads,
                &inputs.inferred_set_constructions,
                &inputs.assumptions.argument_inputs,
            ),
        };
        let views = structural.body;
        let shadowed = structural.shadowed;
        let value_flow = structural.value_flow;
        let plain_data_bindings = structural.plain_data_bindings;
        let plain_data_dependencies = structural.plain_data_dependencies;
        let plain_data_inference_uses = structural.plain_data_inference_uses;
        let source_semantic_facts = structural.source_semantic_facts;
        let stable_array_spread_shapes = structural.stable_array_spread_shapes;
        let (known_eager_rebindings, known_lazy_rebindings, known_member_write_roots) =
            if options.collect_imported_array_write_sites {
                let mut known_eager_rebindings = BTreeMap::<_, BTreeSet<_>>::new();
                let mut known_lazy_rebindings = BTreeSet::new();
                let mut known_member_write_roots = BTreeMap::<_, BTreeSet<_>>::new();
                let mut var_initializer_totals = BTreeMap::<_, usize>::new();
                for statement in &structural.per_statement {
                    for (binding, count) in &statement.var_initializer_counts {
                        *var_initializer_totals.entry(binding.clone()).or_default() += *count;
                    }
                }
                for statement in &structural.per_statement {
                    let write_position = views
                        .get(statement.ordinal.0)
                        .map(|view| view.as_module_item().span().lo.0)
                        .unwrap_or(u32::MAX);
                    for binding in &statement.rebinds.eager {
                        known_eager_rebindings
                            .entry(binding.clone())
                            .or_default()
                            .insert(write_position);
                    }
                    for (binding, count) in &statement.var_initializer_counts {
                        if *count > 0
                            && var_initializer_totals
                                .get(binding)
                                .is_some_and(|total| *total > 1)
                        {
                            known_eager_rebindings
                                .entry(binding.clone())
                                .or_default()
                                .insert(write_position);
                        }
                    }
                    known_lazy_rebindings.extend(statement.rebinds.lazy.iter().cloned());
                    for binding in &statement.member_write_roots {
                        known_member_write_roots
                            .entry(binding.clone())
                            .or_default()
                            .insert(write_position);
                    }
                }
                (
                    known_eager_rebindings,
                    known_lazy_rebindings,
                    known_member_write_roots,
                )
            } else {
                (BTreeMap::new(), BTreeSet::new(), BTreeMap::new())
            };
        let plain_array_bindings =
            crate::facts::abstract_domain::heap::plain_array_bindings(&views, &shadowed);
        let set_iterable_proof = structural.set_iterable_shapes;
        let local_class_construction_proof = structural.local_class_shapes;
        let prepared_sync_index =
            crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index_with_source_facts(
                &views,
                &value_flow,
                inputs,
                &plain_array_bindings,
                crate::facts::sync_effects::execution_index::interpreter::PreparedSyncShapeInputs {
                    local_class_construction: &local_class_construction_proof,
                    set_iterable_shapes: set_iterable_proof,
                    stable_array_spread_shapes,
                    shadowed: &shadowed,
                    source_facts: source_semantic_facts.as_deref(),
                },
            );
        let local_class_construction_proof =
            prepared_sync_index.local_class_construction_proof().clone();
        let set_iterable_proof = prepared_sync_index.set_iterable_proof().clone();
        PreparedModulePurityFacts {
            views,
            shadowed,
            set_iterable_proof,
            local_class_construction_proof,
            plain_data_bindings,
            plain_data_dependencies,
            plain_data_inference_uses,
            plain_array_bindings,
            known_eager_rebindings,
            known_lazy_rebindings,
            known_member_write_roots,
        }
    }

    /// Test helper for checking a module graph with pre-resolved imports.
    #[cfg(test)]
    fn build_graph(
        &self,
        prepared: &PreparedModulePurityFacts<'_>,
        imported: &BTreeMap<String, Arc<Purity>>,
    ) -> ChunkCodeGraph {
        let mut locate = |span: Span| {
            let (source_path, index) = self.source.as_ref()?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(SourceLocation {
                source_path: source_path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        ChunkCodeGraph::build_full_with_prepared_plain_data(
            &prepared.views,
            &prepared.shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            imported,
            &BTreeSet::new(),
            &prepared.set_iterable_proof,
            &prepared.local_class_construction_proof,
            &prepared.plain_data_bindings,
            &prepared.plain_data_dependencies,
            &prepared.plain_data_inference_uses,
            &prepared.plain_array_bindings,
            &BTreeSet::new(),
            Some(&mut locate),
        )
    }

    /// Raw value projection of each local import under the program-wide
    /// export facts. This does not prove the consumer's normal imported
    /// `GetValue`; candidate execution must join it with a read capability.
    pub fn imported_values(
        &self,
        export_values: &BTreeMap<(SourceModuleKey, String), Value>,
    ) -> BTreeMap<String, Value> {
        self.imports
            .iter()
            .filter_map(|(local, target)| {
                let value = export_values.get(&(target.module.clone(), target.export.clone()))?;
                Some((local.clone(), *value))
            })
            .collect()
    }

    fn imported_purities(
        &self,
        export_purity: &BTreeMap<(SourceModuleKey, String), Arc<Purity>>,
    ) -> BTreeMap<String, Arc<Purity>> {
        self.imports
            .iter()
            .filter_map(|(local, target)| {
                let key = (target.module.clone(), target.export.clone());
                export_purity
                    .get(&key)
                    .map(|purity| (local.clone(), purity.clone()))
            })
            .collect()
    }
}

/// Compute, per module, the imported-binding purity map to feed that module's
/// callers. See the module docs for the shared effects closure.
///
/// `asserted_pure` carries author-asserted pure exports keyed by defining
/// module (`TransformSpec::chunk_export_purity`). Assertions are trusted
/// axioms: they enter the verdict map as `Pure` even when the export's
/// binding is not a classifiable chunk-top function (interop wrappers,
/// re-exported callables), and the effects closure never demotes them. An assertion
/// naming a module or export the program doesn't have is reported to stderr
/// as a dangling assertion (likely a typo or a stale entry) and ignored.
pub fn resolve_imported_purities(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    callable_summaries: &mut ResolvedCallableSummaries,
) -> Result<BTreeMap<SourceModuleKey, BTreeMap<String, Arc<Purity>>>> {
    // Only exported verdicts read by a named/default import can affect the
    // returned maps or another module's closure. Namespace member calls are
    // intentionally outside this oracle.
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| (target.module.clone(), target.export.clone()))
        .collect();
    // Author-asserted axioms stay pinned Pure, even when no source body exists.
    let mut pinned: BTreeSet<(SourceModuleKey, String)> = BTreeSet::new();
    let mut export_purity: BTreeMap<(SourceModuleKey, String), Arc<Purity>> = BTreeMap::new();
    for (module, export_names) in asserted_pure {
        for export_name in export_names {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                eprintln!(
                    "cross_module_purity: dangling pure_exports assertion \
                     {module}:{export_name} — no such analyzed module export; ignoring"
                );
                continue;
            }
            export_purity.insert(
                (module.clone(), export_name.clone()),
                Arc::new(Purity::pure()),
            );
            pinned.insert((module.clone(), export_name.clone()));
        }
    }

    // Establish module-qualified function identities before extracting any
    // bodies, so imports become exact edges in one program-wide effect graph.
    let function_keys_by_module: BTreeMap<_, _> = modules
        .iter()
        .map(|(module, facts)| {
            let views = top_level_item_views(facts.body);
            (
                module.clone(),
                ChunkCodeGraph::inferred_function_keys(&views, module, PurityMode::Export),
            )
        })
        .collect();
    let inferred_export_keys: BTreeMap<_, _> = required
        .iter()
        .filter(|slot| !pinned.contains(*slot))
        .filter_map(|(module, export)| {
            let local = modules.get(module)?.exports.get(export)?;
            let function = function_keys_by_module.get(module)?.get(local)?;
            Some(((module.clone(), export.clone()), function.clone()))
        })
        .collect();
    let imported_array_write_fact_chunks: BTreeSet<_> = callable_summaries
        .selected_imported_primitive_array_spread_inputs
        .iter()
        .flat_map(|(consumer, inputs)| {
            std::iter::once(consumer.clone()).chain(inputs.iter().flat_map(|input| {
                input
                    .operands
                    .iter()
                    .map(|operand| operand.provider_chunk.clone())
            }))
        })
        .collect();

    // Authoritative source/native/captured-input facts are fixed before this
    // one effects closure starts. Declared chunk-local purity hints remain
    // absent from export mode, as before.
    let progress = std::env::var_os("DUCKTAPE_DEBUNDLE_PROGRESS").is_some();
    let jobs: Vec<_> = modules
        .iter()
        .filter(|(key, _)| {
            imported_array_write_fact_chunks.contains(*key)
                || inferred_export_keys
                    .keys()
                    .any(|(module, _)| module == *key)
        })
        .map(|(key, facts)| {
            // Rebuilding a provider's execution index must retain the same captured
            // receiver and imported-call facts that established the program census.
            // A native-integrity result alone cannot refine the provider's local
            // mutation census, which would otherwise reintroduce unknown aliases.
            let inputs = crate::analysis_inputs::AnalysisInputs {
                native_integrity: callable_summaries.native_integrity.clone(),
                inferred_set_constructions: callable_summaries
                    .inferred_set_constructions
                    .get(key)
                    .cloned()
                    .unwrap_or_default(),
                stable_set_spreads: callable_summaries
                    .stable_set_spreads
                    .get(key)
                    .cloned()
                    .unwrap_or_default(),
                assumptions: crate::analysis_inputs::AnalysisAssumptions {
                    captured_value_inputs: callable_summaries
                        .value_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    native_operation_inputs: callable_summaries
                        .native_operation_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    argument_inputs: callable_summaries
                        .argument_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    ..Default::default()
                },
                imported_callables: callable_summaries
                    .imported_callables
                    .get(key)
                    .cloned()
                    .unwrap_or_default(),
                ..Default::default()
            };
            (
                key,
                facts,
                inputs,
                callable_summaries.structural_flows.remove(key),
                imported_array_write_fact_chunks.contains(key),
            )
        })
        .collect();
    // Each provider prepares from its own body and inputs alone.
    let prepared: BTreeMap<_, _> = crate::swc_parallel::map_with_swc_globals(
        jobs,
        |(key, facts, inputs, flow, collect_write_sites)| {
            if progress {
                eprintln!(
                    "[debundle progress] chunk={key} phase=prepare_cross_module_purity state=start"
                );
            }
            let facts_prepared = facts.prepare(
                &inputs,
                flow,
                PreparationOptions {
                    collect_imported_array_write_sites: collect_write_sites,
                },
            );
            if progress {
                eprintln!(
                    "[debundle progress] chunk={key} phase=prepare_cross_module_purity state=end"
                );
            }
            (key.clone(), facts_prepared)
        },
    )
    .into_iter()
    .collect();

    callable_summaries.imported_primitive_array_spread_inputs =
        resolve_imported_primitive_array_spread_inputs(
            modules,
            &prepared,
            &callable_summaries.selected_imported_primitive_array_spread_inputs,
        )?;

    let mut bodies = BTreeMap::new();
    let no_declared_pure = BTreeSet::new();
    let no_declared_pure_members = BTreeMap::new();
    for (module, prepared_facts) in &prepared {
        let facts = &modules[module];
        let imported_targets: BTreeMap<_, _> = facts
            .imports
            .iter()
            .filter_map(|(local, target)| {
                inferred_export_keys
                    .get(&(target.module.clone(), target.export.clone()))
                    .map(|key| (local.clone(), key.clone()))
            })
            .collect();
        let imported_purities: BTreeMap<_, _> = facts
            .imports
            .iter()
            .filter(|(_, target)| pinned.contains(&(target.module.clone(), target.export.clone())))
            .map(|(local, _)| (local.clone(), Arc::new(Purity::pure())))
            .collect();
        let mut locate = |span: Span| {
            let (source_path, index) = facts.source.as_ref()?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(SourceLocation {
                source_path: source_path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        let shape_inputs = crate::purity::PurityShapeInputs {
            shadowed: &prepared_facts.shadowed,
            declared_pure_new: &no_declared_pure,
            declared_pure_members: &no_declared_pure_members,
            imported_purities: &imported_purities,
            fluent_bindings: &no_declared_pure,
            set_iterable_proof: &prepared_facts.set_iterable_proof,
            local_class_construction_proof: &prepared_facts.local_class_construction_proof,
            plain_data_bindings: &prepared_facts.plain_data_bindings,
            plain_data_dependencies: &prepared_facts.plain_data_dependencies,
            plain_data_inference_uses: &prepared_facts.plain_data_inference_uses,
            plain_array_bindings: &prepared_facts.plain_array_bindings,
            stable_var_number_candidates: &BTreeSet::new(),
        };
        let call_context = crate::purity::PurityCallContext {
            module,
            mode: PurityMode::Export,
            declared_pure: &no_declared_pure,
            imported_targets: &imported_targets,
        };
        let transfers = ChunkCodeGraph::effect_transfers(
            &prepared_facts.views,
            &shape_inputs,
            &call_context,
            Some(&mut locate),
        );
        bodies.extend(transfers);
    }
    // Every module-qualified function body shares this one finite effects
    // store/worklist, including mutually recursive imports across modules.
    let solved = PurityEffectRules::new(bodies).solve_to_purities();
    for (slot, function) in inferred_export_keys {
        if let Some(purity) = solved.get(&function) {
            export_purity.insert(slot, purity.clone());
        }
    }

    Ok(modules
        .iter()
        .map(|(key, facts)| (key.clone(), facts.imported_purities(&export_purity)))
        .collect())
}

/// Callback transfer is independent of purity, including asserted purity.
/// Compose checked source call timing and returned capabilities separately
/// from author assertions. Missing exports and unsupported bodies stay opaque.
#[derive(Default)]
pub struct ResolvedCallableSummaries {
    pub imported_callables: BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>>,
    pub native_integrity: Option<NativeIntegrity>,
    pub value_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedBindingValueInput>>,
    pub native_operation_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedNativeOperationInput>>,
    pub imported_primitive_array_spread_inputs: BTreeMap<
        SourceModuleKey,
        Vec<crate::analysis_inputs::ResolvedImportedPrimitiveArraySpreadInput>,
    >,
    pub(crate) selected_imported_primitive_array_spread_inputs: BTreeMap<
        SourceModuleKey,
        Vec<crate::analysis_inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
    pub stable_set_spreads:
        BTreeMap<SourceModuleKey, BTreeMap<u32, crate::analysis_inputs::StableSetIterableUse>>,
    pub inferred_set_constructions:
        BTreeMap<SourceModuleKey, BTreeMap<u32, crate::analysis_inputs::InferredSetConstruction>>,
    pub argument_inputs:
        BTreeMap<SourceModuleKey, Vec<crate::analysis_inputs::ResolvedArgumentInput>>,
    /// The whole-program bundle-owned globals the census used.
    pub bundle_globals: BundleGlobalNames,
    /// Structural value flows already solved while deriving summaries, which
    /// the purity oracle's census of the same body takes instead of solving
    /// again.
    pub(crate) structural_flows: BTreeMap<SourceModuleKey, ChunkValueFlow>,
    /// Negative facts from the already prepared original provider callables.
    /// Missing entries are unknown, not proof that a provider is effect free.
    known_original_external_writes: BTreeMap<SourceModuleKey, BTreeMap<String, String>>,
}

impl ResolvedCallableSummaries {
    /// A source-known contradiction for one requested original export. No
    /// result means this bounded negative check found no contradiction.
    pub fn known_original_external_write(&self, module: &str, export: &str) -> Option<&str> {
        self.known_original_external_writes
            .get(module)?
            .get(export)
            .map(String::as_str)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct CallableExportKey {
    module: SourceModuleKey,
    export: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct CallableImportKey {
    module: SourceModuleKey,
    local: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum CallableFactKey {
    Export(CallableExportKey),
    Import(CallableImportKey),
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct CallableImportProjection {
    imported: CallableImportKey,
    provider: CallableExportKey,
}

#[derive(Debug, Clone, Default)]
struct CallableExportProof {
    summary: CallableSummary,
    dependencies: ProofDependencies,
}

impl CallableExportProof {
    fn add(&mut self, summary: CallableSummary, dependencies: ProofDependencies) {
        self.summary.merge(summary);
        self.dependencies.extend(&dependencies);
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum CallableProjectionRule {
    Export(CallableExportKey),
    Import(CallableImportProjection),
}

struct CallableProjectionRules {
    exports: BTreeMap<CallableExportKey, CallableExportProof>,
    imports: Vec<CallableImportProjection>,
}

impl CallableProjectionRules {
    fn new(
        exports: BTreeMap<CallableExportKey, CallableExportProof>,
        modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    ) -> Self {
        let imports = modules
            .iter()
            .flat_map(|(module, facts)| {
                facts
                    .imports
                    .iter()
                    .map(|(local, target)| CallableImportProjection {
                        imported: CallableImportKey {
                            module: module.clone(),
                            local: local.clone(),
                        },
                        provider: CallableExportKey {
                            module: target.module.clone(),
                            export: target.export.clone(),
                        },
                    })
            })
            .collect();
        Self { exports, imports }
    }

    fn producers(&self) -> BTreeSet<CallableProjectionRule> {
        self.exports
            .keys()
            .cloned()
            .map(CallableProjectionRule::Export)
            .chain(
                self.imports
                    .iter()
                    .cloned()
                    .map(CallableProjectionRule::Import),
            )
            .collect()
    }
}

impl IterativeRules for CallableProjectionRules {
    type Key = CallableFactKey;
    type Fact = CallableSummary;
    type Rule = CallableProjectionRule;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            CallableProjectionRule::Export(key) => {
                let Some(proof) = self.exports.get(key) else {
                    return;
                };
                context.assume(&proof.dependencies);
                context.propose(CallableFactKey::Export(key.clone()), proof.summary.clone());
            }
            CallableProjectionRule::Import(projection) => {
                let provider = CallableFactKey::Export(projection.provider.clone());
                let Some(summary) = context.read(provider).cloned() else {
                    return;
                };
                context.propose(
                    CallableFactKey::Import(projection.imported.clone()),
                    summary,
                );
            }
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous = current.clone();
        current.merge(contribution);
        *current != previous
    }
}

fn solve_callable_import_projections(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    exports: BTreeMap<CallableExportKey, CallableExportProof>,
) -> BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>> {
    let rules = CallableProjectionRules::new(exports, modules);
    let producers = rules.producers();
    let solved = IterativeInference::new(rules).solve(producers);
    modules
        .iter()
        .map(|(module, facts)| {
            let bindings = facts
                .imports
                .keys()
                .filter_map(|local| {
                    let key = CallableFactKey::Import(CallableImportKey {
                        module: module.clone(),
                        local: local.clone(),
                    });
                    solved
                        .get(&key)
                        .map(|judgment| (local.clone(), judgment.value().clone()))
                })
                .collect();
            (module.clone(), bindings)
        })
        .collect()
}

/// The author inputs and host claims `resolve_imported_callable_summaries`
/// checks the providers against.
pub struct CallableSummaryInputs<'a> {
    /// Exact original exports selected by audited initializer call inputs.
    pub requested_original_provider_exports: &'a BTreeSet<(SourceModuleKey, String)>,
    pub selected_value_inputs: &'a BTreeMap<SourceModuleKey, Vec<SelectedBindingValueInput>>,
    pub selected_native_operation_inputs:
        &'a BTreeMap<SourceModuleKey, Vec<SelectedNativeOperationInput>>,
    pub selected_imported_primitive_array_spread_inputs: &'a BTreeMap<
        SourceModuleKey,
        Vec<crate::analysis_inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
    pub argument_inputs:
        &'a BTreeMap<SourceModuleKey, Vec<crate::analysis_inputs::ResolvedArgumentInput>>,
    pub predefined_globals: spec::PredefinedGlobals,
    pub no_accessor_install_callables:
        &'a BTreeMap<SourceModuleKey, Vec<crate::analysis_inputs::ResolvedNoAccessorInstall>>,
    /// `program_premises.intrinsics_never_written` families.
    pub intrinsics_never_written: BTreeSet<NativeIntrinsic>,
}

pub fn resolve_imported_callable_summaries(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    descriptor_copy_asserted: &BTreeMap<
        SourceModuleKey,
        BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
    >,
    inputs: CallableSummaryInputs<'_>,
) -> Result<ResolvedCallableSummaries> {
    let CallableSummaryInputs {
        requested_original_provider_exports,
        selected_value_inputs,
        selected_native_operation_inputs,
        selected_imported_primitive_array_spread_inputs,
        argument_inputs,
        predefined_globals,
        no_accessor_install_callables,
        intrinsics_never_written,
    } = inputs;
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| CallableExportKey {
            module: target.module.clone(),
            export: target.export.clone(),
        })
        .collect();
    let mut export_summaries: BTreeMap<CallableExportKey, CallableExportProof> = BTreeMap::new();
    for (module, exports) in asserted {
        for export in exports {
            if modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                export_summaries
                    .entry(CallableExportKey {
                        module: module.clone(),
                        export: export.clone(),
                    })
                    .or_default()
                    .add(
                        CallableSummary::callback_factory(
                            CallbackFactoryEvidence::AuthorAssertion {
                                defining_module: module.clone(),
                                export: export.clone(),
                            },
                        ),
                        ProofDependencies::default(),
                    );
            } else {
                eprintln!(
                    "cross_module_purity: dangling callback_factory_exports assertion {module}:{export} — no such analyzed module export; ignoring"
                );
            }
        }
    }
    // The bundle-owned globals are a whole-program greatest fixed point over
    // every module's writes; a second preparation reads them.
    let prepare = |bundle_globals: &BundleGlobalNames| -> Result<_> {
        let mut prepared = BTreeMap::new();
        let mut resolved_value_inputs = BTreeMap::new();
        let mut resolved_native_operation_inputs = BTreeMap::new();
        for (module, facts) in modules {
            let locals = facts
                .exports
                .iter()
                .filter(|(export, _)| {
                    required.contains(&CallableExportKey {
                        module: module.clone(),
                        export: (*export).clone(),
                    })
                })
                .map(|(_, local)| local.clone())
                .collect();
            let views = top_level_item_views(facts.body);
            let mut prepared_module =
                crate::facts::sync_effects::execution_index::prepare_callable_capabilities(
                    &views,
                    crate::facts::StableArraySpreadShapes::collect(facts.body, &views),
                    locals,
                    selected_value_inputs
                        .get(module)
                        .cloned()
                        .unwrap_or_default(),
                    selected_native_operation_inputs
                        .get(module)
                        .cloned()
                        .unwrap_or_default(),
                    predefined_globals,
                    bundle_globals.clone(),
                );
            if !prepared_module.value_input_errors().is_empty() {
                bail!(
                    "invalid captured value input for provider {module}: {}",
                    prepared_module.value_input_errors().join("; ")
                );
            }
            prepared_module.locate_value_inputs(facts.source.as_ref());
            let module_inputs = prepared_module.resolved_value_inputs();
            if !module_inputs.is_empty() && facts.source.is_none() {
                bail!(
                    "captured value input for provider {module} has no retained parser source locations"
                );
            }
            if !module_inputs.is_empty() {
                resolved_value_inputs.insert(module.clone(), module_inputs);
            }
            if !prepared_module.native_operation_input_errors().is_empty() {
                bail!(
                    "invalid native operation input for provider {module}: {}",
                    prepared_module.native_operation_input_errors().join("; ")
                );
            }
            prepared_module.locate_native_operation_inputs(facts.source.as_ref());
            let module_native_inputs = prepared_module.resolved_native_operation_inputs();
            if !module_native_inputs.is_empty() && facts.source.is_none() {
                bail!(
                    "native operation input for provider {module} has no retained parser source locations"
                );
            }
            if !module_native_inputs.is_empty() {
                resolved_native_operation_inputs.insert(module.clone(), module_native_inputs);
            }
            prepared_module.locate_native_writes(module, facts.source.as_ref());
            prepared.insert(module, prepared_module);
        }
        Ok((
            prepared,
            resolved_value_inputs,
            resolved_native_operation_inputs,
        ))
    };
    let (mut prepared, mut resolved_value_inputs, mut resolved_native_operation_inputs) =
        prepare(&BundleGlobalNames::default())?;
    let mut bundle_globals = BundleGlobalNames::default();
    if !predefined_globals.is_unknown() {
        bundle_globals.fresh_collections = true;
        for module in prepared.values() {
            let candidates = module.bundle_global_candidates();
            bundle_globals.names.extend(candidates.names);
            bundle_globals.registered_symbols |= candidates.registered_symbols;
        }
        loop {
            let mut next = bundle_globals.clone();
            next.no_accessor_install_callables.clear();
            for (module, facts) in &prepared {
                let survivors = facts.bundle_global_survivors(
                    &bundle_globals,
                    no_accessor_install_callables
                        .get(*module)
                        .map_or(&[], Vec::as_slice),
                );
                next.names.retain(|name| survivors.names.contains(name));
                next.registered_symbols &= survivors.registered_symbols;
                next.fresh_collections &= survivors.fresh_collections;
                next.no_accessor_install_callables
                    .extend(survivors.no_accessor_install_callables);
            }
            if next == bundle_globals {
                break;
            }
            bundle_globals = next;
        }
        if bundle_globals != BundleGlobalNames::default() {
            (
                prepared,
                resolved_value_inputs,
                resolved_native_operation_inputs,
            ) = prepare(&bundle_globals)?;
        }
    }
    let mut requested_original_exports = (*requested_original_provider_exports).clone();
    let mut pending = requested_original_exports
        .iter()
        .cloned()
        .collect::<Vec<_>>();
    while let Some((module, export)) = pending.pop() {
        let Some(local) = modules
            .get(&module)
            .and_then(|facts| facts.exports.get(&export))
        else {
            continue;
        };
        let Some(target) = modules[&module].imports.get(local) else {
            continue;
        };
        let next = (target.module.clone(), target.export.clone());
        if requested_original_exports.insert(next.clone()) {
            pending.push(next);
        }
    }
    let mut known_original_external_writes = BTreeMap::new();
    for (module, export) in requested_original_exports {
        let Some(index) = prepared.get(&module) else {
            continue;
        };
        let Some(local) = modules[&module].exports.get(&export) else {
            continue;
        };
        if let Some(reason) = index.known_external_write_for_local(local) {
            known_original_external_writes
                .entry(module)
                .or_insert_with(BTreeMap::new)
                .insert(export, reason);
        }
    }
    for input in selected_native_operation_inputs
        .values()
        .flatten()
        .filter(|input| {
            matches!(
                input.evidence.operation,
                NativeInputKind::Operation(
                    spec::NativeOperationKind::Symbol
                        | spec::NativeOperationKind::SymbolFor
                        | spec::NativeOperationKind::PromiseResolve
                        | spec::NativeOperationKind::TextEncoderEmpty
                        | spec::NativeOperationKind::MapEmpty
                        | spec::NativeOperationKind::WeakMapEmpty
                        | spec::NativeOperationKind::SetEmpty
                )
            )
        })
    {
        if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_global_native_operation_mutation_owner(input.evidence.operation)
                .map(|owner| (module, owner))
        }) {
            bail!(
                "{} is contradicted by a positively identified global {} replacement or operation mutation in provider {module}, source unit {}",
                input.evidence.spec_location,
                match input.evidence.operation {
                    NativeInputKind::Operation(
                        spec::NativeOperationKind::Symbol | spec::NativeOperationKind::SymbolFor,
                    ) => "Symbol",
                    NativeInputKind::Operation(spec::NativeOperationKind::TextEncoderEmpty) =>
                        "TextEncoder",
                    NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty) => "Map",
                    NativeInputKind::Operation(spec::NativeOperationKind::WeakMapEmpty) => {
                        "WeakMap"
                    }
                    NativeInputKind::Operation(spec::NativeOperationKind::SetEmpty) => "Set",
                    _ => "Promise",
                },
                owner.0
            );
        }
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_object_freeze_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_call_identity_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Object.freeze slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::SetEmptyPrivateField)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_global_set_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(
                        spec::NativeCallIdentityKind::SetEmptyPrivateField,
                    )
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_call_identity_inputs");
        bail!(
            "{request} is contradicted by a positively identified global Set slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if let Some(input) = selected_native_operation_inputs
        .values()
        .flatten()
        .find(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_symbol_for_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        bail!(
            "{} is contradicted by a positively identified Symbol.for replacement or accessor installation in provider {module}, source unit {}",
            input.evidence.spec_location,
            owner.0
        );
    }
    let mut stable_set_spreads = BTreeMap::new();
    let no_spreads = BTreeMap::new();
    for (module, inputs) in &resolved_native_operation_inputs {
        let uses = crate::facts::certify_stable_set_spreads(inputs)?;
        if !uses.is_empty() {
            stable_set_spreads.insert(module.clone(), uses);
        }
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_set_protocol_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_operation_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Set construction protocol slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_set_spread_protocol_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_operation_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Set spread protocol slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    for (provider, inputs) in &resolved_native_operation_inputs {
        for input in inputs {
            if !matches!(
                input.evidence.operation,
                NativeInputKind::Operation(
                    spec::NativeOperationKind::ObjectOwnDataFunctionRead
                        | spec::NativeOperationKind::SymbolWellKnownClassKeys
                        | spec::NativeOperationKind::SymbolPrimitiveClassKeys
                )
            ) {
                continue;
            }
            let (family, property) = prepared[provider]
                .native_input_read_property(input)
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "{} lost its exact native own-data read during preparation",
                        input.evidence.spec_location
                    )
                })?;
            if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
                facts
                    .known_native_read_contradiction_owner(
                        input.evidence.operation,
                        family,
                        property,
                    )
                    .map(|owner| (module, owner))
            }) {
                bail!(
                    "{} is contradicted by a known {family:?} binding or {family:?}.{property} write in provider {module}, source unit {}",
                    input.evidence.spec_location,
                    owner.0
                );
            }
        }
    }
    // Source inference must finish before any root can seed structural flow.
    // The completed raw census is conservative; later branch refinement may
    // not retroactively introduce a root into an already solved flow.
    let mut preflow_native_integrity =
        crate::facts::sync_effects::execution_index::resolve_native_integrity(
            prepared.values().map(|facts| facts.native_writes()),
        );
    // Validate the existing program premise against the conservative raw
    // census too. Failure only declines this early inference; the established
    // refined-census check below remains authoritative for the author input.
    let preflow_premises_hold = intrinsics_never_written.is_empty()
        || crate::facts::sync_effects::execution_index::apply_intrinsics_never_written(
            &mut preflow_native_integrity,
            &intrinsics_never_written,
        )
        .is_ok();
    let inferred_set_constructions: BTreeMap<_, _> = prepared
        .iter()
        .filter_map(|(module, facts)| {
            if !preflow_premises_hold {
                return None;
            }
            let proofs = facts.inferred_set_constructions(&preflow_native_integrity);
            (!proofs.is_empty()).then_some(((*module).clone(), proofs))
        })
        .collect();

    // At most one structural flow per module, solved on first need and kept
    // for the purity oracle (`ModulePurityFacts::prepare`).
    let structural_flows: BTreeMap<&SourceModuleKey, OnceCell<ChunkValueFlow>> = modules
        .keys()
        .map(|module| (module, OnceCell::new()))
        .collect();
    let structural_flow = |module: &SourceModuleKey, views: &[TopLevelItemView<'_>]| {
        structural_flows[module].get_or_init(|| {
            crate::facts::structural_value_flow(
                views,
                resolved_native_operation_inputs
                    .get(module)
                    .map_or(&[][..], Vec::as_slice),
                stable_set_spreads.get(module).unwrap_or(&no_spreads),
                inferred_set_constructions
                    .get(module)
                    .unwrap_or(&BTreeMap::new()),
            )
        })
    };
    // Argument/entry coverage is structural and independent of the native
    // integrity that it can refine. Author timing assertions cannot supply it.
    for (module, facts) in modules {
        let views = top_level_item_views(facts.body);
        let arguments =
            prepared[module].callback_object_arguments(&views, || structural_flow(module, &views));
        for (export, local) in &facts.exports {
            let Some((start, objects)) = arguments.get(local) else {
                continue;
            };
            let evidence = CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: module.clone(),
                export: export.clone(),
                local: local.clone(),
                source_location: facts.source.as_ref().and_then(|(path, index)| {
                    let (line, column) = index.line_and_column_for_pos(BytePos(*start))?;
                    Some(SourceLocation {
                        source_path: path.clone(),
                        start_line: line,
                        end_line: line,
                        start_column: Some(column),
                    })
                }),
            };
            let mut summary = CallableSummary::callback_factory(evidence.clone());
            summary.callback_object_arguments = Some(CallbackObjectArguments {
                wrapper: evidence,
                object_arguments: objects.clone(),
            });
            export_summaries
                .entry(CallableExportKey {
                    module: module.clone(),
                    export: export.clone(),
                })
                .or_default()
                .add(summary, ProofDependencies::default());
        }
    }
    // This projection is the source-only input to native branch refinement.
    // Generic inferred callback factories are deliberately added after the
    // completed all-module native census and cannot flow back into it.
    let source_only_imports = solve_callable_import_projections(modules, export_summaries.clone());
    for (module, facts) in &mut prepared {
        facts.refine_native_branches(
            source_only_imports
                .get(module.as_str())
                .expect("every prepared module has an import projection"),
        );
        facts.locate_native_writes(module, modules[*module].source.as_ref());
    }
    let mut native_integrity =
        crate::facts::sync_effects::execution_index::resolve_native_integrity(
            prepared.values().map(|facts| facts.native_writes()),
        );
    if !intrinsics_never_written.is_empty() {
        crate::facts::sync_effects::execution_index::apply_intrinsics_never_written(
            &mut native_integrity,
            &intrinsics_never_written,
        )
        .map_err(|refutations| {
            anyhow::anyhow!(
                "program_premises.intrinsics_never_written is refuted by census writes on the exact objects of named families: {}",
                refutations.join("; ")
            )
        })?;
    }
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            if !required.contains(&CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            }) || !modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                continue;
            }
            if signatures.len() != 1 {
                bail!(
                    "descriptor_copy_factory_exports {module}:{export} requires exactly one audited signature"
                );
            }
            for signature in signatures {
                if signature.argument_count != 1
                    || signature.object_argument != 0
                    || signature.eager_own_data_keys.is_empty()
                    || signature.assigned_data_keys.is_empty()
                    || signature
                        .eager_own_data_keys
                        .iter()
                        .any(|key| key == "__proto__")
                    || signature
                        .assigned_data_keys
                        .iter()
                        .any(|key| key == "__proto__")
                {
                    bail!(
                        "descriptor_copy_factory_exports {module}:{export} requires one object argument and nonempty safe key sets"
                    );
                }
                if let Some((write_module, owner)) =
                    prepared.iter().find_map(|(write_module, facts)| {
                        facts
                            .known_descriptor_copy_mutation_owner(&signature.assigned_data_keys)
                            .map(|owner| (write_module, owner))
                    })
                {
                    bail!(
                        "descriptor_copy_factory_exports {module}:{export} is contradicted by required Object slot/prototype write in {write_module}, source unit {}",
                        owner.0
                    );
                }
            }
        }
    }
    // Array-protocol contradictions use a premise-relative copy of the
    // all-source census. It preserves each candidate's Array effect and its
    // conservative fallback, but the selected Array-spread premise may
    // identify the iterator receiver while Object/Symbol remain independent.
    let array_spread_request = selected_native_operation_inputs
        .values()
        .flatten()
        .find_map(|input| {
            (input.evidence.operation
                == NativeInputKind::Operation(
                    spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                ))
            .then_some(input.evidence.spec_location.as_str())
        })
        .or_else(|| {
            resolved_native_operation_inputs
                .values()
                .flatten()
                .find_map(|input| {
                    (input.evidence.operation
                        == NativeInputKind::Operation(
                            spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                        ))
                    .then_some(input.evidence.spec_location.as_str())
                })
        });
    let array_spread_request = array_spread_request.map(str::to_owned).or_else(|| {
        selected_imported_primitive_array_spread_inputs
            .iter()
            .find_map(|(consumer, inputs)| {
                (!inputs.is_empty()).then(|| {
                    format!(
                        "chunk_analysis_options.{consumer}.imported_primitive_array_spread_inputs[0]"
                    )
                })
            })
    });
    if let Some(request) = array_spread_request {
        let protocol_integrity =
            crate::facts::sync_effects::execution_index::resolve_array_spread_protocol_integrity(
                prepared.values().map(|facts| facts.native_writes()),
            );
        if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_array_iterator_protocol_mutation_owner_in(&protocol_integrity)
                .map(|owner| (module, owner))
        }) {
            bail!(
                "{request} is contradicted by a known Array iterator protocol write in provider {module}, source unit {}",
                owner.0
            );
        }
    }
    let inferred: BTreeMap<_, _> = prepared
        .into_iter()
        .map(|(module, prepared)| {
            let views = top_level_item_views(modules[module].body);
            let required_stable_providers = descriptor_copy_asserted
                .get(module)
                .into_iter()
                .flat_map(|exports| exports.keys())
                .filter_map(|export| modules[module].exports.get(export.as_str()))
                .cloned()
                .collect();
            (
                module,
                crate::facts::sync_effects::execution_index::infer_callable_capabilities(
                    &views,
                    prepared,
                    &native_integrity,
                    &required_stable_providers,
                    modules[module].exports.keys().any(|export| {
                        required.contains(&CallableExportKey {
                            module: module.clone(),
                            export: export.clone(),
                        })
                    }),
                    || structural_flow(module, &views),
                ),
            )
        })
        .collect();
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            if !required.contains(&key) {
                continue;
            }
            let Some(local) = modules
                .get(module)
                .and_then(|facts| facts.exports.get(export))
            else {
                continue;
            };
            if !inferred[module].stable_providers.contains(local) {
                bail!(
                    "descriptor_copy_factory_exports {module}:{export} requires a stable, unrebound direct function export"
                );
            }
            export_summaries.entry(key).or_default().add(
                CallableSummary::descriptor_copy_factory(DescriptorCopyFactorySummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    author_location: format!(
                        "chunk_export_purity.{module}.descriptor_copy_factory_exports.{export}"
                    ),
                    signatures: signatures.clone(),
                }),
                ProofDependencies::default(),
            );
        }
    }
    // A construction premise must hold in every analyzed chunk's census,
    // including the importer's.
    let intact_collections = inferred
        .values()
        .map(|capabilities| &capabilities.intact_collections)
        .fold(None::<BTreeSet<_>>, |all, module| {
            Some(match all {
                None => module.clone(),
                Some(all) => all.intersection(module).copied().collect(),
            })
        })
        .unwrap_or_default();
    for (module, facts) in modules {
        let Some(capabilities) = inferred.get(module) else {
            continue;
        };
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            if !required.contains(&key) || export_summaries.contains_key(&key) {
                continue;
            }
            let Some(factory) = capabilities.foreign_calls.get(local).filter(|call| {
                call.callback_factory
                    && call
                        .derived
                        .native_constructions
                        .is_subset(&intact_collections)
            }) else {
                continue;
            };
            export_summaries.entry(key).or_default().add(
                CallableSummary::callback_factory(
                    CallbackFactoryEvidence::InferredImmutableWrapper {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: facts.source.as_ref().and_then(|(path, index)| {
                            let (line, column) = index
                                .line_and_column_for_pos(BytePos(factory.declaration_start))?;
                            Some(SourceLocation {
                                source_path: path.clone(),
                                start_line: line,
                                end_line: line,
                                start_column: Some(column),
                            })
                        }),
                    },
                ),
                factory.dependencies.clone(),
            );
        }
    }
    for (module, facts) in modules {
        let Some(capabilities) = inferred.get(module) else {
            continue;
        };
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            // Publish only after proving availability for the resolved import.
            // Isolated initializer providers stay leaves under every assignment;
            // general initialized bindings still require a use/order proof.
            let Some(call) = capabilities
                .foreign_calls
                .get(local)
                .filter(|call| call.availability.available_to_importer())
            else {
                continue;
            };
            if !required.contains(&key)
                || !call
                    .derived
                    .native_constructions
                    .is_subset(&intact_collections)
            {
                continue;
            }
            let locate = |start| {
                facts.source.as_ref().and_then(|(path, index)| {
                    let (line, column) = index.line_and_column_for_pos(BytePos(start))?;
                    Some(SourceLocation {
                        source_path: path.clone(),
                        start_line: line,
                        end_line: line,
                        start_column: Some(column),
                    })
                })
            };
            if let Some(record) = &call.record {
                export_summaries.entry(key.clone()).or_default().add(
                    CallableSummary::returned_record(ReturnedRecordSummary {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: locate(call.declaration_start),
                        members: record
                            .members
                            .iter()
                            .map(|(name, start, captures)| DeferredRecordMember {
                                name: name.clone(),
                                source_location: locate(*start),
                                captures: captures.clone(),
                            })
                            .collect(),
                        inference: record.inference.clone(),
                        replacement_package: facts.replacement_packages.get(export).cloned(),
                    }),
                    call.dependencies.clone(),
                );
            }
            export_summaries.entry(key).or_default().add(
                CallableSummary::foreign_call(ForeignCallSummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    local: local.clone(),
                    source_location: locate(call.declaration_start),
                    invocation: call.derived.invocation,
                    returns: call.derived.returns.clone(),
                    retained_arguments: call.derived.retained_arguments.clone(),
                    retained_provider_captures: call.derived.retained_provider_captures.clone(),
                    native_constructions: call.derived.native_constructions.clone(),
                    replacement_package: facts.replacement_packages.get(export).cloned(),
                    dependencies: call.dependencies.clone(),
                }),
                call.dependencies.clone(),
            );
        }
    }
    for (module, facts) in modules {
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            if !required.contains(&key) {
                continue;
            }
            let Some(wrapper) = inferred[module].namespace_wrappers.get(local) else {
                continue;
            };
            let source_location = facts.source.as_ref().and_then(|(path, index)| {
                let (line, column) =
                    index.line_and_column_for_pos(BytePos(wrapper.function_start))?;
                Some(SourceLocation {
                    source_path: path.clone(),
                    start_line: line,
                    end_line: line,
                    start_column: Some(column),
                })
            });
            export_summaries.entry(key).or_default().add(
                CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    local: local.clone(),
                    source_location,
                    inference: wrapper.inference.clone(),
                    dependencies: wrapper.dependencies.clone(),
                }),
                wrapper.dependencies.clone(),
            );
        }
    }
    let imported_callables = solve_callable_import_projections(modules, export_summaries);
    let structural_flows = structural_flows
        .into_iter()
        .filter_map(|(module, flow)| Some((module.clone(), flow.into_inner()?)))
        .collect();
    Ok(ResolvedCallableSummaries {
        bundle_globals,
        imported_callables,
        native_integrity: Some(native_integrity),
        value_inputs: resolved_value_inputs,
        native_operation_inputs: resolved_native_operation_inputs,
        imported_primitive_array_spread_inputs: BTreeMap::new(),
        selected_imported_primitive_array_spread_inputs:
            selected_imported_primitive_array_spread_inputs.clone(),
        stable_set_spreads,
        inferred_set_constructions,
        argument_inputs: argument_inputs.clone(),
        structural_flows,
        known_original_external_writes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{ProofDependencies, ValueInputRef};
    use swc_common::{FileName, Span, sync::Lrc};
    use swc_ecma_ast::{Expr, Module, NewExpr};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};
    use swc_ecma_visit::{Visit, VisitWith};

    fn parse(source: &str) -> Module {
        let cm: Lrc<swc_common::SourceMap> = Default::default();
        let fm = cm.new_source_file(FileName::Custom("m.js".into()).into(), source.to_string());
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer).parse_module().unwrap()
    }

    #[test]
    fn imported_primitive_value_closure_retains_all_export_cells() {
        let provider_cell = PrimitiveExportCell {
            module: "provider".to_string(),
            export: "limit".to_string(),
        };
        let bridge_cell = PrimitiveExportCell {
            module: "bridge".to_string(),
            export: "copied".to_string(),
        };
        let app_cell = PrimitiveExportCell {
            module: "app".to_string(),
            export: "forwarded".to_string(),
        };
        let sources = BTreeMap::from([
            (
                ("provider".to_string(), "limit".to_string()),
                ExportValueSource {
                    kinds: PrimKinds::NUMBER,
                    copies: Vec::new(),
                    cell: Some(provider_cell.clone()),
                },
            ),
            (
                ("bridge".to_string(), "forwarded".to_string()),
                ExportValueSource {
                    kinds: PrimKinds::NONE,
                    copies: vec![ResolvedImport {
                        module: "provider".to_string(),
                        export: "limit".to_string(),
                        source_specifier: "provider".to_string(),
                    }],
                    cell: None,
                },
            ),
            (
                ("bridge".to_string(), "copied".to_string()),
                ExportValueSource {
                    kinds: PrimKinds::NONE,
                    copies: vec![ResolvedImport {
                        module: "provider".to_string(),
                        export: "limit".to_string(),
                        source_specifier: "provider".to_string(),
                    }],
                    cell: Some(bridge_cell.clone()),
                },
            ),
            (
                ("app".to_string(), "forwarded".to_string()),
                ExportValueSource {
                    kinds: PrimKinds::NONE,
                    copies: vec![ResolvedImport {
                        module: "bridge".to_string(),
                        export: "copied".to_string(),
                        source_specifier: "bridge".to_string(),
                    }],
                    cell: Some(app_cell.clone()),
                },
            ),
        ]);

        let resolved = resolve_export_primitives(sources.clone());
        assert_eq!(
            resolved[&("bridge".to_string(), "forwarded".to_string())].kinds,
            PrimKinds::NUMBER
        );
        assert_eq!(
            resolved[&("bridge".to_string(), "forwarded".to_string())].source_cells,
            BTreeSet::from([provider_cell.clone()]),
        );
        assert_eq!(
            resolved[&("bridge".to_string(), "copied".to_string())].source_cells,
            BTreeSet::from([provider_cell.clone(), bridge_cell.clone()]),
        );
        assert_eq!(
            resolved[&("app".to_string(), "forwarded".to_string())].source_cells,
            BTreeSet::from([provider_cell, bridge_cell, app_cell]),
        );
    }

    fn facts<'a>(
        module: &'a Module,
        imports: &[(&str, &str, &str)],
        exports: &[(&str, &str)],
    ) -> ModulePurityFacts<'a> {
        ModulePurityFacts {
            body: &module.body,
            imports: imports
                .iter()
                .map(|(local, source, export)| {
                    (
                        (*local).to_string(),
                        ResolvedImport {
                            module: (*source).to_string(),
                            export: (*export).to_string(),
                            source_specifier: (*source).to_string(),
                        },
                    )
                })
                .collect(),
            exports: exports
                .iter()
                .map(|(name, local)| ((*name).to_string(), (*local).to_string()))
                .collect(),
            source: None,
            replacement_packages: BTreeMap::new(),
        }
    }

    fn function_purity_with_imports(
        source: &str,
        local: &str,
        imported: &BTreeMap<String, Arc<Purity>>,
    ) -> Purity {
        let module = parse(source);
        let facts = facts(&module, &[], &[("out", local)]);
        let prepared = facts.prepare(
            &crate::analysis_inputs::AnalysisInputs::default(),
            None,
            PreparationOptions::default(),
        );
        facts
            .build_graph(&prepared, imported)
            .function_purity(local)
            .expect("function is indexed")
            .as_ref()
            .clone()
    }

    fn value_input_dependencies(provider: &str, input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: provider.to_string(),
                input_index,
            }]),
            ..Default::default()
        }
    }

    #[test]
    fn callable_import_projection_preserves_typed_judgment_dependencies() {
        let app = parse("import { build } from 'provider';");
        let provider = parse("export function build() { return {}; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("build", "provider", "build")], &[]),
            ),
            (
                "provider".to_string(),
                facts(&provider, &[], &[("build", "build")]),
            ),
        ]);
        let reported_dependencies = ValueInputRef {
            defining_chunk: "report-only.js".to_string(),
            input_index: 99,
        };
        let summary = CallableSummary::returned_record(ReturnedRecordSummary {
            defining_module: "provider".to_string(),
            export: "build".to_string(),
            local: "build".to_string(),
            source_location: None,
            members: Vec::new(),
            inference: crate::analysis_provenance::InferenceUse {
                conclusion: crate::analysis_provenance::InferenceRule::ReturnedRecord,
                derivation_rules: BTreeSet::from([
                    crate::analysis_provenance::InferenceRule::ReturnedRecord,
                ]),
                value_input_refs: BTreeSet::from([reported_dependencies]),
                descriptor_copy_author_refs: Default::default(),
                argument_input_refs: BTreeSet::new(),
                native_operation_input_refs: BTreeSet::new(),
                imported_primitive_array_spread_input_refs: BTreeSet::new(),
            },
            replacement_package: None,
        });
        let trusted_dependencies = value_input_dependencies("provider-values.js", 0);
        let mut export = CallableExportProof::default();
        export.add(summary.clone(), trusted_dependencies.clone());
        let exports = BTreeMap::from([(
            CallableExportKey {
                module: "provider".to_string(),
                export: "build".to_string(),
            },
            export,
        )]);
        let rules = CallableProjectionRules::new(exports, &modules);
        let producers = rules.producers();
        let solved = IterativeInference::new(rules).solve(producers);

        let import_key = CallableFactKey::Import(CallableImportKey {
            module: "app".to_string(),
            local: "build".to_string(),
        });
        let judgment = solved
            .get(&import_key)
            .expect("provider summary projects onto the resolved importer");
        assert_eq!(judgment.value(), &summary);
        assert_eq!(
            judgment.dependencies(),
            &trusted_dependencies,
            "typed proof dependencies flow through the import; report references are projection-only"
        );
    }

    #[test]
    fn impurity_propagates_back_across_a_module_chain() {
        // c.base (impure) demotes b.wrap, which demotes a's `wrap` import.
        let a = parse("const W = wrap();");
        let b = parse("import { base } from \"c\";\nexport function wrap() { return base(); }");
        let c = parse("export function base() { globalSink(); }");
        let modules = BTreeMap::from([
            ("a".to_string(), facts(&a, &[("wrap", "b", "wrap")], &[])),
            (
                "b".to_string(),
                facts(&b, &[("base", "c", "base")], &[("wrap", "wrap")]),
            ),
            ("c".to_string(), facts(&c, &[], &[("base", "base")])),
        ]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();
        assert!(!resolved["a"]["wrap"].is_pure());
    }

    #[test]
    fn mutually_recursive_imported_functions_close_without_optimistic_seeds() {
        let a = parse("import { right } from 'b'; export function left() { return right(); }");
        let b = parse("import { left } from 'a'; export function right() { return left(); }");
        let modules = BTreeMap::from([
            (
                "a".to_string(),
                facts(&a, &[("right", "b", "right")], &[("left", "left")]),
            ),
            (
                "b".to_string(),
                facts(&b, &[("left", "a", "left")], &[("right", "right")]),
            ),
        ]);

        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();

        assert!(resolved["a"]["right"].is_pure());
        assert!(resolved["b"]["left"].is_pure());
    }

    #[test]
    fn impurity_in_mutually_recursive_imports_has_finite_cause_chains() {
        let a = parse("import { right } from 'b'; export function left() { return right(); }");
        let b =
            parse("import { left } from 'a'; export function right() { left(); globalSink(); }");
        let modules = BTreeMap::from([
            (
                "a".to_string(),
                facts(&a, &[("right", "b", "right")], &[("left", "left")]),
            ),
            (
                "b".to_string(),
                facts(&b, &[("left", "a", "left")], &[("right", "right")]),
            ),
        ]);

        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();

        let right = &resolved["a"]["right"];
        let left = &resolved["b"]["left"];
        assert!(!right.is_pure());
        assert!(!left.is_pure());
        assert_finite_causes(right);
        assert_finite_causes(left);
    }

    fn assert_finite_causes(root: &Arc<Purity>) {
        fn visit(
            purity: &Arc<Purity>,
            active: &mut BTreeSet<usize>,
            completed: &mut BTreeSet<usize>,
        ) {
            let identity = Arc::as_ptr(purity) as usize;
            assert!(
                !active.contains(&identity),
                "purity cause graph contains a cycle"
            );
            if !completed.insert(identity) {
                return;
            }
            active.insert(identity);
            if let Purity::NotPure { reasons } = purity.as_ref() {
                for reason in reasons {
                    if let Some(cause) = &reason.cause {
                        visit(cause, active, completed);
                    }
                }
            }
            active.remove(&identity);
        }

        visit(root, &mut BTreeSet::new(), &mut BTreeSet::new());
    }

    #[test]
    fn import_of_an_unanalyzed_module_stays_unknown() {
        // `vendor` is not in the module set (external/opaque) → no entry.
        let app = parse("const C = ext(x);");
        let modules = BTreeMap::from([(
            "app".to_string(),
            facts(&app, &[("ext", "vendor", "ext")], &[]),
        )]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();
        assert!(!resolved["app"].contains_key("ext"));
    }

    #[test]
    fn import_of_a_non_function_export_stays_unknown() {
        // A data export is not callable → no purity entry, stays unknown_call.
        let app = parse("const C = table(x);");
        let lib = parse("export const table = 42;");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("table", "lib", "table")], &[]),
            ),
            ("lib".to_string(), facts(&lib, &[], &[("table", "table")])),
        ]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();
        assert!(!resolved["app"].contains_key("table"));
    }

    #[test]
    fn cross_module_graph_preparation_gates_mutated_set_iterable_proof() {
        let module = parse(
            "const values = [1]; Set.prototype.add = function add(value) {}; new Set(values);",
        );
        let prepared = facts(&module, &[], &[]).prepare(
            &crate::analysis_inputs::AnalysisInputs::default(),
            None,
            PreparationOptions::default(),
        );

        struct SetConstructors(Vec<Span>);
        impl Visit for SetConstructors {
            fn visit_new_expr(&mut self, node: &NewExpr) {
                if matches!(node.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "Set")
                {
                    self.0.push(node.span);
                }
                node.visit_children_with(self);
            }
        }
        let mut constructors = SetConstructors(Vec::new());
        module.visit_with(&mut constructors);
        assert_eq!(constructors.0.len(), 1, "fixture needs one Set constructor");
        assert!(
            prepared
                .set_iterable_proof
                .dependencies_for_constructor(constructors.0[0])
                .is_none(),
            "cross-module purity preparation must honor the canonical intrinsic census"
        );
    }

    #[test]
    fn asserted_pure_export_overrides_inferred_impurity() {
        // `observer` is genuinely impure to the classifier (writes a global),
        // so inference demotes it. An author assertion pins it Pure, and the
        // verdict propagates to the importing chunk.
        let app = parse("const C = observer(x);");
        let lib = parse("export function observer(c) { globalThis.__warned = 1; return c; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("observer", "lib", "observer")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("observer", "observer")]),
            ),
        ]);
        // Without the assertion: impure.
        assert!(
            !resolve_imported_purities(
                &modules,
                &BTreeMap::new(),
                &mut ResolvedCallableSummaries::default()
            )
            .unwrap()["app"]["observer"]
                .is_pure()
        );
        // With the assertion on the defining chunk: pure, at every importer.
        let asserted =
            BTreeMap::from([("lib".to_string(), BTreeSet::from(["observer".to_string()]))]);
        let resolved = resolve_imported_purities(
            &modules,
            &asserted,
            &mut ResolvedCallableSummaries::default(),
        )
        .unwrap();
        assert!(resolved["app"]["observer"].is_pure());
        assert_eq!(
            resolved["app"]["observer"].dependencies(),
            Some(&ProofDependencies::default()),
            "the separate pure_exports axiom must not acquire inferred captured-value dependencies"
        );
    }

    #[test]
    fn imported_pure_dependencies_are_retained_by_local_wrappers() {
        let dependencies = value_input_dependencies("static/provider.js", 1);
        let imported = BTreeMap::from([(
            "upstream".to_string(),
            Arc::new(Purity::pure().with_dependencies(&dependencies)),
        )]);
        let wrapper = function_purity_with_imports(
            "export function forward() { return upstream(); }",
            "forward",
            &imported,
        );
        assert_eq!(wrapper.dependencies(), Some(&dependencies));
    }

    #[test]
    fn asserted_pure_export_applies_to_a_non_function_callable() {
        // The export is a re-exported interop value the classifier does not see
        // as a chunk-top function, so inference yields no verdict. The
        // assertion still admits it (author trust), reaching the importer.
        let app = parse("const C = forwardRef(x);");
        let lib = parse("const inner = makeRef();\nexport { inner as forwardRef };");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("forwardRef", "lib", "forwardRef")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("forwardRef", "inner")]),
            ),
        ]);
        assert!(
            !resolve_imported_purities(
                &modules,
                &BTreeMap::new(),
                &mut ResolvedCallableSummaries::default()
            )
            .unwrap()["app"]
                .contains_key("forwardRef")
        );
        let asserted = BTreeMap::from([(
            "lib".to_string(),
            BTreeSet::from(["forwardRef".to_string()]),
        )]);
        assert!(
            resolve_imported_purities(
                &modules,
                &asserted,
                &mut ResolvedCallableSummaries::default()
            )
            .unwrap()["app"]["forwardRef"]
                .is_pure()
        );
    }

    #[test]
    fn callback_factory_summaries_keep_source_and_assertion_provenance() {
        let app = parse(
            "import { wrap as localWrap, audited as localAudited } from 'lib';\n\
             import { wrap as missingWrap } from 'vendor';",
        );
        let lib = parse(
            "export function wrap(callback) { return () => callback(); }\n\
             export const audited = makeOpaque();",
        );
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(
                    &app,
                    &[
                        ("localWrap", "lib", "wrap"),
                        ("localAudited", "lib", "audited"),
                        ("missingWrap", "vendor", "wrap"),
                    ],
                    &[],
                ),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("wrap", "wrap"), ("audited", "audited")]),
            ),
        ]);
        let asserted = BTreeMap::from([
            ("lib".to_string(), BTreeSet::from(["audited".to_string()])),
            ("vendor".to_string(), BTreeSet::from(["wrap".to_string()])),
        ]);
        let resolved = resolve_imported_callable_summaries(
            &modules,
            &asserted,
            &BTreeMap::new(),
            CallableSummaryInputs {
                requested_original_provider_exports: &BTreeSet::new(),
                selected_value_inputs: &BTreeMap::new(),
                selected_native_operation_inputs: &BTreeMap::new(),
                selected_imported_primitive_array_spread_inputs: &BTreeMap::new(),
                argument_inputs: &BTreeMap::new(),
                predefined_globals: spec::PredefinedGlobals::Unknown,
                no_accessor_install_callables: &BTreeMap::new(),
                intrinsics_never_written: BTreeSet::new(),
            },
        )
        .unwrap()
        .imported_callables;
        assert_eq!(
            resolved["app"]["localWrap"].callback_factory_evidence(),
            &BTreeSet::from([CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: "lib".to_string(),
                export: "wrap".to_string(),
                local: "wrap".to_string(),
                source_location: None,
            }]),
        );
        assert_eq!(
            resolved["app"]["localAudited"].callback_factory_evidence(),
            &BTreeSet::from([CallbackFactoryEvidence::AuthorAssertion {
                defining_module: "lib".to_string(),
                export: "audited".to_string(),
            }]),
        );
        assert!(!resolved["app"].contains_key("missingWrap"));
    }
}
