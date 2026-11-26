use analysis_domain::item_views::TopLevelItemView;
use analysis_model::PrimKinds;
use analysis_vocabulary::execution_evidence::SourceLocator;
use analysis_vocabulary::inputs::{
    ImportedPrimitiveArrayOperandEvidence, ImportedPrimitiveArraySpreadInputEvidence,
    ImportedPrimitiveArraySpreadInputRef, ResolvedImportedPrimitiveArraySpreadInput,
};
use analysis_vocabulary::purity::Purity;
use analysis_vocabulary::value::Value;
use anyhow::{Context, Result, bail};
use js_ast::SourceLineIndex;
#[cfg(test)]
use purity::ChunkCodeGraph;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Spanned;
use swc_ecma_ast::{Decl, Expr, Lit, ModuleDecl, ModuleItem, Pat, Stmt};
use value_flow::ChunkValueFlow;
use value_flow::program_bindings::{ModuleBindings, ResolvedImport, SourceModuleKey};

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

pub(super) struct PreparedModulePurityFacts<'a> {
    pub(super) views: Vec<TopLevelItemView<'a>>,
    pub(super) shadowed: BTreeSet<&'static str>,
    pub(super) set_iterable_proof: purity::SetIterableProof,
    pub(super) local_class_construction_proof: purity::LocalClassConstructionProof,
    pub(super) plain_data_bindings: Vec<String>,
    pub(super) plain_data_dependencies:
        BTreeMap<String, analysis_vocabulary::inputs::ProofDependencies>,
    pub(super) plain_data_inference_uses:
        BTreeMap<String, BTreeSet<analysis_vocabulary::provenance::InferenceUse>>,
    pub(super) plain_array_bindings: BTreeSet<String>,
    known_eager_rebindings: BTreeMap<swc_ecma_ast::Id, BTreeSet<u32>>,
    known_lazy_rebindings: BTreeSet<swc_ecma_ast::Id>,
    known_member_write_roots: BTreeMap<swc_ecma_ast::Id, BTreeSet<u32>>,
}

#[derive(Debug, Clone, Copy, Default)]
pub(super) struct PreparationOptions {
    /// Retain exact mutation roots only for chunks named by an imported-array
    /// spread premise. The ordinary purity path does not need these facts.
    pub(super) collect_imported_array_write_sites: bool,
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
    declaration: &analysis_vocabulary::inputs::SourceDeclaration,
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

pub(super) fn resolve_imported_primitive_array_spread_inputs(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &BTreeMap<SourceModuleKey, PreparedModulePurityFacts<'_>>,
    selected: &BTreeMap<
        SourceModuleKey,
        Vec<analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput>,
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
                    provider_source_location: provider_facts
                        .locator()
                        .and_then(|locator| locator.span(producer_array.span)),
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
                consumer_source_location: consumer_facts
                    .locator()
                    .and_then(|locator| locator.span(consumer_array.span)),
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
    pub fn bindings<'b>(&'b self, module: &'b str) -> ModuleBindings<'b> {
        ModuleBindings {
            module,
            imports: &self.imports,
            exports: &self.exports,
        }
    }

    pub(super) fn locator(&self) -> Option<SourceLocator<'_>> {
        self.source
            .as_ref()
            .map(|(source_path, lines)| SourceLocator { source_path, lines })
    }

    /// `structural_flow` is this body's flow under the same inputs, when the
    /// summary pass already solved it.
    pub(super) fn prepare(
        &self,
        inputs: &analysis_vocabulary::inputs::AnalysisInputs,
        structural_flow: Option<Arc<ChunkValueFlow>>,
        options: PreparationOptions,
    ) -> PreparedModulePurityFacts<'a> {
        let source = analysis::facts::collect_chunk_source_facts_from_items(self.body, None);
        let structural = match structural_flow {
            Some(flow) => analysis::facts::complete_chunk_structural_with_flow(
                source,
                &inputs.assumptions.argument_inputs,
                flow,
            ),
            None => analysis::facts::complete_chunk_structural_with_inputs(
                source,
                &inputs.assumptions.native_operation_inputs,
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
        let plain_array_bindings = purity::heap::plain_array_bindings(&views, &shadowed);
        let set_iterable_proof = structural.set_iterable_shapes;
        let local_class_construction_proof = structural.local_class_shapes;
        let prepared_sync_index =
            sync_effects::interpreter::synchronous_calls::prepare_synchronous_call_index_with_source_facts(
                &views,
                &value_flow,
                inputs,
                &plain_array_bindings,
                sync_effects::interpreter::synchronous_calls::PreparedSyncShapeInputs {
                    local_class_construction: &local_class_construction_proof,
                    set_iterable_shapes: set_iterable_proof,
                    stable_array_spread_shapes,
                    shadowed: &shadowed,
                    source_facts: source_semantic_facts.as_ref(),
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
    pub(super) fn build_graph(
        &self,
        prepared: &PreparedModulePurityFacts<'_>,
        imported: &BTreeMap<String, Arc<Purity>>,
    ) -> ChunkCodeGraph {
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
            &BTreeSet::new(),
            self.locator(),
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

    pub(super) fn imported_purities(
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
