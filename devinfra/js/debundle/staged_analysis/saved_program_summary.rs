//! The saved program-summary boundary. Runtime verdicts and fields containing
//! SWC bindings have explicit artifact representations. Other summary fields
//! are borrowed on write and owned on read; Id-free data keeps its existing
//! codec representation.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::saved::{SavedCallableSummary, SavedNativeIntegrity};
use analysis::analysis_inputs::{
    ArgumentInputEvidence, ImportedPrimitiveRead, InferredSetConstruction, InferredSetOperation,
    NativeOperationInputEvidence, ResolvedArgumentInput, ResolvedBindingValueInput,
    ResolvedNativeOperationInput, StableSetIterableUse, ValueInputEvidence,
};
use analysis::facts::abstract_domain::value::Value;
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::Result;
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
                (
                    chunk.clone(),
                    callables
                        .iter()
                        .map(|(name, callable)| {
                            (name.clone(), SavedCallableSummary::snapshot(callable))
                        })
                        .collect(),
                )
            })
            .collect();
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
                        .map(|(name, callable)| Ok((name, callable.restore()?)))
                        .collect::<Result<_>>()?,
                ))
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
        let summary = ProgramSummary {
            bindings: BTreeMap::new(),
            values: BTreeMap::new(),
            primitive_reads: primitive_reads.clone(),
            members: BTreeMap::new(),
            fluent: BTreeMap::new(),
            constructors: BTreeMap::new(),
            imported_callables: BTreeMap::new(),
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
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let mut purities = PuritySnapshot::default();
        let saved = SavedProgramSummary::snapshot(&summary, &ordinals, &mut purities)
            .expect("empty program summary snapshots");
        let saved_json = serde_json::to_value(&saved).expect("saved summary serializes");
        assert!(saved_json["native_integrity"].is_null());

        for field in ["native_integrity", "primitive_reads"] {
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

        let decoded: SavedProgramSummary<'static> =
            serde_json::from_value(saved_json).expect("explicit None native census decodes");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let restored = decoded
            .restore(&ordinals, &restored_purities)
            .expect("absent native census restores");
        assert!(restored.native_integrity.is_none());
        assert_eq!(restored.primitive_reads, primitive_reads);
    }
}
