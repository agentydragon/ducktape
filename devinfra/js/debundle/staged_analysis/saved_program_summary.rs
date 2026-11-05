//! The saved program-summary boundary. Runtime verdicts and resolved value
//! bindings have explicit artifact representations. The other summary fields
//! are borrowed on write and owned on read; their existing codec behavior is
//! retained until their own artifact slices migrate them.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::{
    CallableSummary, NativeIntegrity, ResolvedArgumentInput, ResolvedBindingValueInput,
    ResolvedNativeOperationInput, ValueInputEvidence,
};
use analysis::facts::abstract_domain::value::Value;
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

use super::program_summary::ProgramSummary;
use super::saved_purity::{PurityNodeId, PuritySnapshot, SavedPurityGraph};

#[derive(Serialize, Deserialize)]
pub(super) struct SavedProgramSummary<'a> {
    purity_graph: SavedPurityGraph,
    bindings: BTreeMap<String, BTreeMap<String, PurityNodeId>>,
    values: Cow<'a, BTreeMap<String, BTreeMap<String, Value>>>,
    members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    fluent: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    constructors: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    imported_callables: Cow<'a, BTreeMap<String, BTreeMap<String, CallableSummary>>>,
    native_integrity: Cow<'a, Option<NativeIntegrity>>,
    bundle_globals: Cow<'a, analysis::analysis_inputs::BundleGlobalNames>,
    value_inputs: BTreeMap<String, Vec<SavedValueInput>>,
    native_operation_inputs: Cow<'a, BTreeMap<String, Vec<ResolvedNativeOperationInput>>>,
    stable_set_spreads:
        Cow<'a, BTreeMap<String, BTreeMap<u32, analysis::analysis_inputs::StableSetIterableUse>>>,
    inferred_set_constructions: Cow<
        'a,
        BTreeMap<String, BTreeMap<u32, analysis::analysis_inputs::InferredSetConstruction>>,
    >,
    argument_inputs: Cow<'a, BTreeMap<String, Vec<ResolvedArgumentInput>>>,
    inline_callback_factories: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    callback_factory_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    retained_callback_factories:
        Cow<'a, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    retained_callback_factory_members: Cow<
        'a,
        BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    >,
    retained_callback_constructors:
        Cow<'a, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    no_reentry_exports: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    no_reentry_callback_exports:
        Cow<'a, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>>,
    no_reentry_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    no_reentry_record_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    namespace_independent_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
}

impl<'a> SavedProgramSummary<'a> {
    pub(super) fn snapshot(
        summary: &'a ProgramSummary,
        ordinals: &ContextOrdinals,
    ) -> Result<Self> {
        let mut purities = PuritySnapshot::default();
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
        Ok(Self {
            purity_graph: purities.finish(),
            bindings,
            value_inputs,
            values: Cow::Borrowed(&summary.values),
            members: Cow::Borrowed(&summary.members),
            fluent: Cow::Borrowed(&summary.fluent),
            constructors: Cow::Borrowed(&summary.constructors),
            imported_callables: Cow::Borrowed(&summary.imported_callables),
            native_integrity: Cow::Borrowed(&summary.native_integrity),
            bundle_globals: Cow::Borrowed(&summary.bundle_globals),
            native_operation_inputs: Cow::Borrowed(&summary.native_operation_inputs),
            stable_set_spreads: Cow::Borrowed(&summary.stable_set_spreads),
            inferred_set_constructions: Cow::Borrowed(&summary.inferred_set_constructions),
            argument_inputs: Cow::Borrowed(&summary.argument_inputs),
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

    pub(super) fn restore(self, ordinals: &ContextOrdinals) -> Result<ProgramSummary> {
        let purities = self.purity_graph.restore()?;
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
        Ok(ProgramSummary {
            bindings,
            value_inputs,
            values: self.values.into_owned(),
            members: self.members.into_owned(),
            fluent: self.fluent.into_owned(),
            constructors: self.constructors.into_owned(),
            imported_callables: self.imported_callables.into_owned(),
            native_integrity: self.native_integrity.into_owned(),
            bundle_globals: self.bundle_globals.into_owned(),
            native_operation_inputs: self.native_operation_inputs.into_owned(),
            stable_set_spreads: self.stable_set_spreads.into_owned(),
            inferred_set_constructions: self.inferred_set_constructions.into_owned(),
            argument_inputs: self.argument_inputs.into_owned(),
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
