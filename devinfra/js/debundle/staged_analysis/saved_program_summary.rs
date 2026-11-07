//! The saved program-summary boundary. Runtime verdicts and fields containing
//! SWC bindings have explicit artifact representations. Other summary fields
//! are borrowed on write and owned on read; Id-free data keeps its existing
//! codec representation.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::saved::{SavedCallableSummary, SavedNativeIntegrity};
use analysis::analysis_inputs::{
    ArgumentInputEvidence, ImportedPrimitiveRead, InferredSetConstruction, InferredSetOperation,
    NativeOperationInputEvidence, PrimitiveCellInvariant, ResolvedArgumentInput,
    ResolvedBindingValueInput, ResolvedNativeOperationInput, StableSetIterableUse,
    ValueInputEvidence,
};
use analysis::facts::abstract_domain::value::Value;
use analysis_codec::{ContextOrdinals, SavedBinding, SavedChunk, SavedModuleCell};
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

use super::program_summary::ProgramSummary;
use analysis::purity::saved::{PurityNodeId, PuritySnapshot, RestoredPurities};

type RetainedCallbackSignatures = BTreeMap<String, Vec<spec::RetainedCallbackSignature>>;

#[derive(Serialize, Deserialize)]
pub(super) struct SavedProgramSummary<'a> {
    bindings: BTreeMap<String, BTreeMap<String, PurityNodeId>>,
    values: Cow<'a, BTreeMap<String, BTreeMap<String, Value>>>,
    primitive_reads: Cow<'a, BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>>,
    members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    fluent: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    constructors: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    imported_callables: BTreeMap<String, BTreeMap<String, SavedCallableSummary<'a>>>,
    module_cells: BTreeMap<String, Vec<SavedModuleCellEntry>>,
    module_cell_invariants: Vec<SavedModuleCellInvariant>,
    source_initialized_module_cells: BTreeMap<String, Vec<SavedModuleCell>>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    native_integrity: Option<SavedNativeIntegrity<'a>>,
    bundle_globals: Cow<'a, analysis::analysis_inputs::BundleGlobalNames>,
    value_inputs: BTreeMap<String, Vec<SavedValueInput>>,
    native_operation_inputs: BTreeMap<String, Vec<SavedNativeOperationInput>>,
    stable_set_spreads: BTreeMap<String, BTreeMap<u32, SavedStableSetIterableUse>>,
    inferred_set_constructions: BTreeMap<String, BTreeMap<u32, SavedInferredSetConstruction>>,
    argument_inputs: BTreeMap<String, Vec<SavedArgumentInput>>,
    inline_callback_factories: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    callback_factory_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    retained_callback_factories: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    retained_callback_factory_members:
        Cow<'a, BTreeMap<String, BTreeMap<String, RetainedCallbackSignatures>>>,
    retained_callback_constructors: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    no_reentry_exports: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    no_reentry_callback_exports: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    no_reentry_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    no_reentry_record_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    namespace_independent_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
}

#[derive(Serialize, Deserialize)]
struct SavedModuleCellEntry {
    binding: SavedBinding,
    cell: SavedModuleCell,
}

#[derive(Serialize, Deserialize)]
struct SavedModuleCellInvariant {
    cell: SavedModuleCell,
    invariant: PrimitiveCellInvariant,
}

impl<'a> SavedProgramSummary<'a> {
    pub(super) fn snapshot(
        summary: &'a ProgramSummary,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        let bindings = summary
            .bindings
            .iter()
            .map(|(chunk, bindings)| {
                (
                    chunk.clone(),
                    bindings
                        .iter()
                        .map(|(name, purity)| (name.clone(), purities.snapshot(purity)))
                        .collect(),
                )
            })
            .collect();
        let value_inputs = summary
            .value_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedValueInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_callables = summary
            .imported_callables
            .iter()
            .map(|(chunk, callables)| {
                Ok((
                    chunk.clone(),
                    callables
                        .iter()
                        .map(|(name, callable)| {
                            Ok((
                                name.clone(),
                                SavedCallableSummary::snapshot(callable, ordinals)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cells = summary
            .module_cells
            .iter()
            .map(|(chunk, cells)| {
                ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
                Ok((
                    chunk.clone(),
                    cells
                        .iter()
                        .map(|(binding, cell)| {
                            Ok(SavedModuleCellEntry {
                                binding: ordinals.snapshot_binding(binding)?,
                                cell: ordinals.snapshot_module_cell(*cell)?,
                            })
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cell_invariants = summary
            .module_cell_invariants
            .iter()
            .map(|(cell, invariant)| {
                Ok(SavedModuleCellInvariant {
                    cell: ordinals.snapshot_module_cell(*cell)?,
                    invariant: invariant.clone(),
                })
            })
            .collect::<Result<_>>()?;
        let source_initialized_module_cells = summary
            .source_initialized_module_cells
            .iter()
            .map(|(importer, cells)| {
                ordinals
                    .restore_chunk(SavedChunk(importer.clone()))
                    .map(|_| ())?;
                Ok((
                    importer.clone(),
                    cells
                        .iter()
                        .map(|cell| ordinals.snapshot_module_cell(*cell))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let native_operation_inputs = summary
            .native_operation_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedNativeOperationInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let stable_set_spreads = summary
            .stable_set_spreads
            .iter()
            .map(|(chunk, spreads)| {
                Ok((
                    chunk.clone(),
                    spreads
                        .iter()
                        .map(|(site, spread)| {
                            Ok((
                                *site,
                                SavedStableSetIterableUse::snapshot(spread, ordinals)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let inferred_set_constructions = summary
            .inferred_set_constructions
            .iter()
            .map(|(chunk, constructions)| {
                Ok((
                    chunk.clone(),
                    constructions
                        .iter()
                        .map(|(site, construction)| {
                            Ok((
                                *site,
                                SavedInferredSetConstruction::snapshot(construction, ordinals)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let argument_inputs = summary
            .argument_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedArgumentInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        Ok(Self {
            bindings,
            value_inputs,
            values: Cow::Borrowed(&summary.values),
            primitive_reads: Cow::Borrowed(&summary.primitive_reads),
            members: Cow::Borrowed(&summary.members),
            fluent: Cow::Borrowed(&summary.fluent),
            constructors: Cow::Borrowed(&summary.constructors),
            imported_callables,
            module_cells,
            module_cell_invariants,
            source_initialized_module_cells,
            native_integrity: summary
                .native_integrity
                .as_ref()
                .map(SavedNativeIntegrity::snapshot),
            bundle_globals: Cow::Borrowed(&summary.bundle_globals),
            native_operation_inputs,
            stable_set_spreads,
            inferred_set_constructions,
            argument_inputs,
            inline_callback_factories: Cow::Borrowed(&summary.inline_callback_factories),
            callback_factory_members: Cow::Borrowed(&summary.callback_factory_members),
            retained_callback_factories: Cow::Borrowed(&summary.retained_callback_factories),
            retained_callback_factory_members: Cow::Borrowed(
                &summary.retained_callback_factory_members,
            ),
            retained_callback_constructors: Cow::Borrowed(&summary.retained_callback_constructors),
            no_reentry_exports: Cow::Borrowed(&summary.no_reentry_exports),
            no_reentry_callback_exports: Cow::Borrowed(&summary.no_reentry_callback_exports),
            no_reentry_members: Cow::Borrowed(&summary.no_reentry_members),
            no_reentry_record_members: Cow::Borrowed(&summary.no_reentry_record_members),
            namespace_independent_members: Cow::Borrowed(&summary.namespace_independent_members),
        })
    }

    pub(super) fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
    ) -> Result<ProgramSummary> {
        let bindings = self
            .bindings
            .into_iter()
            .map(|(chunk, bindings)| {
                Ok((
                    chunk,
                    bindings
                        .into_iter()
                        .map(|(name, id)| Ok((name, purities.get(id)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let value_inputs = self
            .value_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_callables = self
            .imported_callables
            .into_iter()
            .map(|(chunk, callables)| {
                Ok((
                    chunk,
                    callables
                        .into_iter()
                        .map(|(name, callable)| Ok((name, callable.restore(ordinals)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cells = self
            .module_cells
            .into_iter()
            .map(|(chunk, entries)| {
                ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
                let mut cells = BTreeMap::new();
                for entry in entries {
                    let binding = ordinals.restore_binding(entry.binding)?;
                    let cell = ordinals.restore_module_cell(entry.cell)?;
                    if cells.insert(binding, cell).is_some() {
                        bail!("duplicate saved module-cell binding in chunk {chunk}");
                    }
                }
                Ok((chunk, cells))
            })
            .collect::<Result<_>>()?;
        let mut module_cell_invariants = BTreeMap::new();
        for entry in self.module_cell_invariants {
            let cell = ordinals.restore_module_cell(entry.cell)?;
            if module_cell_invariants
                .insert(cell, entry.invariant)
                .is_some()
            {
                bail!("duplicate saved module-cell invariant for {cell:?}");
            }
        }
        let source_initialized_module_cells = self
            .source_initialized_module_cells
            .into_iter()
            .map(|(importer, saved_cells)| {
                ordinals.restore_chunk(SavedChunk(importer.clone()))?;
                let mut cells = BTreeSet::new();
                for saved_cell in saved_cells {
                    let cell = ordinals.restore_module_cell(saved_cell)?;
                    if !cells.insert(cell) {
                        bail!(
                            "duplicate saved source-initialized module cell {cell:?} for importer {importer}"
                        );
                    }
                }
                Ok((importer, cells))
            })
            .collect::<Result<_>>()?;
        let native_operation_inputs = self
            .native_operation_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let stable_set_spreads = self
            .stable_set_spreads
            .into_iter()
            .map(|(chunk, spreads)| {
                Ok((
                    chunk,
                    spreads
                        .into_iter()
                        .map(|(site, spread)| Ok((site, spread.restore(ordinals)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let inferred_set_constructions = self
            .inferred_set_constructions
            .into_iter()
            .map(|(chunk, constructions)| {
                Ok((
                    chunk,
                    constructions
                        .into_iter()
                        .map(|(site, construction)| Ok((site, construction.restore(ordinals)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let argument_inputs = self
            .argument_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        Ok(ProgramSummary {
            bindings,
            value_inputs,
            values: self.values.into_owned(),
            primitive_reads: self.primitive_reads.into_owned(),
            members: self.members.into_owned(),
            fluent: self.fluent.into_owned(),
            constructors: self.constructors.into_owned(),
            imported_callables,
            module_cells,
            module_cell_invariants,
            source_initialized_module_cells,
            native_integrity: self
                .native_integrity
                .map(SavedNativeIntegrity::restore)
                .transpose()?,
            bundle_globals: self.bundle_globals.into_owned(),
            native_operation_inputs,
            stable_set_spreads,
            inferred_set_constructions,
            argument_inputs,
            inline_callback_factories: self.inline_callback_factories.into_owned(),
            callback_factory_members: self.callback_factory_members.into_owned(),
            retained_callback_factories: self.retained_callback_factories.into_owned(),
            retained_callback_factory_members: self.retained_callback_factory_members.into_owned(),
            retained_callback_constructors: self.retained_callback_constructors.into_owned(),
            no_reentry_exports: self.no_reentry_exports.into_owned(),
            no_reentry_callback_exports: self.no_reentry_callback_exports.into_owned(),
            no_reentry_members: self.no_reentry_members.into_owned(),
            no_reentry_record_members: self.no_reentry_record_members.into_owned(),
            namespace_independent_members: self.namespace_independent_members.into_owned(),
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedValueInput {
    evidence: ValueInputEvidence,
    binding: SavedBinding,
    declaration_span: Span,
    initializer_span: Span,
    initializer_ordinal: SourceUnitOrdinal,
}

impl SavedValueInput {
    fn snapshot(input: &ResolvedBindingValueInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            declaration_span: input.declaration_span,
            initializer_span: input.initializer_span,
            initializer_ordinal: input.initializer_ordinal,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedBindingValueInput> {
        Ok(ResolvedBindingValueInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            declaration_span: self.declaration_span,
            initializer_span: self.initializer_span,
            initializer_ordinal: self.initializer_ordinal,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedArgumentInput {
    evidence: ArgumentInputEvidence,
    binding: SavedBinding,
    function_start: u32,
}

impl SavedArgumentInput {
    fn snapshot(input: &ResolvedArgumentInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            function_start: input.function_start,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedArgumentInput> {
        Ok(ResolvedArgumentInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            function_start: self.function_start,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedNativeOperationInput {
    evidence: NativeOperationInputEvidence,
    binding: SavedBinding,
    selected_statement_span: Span,
    declaration_span: Span,
    initializer_span: Span,
    call_span: Span,
    initializer_ordinal: SourceUnitOrdinal,
    spread_source: Option<SavedBinding>,
    spread_source_span: Option<Span>,
    spread_token_span: Option<Span>,
}

impl SavedNativeOperationInput {
    fn snapshot(input: &ResolvedNativeOperationInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            selected_statement_span: input.selected_statement_span,
            declaration_span: input.declaration_span,
            initializer_span: input.initializer_span,
            call_span: input.call_span,
            initializer_ordinal: input.initializer_ordinal,
            spread_source: input
                .spread_source
                .as_ref()
                .map(|binding| ordinals.snapshot_binding(binding))
                .transpose()?,
            spread_source_span: input.spread_source_span,
            spread_token_span: input.spread_token_span,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedNativeOperationInput> {
        Ok(ResolvedNativeOperationInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            selected_statement_span: self.selected_statement_span,
            declaration_span: self.declaration_span,
            initializer_span: self.initializer_span,
            call_span: self.call_span,
            initializer_ordinal: self.initializer_ordinal,
            spread_source: self
                .spread_source
                .map(|binding| ordinals.restore_binding(binding))
                .transpose()?,
            spread_source_span: self.spread_source_span,
            spread_token_span: self.spread_token_span,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedStableSetIterableUse {
    source_span: Span,
    spread_token_span: Span,
    interval_bindings: Vec<SavedBinding>,
    dependencies: analysis::analysis_inputs::ProofDependencies,
}

impl SavedStableSetIterableUse {
    fn snapshot(use_fact: &StableSetIterableUse, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            source_span: use_fact.source_span,
            spread_token_span: use_fact.spread_token_span,
            interval_bindings: use_fact
                .interval_bindings
                .iter()
                .map(|binding| ordinals.snapshot_binding(binding))
                .collect::<Result<_>>()?,
            dependencies: use_fact.dependencies.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<StableSetIterableUse> {
        Ok(StableSetIterableUse {
            source_span: self.source_span,
            spread_token_span: self.spread_token_span,
            interval_bindings: self
                .interval_bindings
                .into_iter()
                .map(|binding| ordinals.restore_binding(binding))
                .collect::<Result<_>>()?,
            dependencies: self.dependencies,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedInferredSetConstruction {
    binding: SavedBinding,
    operation: SavedInferredSetOperation,
    dependencies: analysis::analysis_inputs::ProofDependencies,
    inference: analysis::analysis_provenance::InferenceUse,
}

impl SavedInferredSetConstruction {
    fn snapshot(
        construction: &InferredSetConstruction,
        ordinals: &ContextOrdinals,
    ) -> Result<Self> {
        Ok(Self {
            binding: ordinals.snapshot_binding(&construction.binding)?,
            operation: SavedInferredSetOperation::snapshot(&construction.operation, ordinals)?,
            dependencies: construction.dependencies.clone(),
            inference: construction.inference.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<InferredSetConstruction> {
        Ok(InferredSetConstruction {
            binding: ordinals.restore_binding(self.binding)?,
            operation: self.operation.restore(ordinals)?,
            dependencies: self.dependencies,
            inference: self.inference,
        })
    }
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum SavedInferredSetOperation {
    PrimitiveArray,
    ProtectedSpread {
        source: SavedBinding,
        interval: Box<SavedStableSetIterableUse>,
    },
}

impl SavedInferredSetOperation {
    fn snapshot(operation: &InferredSetOperation, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(match operation {
            InferredSetOperation::PrimitiveArray => Self::PrimitiveArray,
            InferredSetOperation::ProtectedSpread { source, interval } => Self::ProtectedSpread {
                source: ordinals.snapshot_binding(source)?,
                interval: Box::new(SavedStableSetIterableUse::snapshot(interval, ordinals)?),
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<InferredSetOperation> {
        Ok(match self {
            Self::PrimitiveArray => InferredSetOperation::PrimitiveArray,
            Self::ProtectedSpread { source, interval } => InferredSetOperation::ProtectedSpread {
                source: ordinals.restore_binding(source)?,
                interval: Box::new(interval.restore(ordinals)?),
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use source_identity::{ChunkTable, ModuleCellId, SourceUnitOrdinal};
    use swc_common::{DUMMY_SP, SyntaxContext};

    #[test]
    fn required_saved_summary_fields_cannot_be_omitted() {
        let primitive_reads = BTreeMap::from([(
            "app".to_string(),
            BTreeMap::from([(
                "limit".to_string(),
                ImportedPrimitiveRead {
                    kinds: analysis_model::PrimKinds::NUMBER,
                    provider_module: "provider".to_string(),
                    dependency_closure: BTreeSet::from(["provider".to_string()]),
                    source_cells: BTreeSet::from([
                        analysis::analysis_inputs::PrimitiveExportCell {
                            module: "provider".to_string(),
                            export: "limit".to_string(),
                        },
                    ]),
                },
            )]),
        )]);
        let binding: swc_ecma_ast::Id = ("effect".into(), SyntaxContext::empty());
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("provider".to_owned());
        writer_chunks.intern("app".to_owned());
        writer_chunks.intern("other".to_owned());
        let cell = ModuleCellId {
            chunk: writer_provider,
            declaration_origin: 41,
        };
        let owner = serde_json::to_value(SourceUnitOrdinal(3)).expect("owner serializes");
        let span = serde_json::to_value(DUMMY_SP).expect("span serializes");
        let invariant: PrimitiveCellInvariant = serde_json::from_value(serde_json::json!({
            "declaration": {
                "owner": owner.clone(),
                "activation_callable": null,
                "binding_span": span.clone(),
                "initializer_span": span.clone(),
            },
            "post_init_kinds": serde_json::to_value(analysis_model::PrimKinds::STRING)
                .expect("primitive kinds serialize"),
            "stores": [{
                "owner": owner,
                "activation_callable": 7,
                "span": span,
                "kind": "assignment",
            }],
        }))
        .expect("public primitive cell proof deserializes");
        let module_cells =
            BTreeMap::from([("app".to_string(), BTreeMap::from([(binding.clone(), cell)]))]);
        let source_initialized_module_cells =
            BTreeMap::from([("app".to_string(), BTreeSet::from([cell]))]);
        let summary = ProgramSummary {
            bindings: BTreeMap::new(),
            values: BTreeMap::new(),
            primitive_reads: primitive_reads.clone(),
            members: BTreeMap::new(),
            fluent: BTreeMap::new(),
            constructors: BTreeMap::new(),
            imported_callables: BTreeMap::new(),
            module_cells,
            module_cell_invariants: BTreeMap::from([(cell, invariant.clone())]),
            source_initialized_module_cells,
            native_integrity: None,
            bundle_globals: Default::default(),
            value_inputs: BTreeMap::new(),
            native_operation_inputs: BTreeMap::new(),
            stable_set_spreads: BTreeMap::new(),
            inferred_set_constructions: BTreeMap::new(),
            argument_inputs: BTreeMap::new(),
            inline_callback_factories: BTreeMap::new(),
            callback_factory_members: BTreeMap::new(),
            retained_callback_factories: BTreeMap::new(),
            retained_callback_factory_members: BTreeMap::new(),
            retained_callback_constructors: BTreeMap::new(),
            no_reentry_exports: BTreeMap::new(),
            no_reentry_callback_exports: BTreeMap::new(),
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            namespace_independent_members: BTreeMap::new(),
        };
        let ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let mut purities = PuritySnapshot::default();
        let saved = SavedProgramSummary::snapshot(&summary, &ordinals, &mut purities)
            .expect("empty program summary snapshots");
        let saved_json = serde_json::to_value(&saved).expect("saved summary serializes");
        assert!(saved_json["native_integrity"].is_null());
        assert_eq!(
            saved_json["module_cells"]["app"][0]["binding"]["symbol"],
            "effect"
        );
        assert_eq!(
            saved_json["module_cells"]["app"][0]["cell"]["chunk"],
            "provider"
        );

        for field in [
            "native_integrity",
            "primitive_reads",
            "module_cells",
            "module_cell_invariants",
            "source_initialized_module_cells",
        ] {
            let mut missing_field = saved_json.clone();
            missing_field
                .as_object_mut()
                .expect("saved summary is a map")
                .remove(field);
            assert!(
                serde_json::from_value::<SavedProgramSummary<'static>>(missing_field).is_err(),
                "omitted {field} must not restore as empty proof metadata"
            );
        }

        let decoded: SavedProgramSummary<'static> = serde_json::from_value(saved_json.clone())
            .expect("explicit None native census decodes");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("other".to_owned());
        reader_chunks.intern("app".to_owned());
        let reader_provider = reader_chunks.intern("provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let restored = decoded
            .restore(&reader_ordinals, &restored_purities)
            .expect("absent native census restores");
        assert!(restored.native_integrity.is_none());
        assert_eq!(restored.primitive_reads, primitive_reads);
        assert_eq!(
            restored.source_initialized_module_cells,
            BTreeMap::from([(
                "app".to_string(),
                BTreeSet::from([ModuleCellId {
                    chunk: reader_provider,
                    declaration_origin: 41,
                }]),
            )]),
        );
        assert_eq!(
            restored.module_cell_invariants,
            BTreeMap::from([(
                ModuleCellId {
                    chunk: reader_provider,
                    declaration_origin: 41,
                },
                invariant.clone(),
            )]),
        );
        assert_eq!(
            restored.module_cells,
            BTreeMap::from([(
                "app".to_string(),
                BTreeMap::from([(
                    binding,
                    ModuleCellId {
                        chunk: reader_provider,
                        declaration_origin: 41,
                    },
                )]),
            )]),
        );

        let mut duplicate_proof = saved_json.clone();
        let proofs = duplicate_proof["module_cell_invariants"]
            .as_array_mut()
            .expect("module-cell invariants are a sequence");
        let duplicate_entry = proofs[0].clone();
        proofs.push(duplicate_entry);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(duplicate_proof)
            .expect("duplicate proof cell decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities)
                .is_err(),
            "duplicate module-cell proof keys must refuse restore"
        );

        let mut unknown_importer = saved_json.clone();
        let source_initialized = unknown_importer["source_initialized_module_cells"]
            .as_object_mut()
            .expect("source initialized cells are a map");
        let app_cells = source_initialized
            .remove("app")
            .expect("app context exists");
        source_initialized.insert("missing-importer".to_owned(), app_cells);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(unknown_importer)
            .expect("unknown importer decodes structurally");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities)
                .is_err(),
            "unknown importer names must refuse source-initialization restore"
        );
    }
}
