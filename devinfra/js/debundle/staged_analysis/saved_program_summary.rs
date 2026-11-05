//! The saved program-summary boundary. Runtime verdicts and resolved value
//! bindings have explicit artifact representations. The other summary fields
//! are borrowed on write and owned on read; their existing codec behavior is
//! retained until their own artifact slices migrate them.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::saved::{SavedCallableSummary, SavedNativeIntegrity};
use analysis::analysis_inputs::{
    ResolvedArgumentInput, ResolvedBindingValueInput, ResolvedNativeOperationInput,
    ValueInputEvidence,
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
    members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    fluent: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    constructors: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    imported_callables: BTreeMap<String, BTreeMap<String, SavedCallableSummary<'a>>>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    native_integrity: Option<SavedNativeIntegrity<'a>>,
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
        Ok(Self {
            bindings,
            value_inputs,
            values: Cow::Borrowed(&summary.values),
            members: Cow::Borrowed(&summary.members),
            fluent: Cow::Borrowed(&summary.fluent),
            constructors: Cow::Borrowed(&summary.constructors),
            imported_callables,
            native_integrity: summary
                .native_integrity
                .as_ref()
                .map(SavedNativeIntegrity::snapshot),
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
        Ok(ProgramSummary {
            bindings,
            value_inputs,
            values: self.values.into_owned(),
            members: self.members.into_owned(),
            fluent: self.fluent.into_owned(),
            constructors: self.constructors.into_owned(),
            imported_callables,
            native_integrity: self
                .native_integrity
                .map(SavedNativeIntegrity::restore)
                .transpose()?,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_census_field_requires_explicit_none_in_ordinary_serde() {
        let summary = ProgramSummary {
            bindings: BTreeMap::new(),
            values: BTreeMap::new(),
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

        let mut missing_field = saved_json.clone();
        missing_field
            .as_object_mut()
            .expect("saved summary is a map")
            .remove("native_integrity");
        assert!(serde_json::from_value::<SavedProgramSummary<'static>>(missing_field).is_err());

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
    }
}
