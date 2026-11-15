//! Saved per-chunk analysis. Snapshot into generated protobuf while preserving
//! explicit binding, effect-cell, and shared-purity conversions.

use std::borrow::Cow;
use std::collections::BTreeMap;

use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::{Result, ensure};
use source_identity::SourceUnitOrdinal;

use super::statement_facts::{
    ConstructorFacts, FallbackOnlyEffects, HeapContentAccesses, HeapContentCells, SynchronousCalls,
};
use super::*;
use crate::execution_evidence::{AssumedEffectReason, ExecutionPrecisionLoss};
use crate::facts::sync_effects::SavedBindingEffectCoverage;
use crate::purity::saved::{PuritySnapshot, RestoredPurities};
use crate::reports::schema::{
    LocalRecordFactoryRejection, LocalRecordFactoryUse, NativeOperationInputUse,
    ReentryFreeOperationUse,
};
use crate::saved_proto::{ProtoConvert, proto, required};

fn snapshot_bindings(
    bindings: &BTreeSet<Id>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<SavedBinding>> {
    bindings
        .iter()
        .map(|binding| ordinals.snapshot_binding(binding))
        .collect()
}

fn restore_bindings(
    bindings: BTreeSet<SavedBinding>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<Id>> {
    bindings
        .into_iter()
        .map(|binding| ordinals.restore_binding(binding))
        .collect()
}

fn snapshot_position_bucketed(
    buckets: &PositionBucketed<BTreeSet<Id>>,
    ordinals: &ContextOrdinals,
) -> Result<PositionBucketed<BTreeSet<SavedBinding>>> {
    Ok(PositionBucketed {
        eager: snapshot_bindings(&buckets.eager, ordinals)?,
        lazy: snapshot_bindings(&buckets.lazy, ordinals)?,
        first_order_lazy: snapshot_bindings(&buckets.first_order_lazy, ordinals)?,
    })
}

fn restore_position_bucketed(
    buckets: PositionBucketed<BTreeSet<SavedBinding>>,
    ordinals: &ContextOrdinals,
) -> Result<PositionBucketed<BTreeSet<Id>>> {
    Ok(PositionBucketed {
        eager: restore_bindings(buckets.eager, ordinals)?,
        lazy: restore_bindings(buckets.lazy, ordinals)?,
        first_order_lazy: restore_bindings(buckets.first_order_lazy, ordinals)?,
    })
}

fn snapshot_constructor(
    facts: &ConstructorFacts,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedConstructorFacts> {
    Ok(proto::SavedConstructorFacts {
        reads: snapshot_bindings(&facts.reads, ordinals)?.to_proto()?,
        rebinds: snapshot_bindings(&facts.rebinds, ordinals)?.to_proto()?,
        calls: snapshot_bindings(&facts.calls, ordinals)?.to_proto()?,
        unresolved_sources: snapshot_bindings(&facts.unresolved_sources, ordinals)?.to_proto()?,
        unresolved_inline_fn: Some(facts.unresolved_inline_fn),
    })
}

fn restore_constructor(
    value: proto::SavedConstructorFacts,
    ordinals: &ContextOrdinals,
) -> Result<ConstructorFacts> {
    Ok(ConstructorFacts {
        reads: restore_bindings(BTreeSet::from_proto(value.reads)?, ordinals)?,
        rebinds: restore_bindings(BTreeSet::from_proto(value.rebinds)?, ordinals)?,
        calls: restore_bindings(BTreeSet::from_proto(value.calls)?, ordinals)?,
        unresolved_sources: restore_bindings(
            BTreeSet::from_proto(value.unresolved_sources)?,
            ordinals,
        )?,
        unresolved_inline_fn: required(
            value.unresolved_inline_fn,
            "SavedConstructorFacts.unresolved_inline_fn",
        )?,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum SavedEffectCell {
    Binding(SavedBinding),
    GlobalProp(String),
    ArrayContents(u32),
    SetContents(u32),
    ObjectShape(u32),
    OwnProperty { origin: u32, property: String },
}

impl SavedEffectCell {
    fn snapshot(cell: &EffectCell, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(match cell {
            EffectCell::Binding(binding) => Self::Binding(ordinals.snapshot_binding(binding)?),
            EffectCell::GlobalProp(property) => Self::GlobalProp(property.clone()),
            EffectCell::ArrayContents(origin) => Self::ArrayContents(*origin),
            EffectCell::SetContents(origin) => Self::SetContents(*origin),
            EffectCell::ObjectShape(origin) => Self::ObjectShape(*origin),
            EffectCell::OwnProperty { origin, property } => Self::OwnProperty {
                origin: *origin,
                property: property.clone(),
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<EffectCell> {
        Ok(match self {
            Self::Binding(binding) => EffectCell::Binding(ordinals.restore_binding(binding)?),
            Self::GlobalProp(property) => EffectCell::GlobalProp(property),
            Self::ArrayContents(origin) => EffectCell::ArrayContents(origin),
            Self::SetContents(origin) => EffectCell::SetContents(origin),
            Self::ObjectShape(origin) => EffectCell::ObjectShape(origin),
            Self::OwnProperty { origin, property } => EffectCell::OwnProperty { origin, property },
        })
    }
}

fn snapshot_heap_cells(
    cells: &HeapContentCells,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedHeapContentCells> {
    Ok(proto::SavedHeapContentCells {
        allocations: snapshot_effect_cells(&cells.allocations, ordinals)?.to_proto()?,
        reads: snapshot_effect_cells(&cells.reads, ordinals)?.to_proto()?,
        writes: snapshot_effect_cells(&cells.writes, ordinals)?.to_proto()?,
        opaque: Some(cells.opaque),
        observes_external_heap: Some(cells.observes_external_heap),
    })
}

fn restore_heap_cells(
    value: proto::SavedHeapContentCells,
    ordinals: &ContextOrdinals,
) -> Result<HeapContentCells> {
    Ok(HeapContentCells {
        allocations: restore_effect_cells(BTreeSet::from_proto(value.allocations)?, ordinals)?,
        reads: restore_effect_cells(BTreeSet::from_proto(value.reads)?, ordinals)?,
        writes: restore_effect_cells(BTreeSet::from_proto(value.writes)?, ordinals)?,
        opaque: required(value.opaque, "SavedHeapContentCells.opaque")?,
        observes_external_heap: required(
            value.observes_external_heap,
            "SavedHeapContentCells.observes_external_heap",
        )?,
    })


fn snapshot_heap_accesses(
    accesses: &HeapContentAccesses,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedHeapContentAccesses> {
    Ok(proto::SavedHeapContentAccesses {
        reads: snapshot_effect_cells(&accesses.reads, ordinals)?.to_proto()?,
        writes: snapshot_effect_cells(&accesses.writes, ordinals)?.to_proto()?,
    })
}

fn restore_heap_accesses(
    value: proto::SavedHeapContentAccesses,
    ordinals: &ContextOrdinals,
) -> Result<HeapContentAccesses> {
    Ok(HeapContentAccesses {
        reads: restore_effect_cells(BTreeSet::from_proto(value.reads)?, ordinals)?,
        writes: restore_effect_cells(BTreeSet::from_proto(value.writes)?, ordinals)?,
    })
}

fn snapshot_effect_cells(
    cells: &BTreeSet<EffectCell>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<SavedEffectCell>> {
    cells
        .iter()
        .map(|cell| SavedEffectCell::snapshot(cell, ordinals))
        .collect()
}

fn restore_effect_cells(
    cells: BTreeSet<SavedEffectCell>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<EffectCell>> {
    cells
        .into_iter()
        .map(|cell| cell.restore(ordinals))
        .collect()
}

struct SavedFallbackOnlyEffects {
    reads: Vec<(SavedBinding, crate::execution_evidence::AssumedEffectReason)>,
    rebinds: Vec<(SavedBinding, crate::execution_evidence::AssumedEffectReason)>,
    uninitialized_reads: Vec<(SavedBinding, crate::execution_evidence::AssumedEffectReason)>,
}

impl SavedFallbackOnlyEffects {
    fn snapshot(effects: &FallbackOnlyEffects, ordinals: &ContextOrdinals) -> Result<Self> {
        fn snapshot_reasons(
            reasons: &BTreeMap<Id, crate::execution_evidence::AssumedEffectReason>,
            ordinals: &ContextOrdinals,
        ) -> Result<Vec<(SavedBinding, crate::execution_evidence::AssumedEffectReason)>> {
            reasons
                .iter()
                .map(|(binding, reason)| Ok((ordinals.snapshot_binding(binding)?, reason.clone())))
                .collect()
        }

        Ok(Self {
            reads: snapshot_reasons(&effects.reads, ordinals)?,
            rebinds: snapshot_reasons(&effects.rebinds, ordinals)?,
            uninitialized_reads: snapshot_reasons(&effects.uninitialized_reads, ordinals)?,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<FallbackOnlyEffects> {
        fn restore_reasons(
            reasons: Vec<(SavedBinding, crate::execution_evidence::AssumedEffectReason)>,
            ordinals: &ContextOrdinals,
        ) -> Result<BTreeMap<Id, crate::execution_evidence::AssumedEffectReason>> {
            reasons
                .into_iter()
                .map(|(binding, reason)| Ok((ordinals.restore_binding(binding)?, reason)))
                .collect()
        }

        Ok(FallbackOnlyEffects {
            reads: restore_reasons(self.reads, ordinals)?,
            rebinds: restore_reasons(self.rebinds, ordinals)?,
            uninitialized_reads: restore_reasons(self.uninitialized_reads, ordinals)?,
        })
    }
}

/// Snapshot directly into the artifact schema. Binding ordinals and purity nodes
/// remain explicit because they depend on state shared across the whole artifact.
pub fn snapshot_chunk_facts<'a>(
    analysis: &'a ChunkFactAnalysis,
    ordinals: &ContextOrdinals,
    purities: &mut PuritySnapshot<'a>,
) -> Result<proto::SavedChunkFactAnalysis> {
    Ok(proto::SavedChunkFactAnalysis {
        facts: analysis
            .facts
            .iter()
            .map(|fact| snapshot_statement(fact, ordinals, purities))
            .collect::<Result<_>>()?,
        top_level_await: analysis.top_level_await.to_proto()?,
        redundant_purity_hints: analysis.redundant_purity_hints.to_proto()?,
        redundant_pure_member_hints: analysis.redundant_pure_member_hints.to_proto()?,
        unconsulted_purity_hints: analysis.unconsulted_purity_hints.to_proto()?,
    })
}

pub fn restore_chunk_facts(
    value: proto::SavedChunkFactAnalysis,
    ordinals: &ContextOrdinals,
    purities: &RestoredPurities,
) -> Result<ChunkFactAnalysis> {
    Ok(ChunkFactAnalysis {
        facts: value
            .facts
            .into_iter()
            .map(|fact| restore_statement(fact, ordinals, purities))
            .collect::<Result<_>>()?,
        top_level_await: Option::from_proto(value.top_level_await)?,
        redundant_purity_hints: Vec::from_proto(value.redundant_purity_hints)?,
        redundant_pure_member_hints: Vec::from_proto(value.redundant_pure_member_hints)?,
        unconsulted_purity_hints: BTreeSet::from_proto(value.unconsulted_purity_hints)?,
    })
}

fn snapshot_statement<'a>(
    fact: &'a StatementFacts,
    ordinals: &ContextOrdinals,
    purities: &mut PuritySnapshot<'a>,
) -> Result<proto::SavedStatementFacts> {
    Ok(proto::SavedStatementFacts {
        ordinal: Some(fact.ordinal.to_proto()?),
        export_linkage: fact.export_linkage.to_proto()?,
        stable_iterable_producers: fact.stable_iterable_producers.to_proto()?,
        source_location: fact.source_location.to_proto()?,
        callback_factory_barriers: fact.callback_factory_barriers.to_proto()?,
        declared: snapshot_bindings(&fact.declared, ordinals)?.to_proto()?,
        local_bindings: snapshot_bindings(&fact.local_bindings, ordinals)?.to_proto()?,
        var_declared: snapshot_bindings(&fact.var_declared, ordinals)?.to_proto()?,
        var_initializers: snapshot_bindings(&fact.var_initializers, ordinals)?.to_proto()?,
        reads: Some(snapshot_position_bucketed(&fact.reads, ordinals)?.to_proto()?),
        rebinds: Some(snapshot_position_bucketed(&fact.rebinds, ordinals)?.to_proto()?),
        member_write_roots: snapshot_bindings(&fact.member_write_roots, ordinals)?.to_proto()?,
        local_effects: snapshot_bindings(&fact.local_effects, ordinals)?.to_proto()?,
        calls: Some(snapshot_position_bucketed(&fact.calls, ordinals)?.to_proto()?),
        constructor: fact
            .constructor
            .as_ref()
            .map(|constructor| snapshot_constructor(constructor, ordinals))
            .transpose()?,
        synchronous_calls: fact
            .synchronous_calls
            .as_ref()
            .map(|calls| SavedSynchronousCalls::snapshot(calls, ordinals, purities)?.to_proto())
            .transpose()?,
        synchronous_prefix_effects: fact
            .synchronous_prefix_effects
            .as_ref()
            .map(|(reads, rebinds)| -> Result<_> {
                Ok(proto::SavedSynchronousPrefixEffects {
                    reads: snapshot_bindings(reads, ordinals)?.to_proto()?,
                    rebinds: snapshot_bindings(rebinds, ordinals)?.to_proto()?,
                })
            })
            .transpose()?,
        at_init_unresolved_sources: snapshot_bindings(&fact.at_init_unresolved_sources, ordinals)?
            .to_proto()?,
        at_init_unresolved_inline_fn: Some(fact.at_init_unresolved_inline_fn),
        first_order_unresolved_sources: snapshot_bindings(
            &fact.first_order_unresolved_sources,
            ordinals,
        )?
        .to_proto()?,
        first_order_unresolved_inline_fn: Some(fact.first_order_unresolved_inline_fn),
        declares_direct_function: Some(fact.declares_direct_function),
        global_writes: fact.global_writes.to_proto()?,
        global_reads: fact.global_reads.to_proto()?,
        havoc: fact.havoc.to_proto()?,
        trusted_summary_discharged: fact.trusted_summary_discharged.to_proto()?,
        heap_contents: Some(snapshot_heap_cells(&fact.heap_contents, ordinals)?),
        heap_one_shot_allocations: snapshot_effect_cells(
            &fact.heap_one_shot_allocations,
            ordinals,
        )?
        .to_proto()?,
        deferred_heap_allocations: snapshot_effect_cells(
            &fact.deferred_heap_allocations,
            ordinals,
        )?
        .to_proto()?,
        heap_native_integrity_uses: fact.heap_native_integrity_uses.to_proto()?,
        purity: Some(purities.snapshot_value(&fact.purity)?),
        kind: Some(fact.kind.to_proto()?),
    })
}

fn restore_statement(
    value: proto::SavedStatementFacts,
    ordinals: &ContextOrdinals,
    purities: &RestoredPurities,
) -> Result<StatementFacts> {
    Ok(StatementFacts {
        ordinal: SourceUnitOrdinal::from_proto(required(
            value.ordinal,
            "SavedStatementFacts.ordinal",
        )?)?,
        export_linkage: Option::from_proto(value.export_linkage)?,
        stable_iterable_producers: BTreeSet::from_proto(value.stable_iterable_producers)?,
        source_location: Option::from_proto(value.source_location)?,
        callback_factory_barriers: Vec::from_proto(value.callback_factory_barriers)?,
        declared: restore_bindings(BTreeSet::from_proto(value.declared)?, ordinals)?,
        local_bindings: restore_bindings(BTreeSet::from_proto(value.local_bindings)?, ordinals)?,
        var_declared: restore_bindings(BTreeSet::from_proto(value.var_declared)?, ordinals)?,
        var_initializers: restore_bindings(
            BTreeSet::from_proto(value.var_initializers)?,
            ordinals,
        )?,
        reads: restore_position_bucketed(
            PositionBucketed::from_proto(required(value.reads, "SavedStatementFacts.reads")?)?,
            ordinals,
        )?,
        rebinds: restore_position_bucketed(
            PositionBucketed::from_proto(required(value.rebinds, "SavedStatementFacts.rebinds")?)?,
            ordinals,
        )?,
        member_write_roots: restore_bindings(
            BTreeSet::from_proto(value.member_write_roots)?,
            ordinals,
        )?,
        local_effects: restore_bindings(BTreeSet::from_proto(value.local_effects)?, ordinals)?,
        calls: restore_position_bucketed(
            PositionBucketed::from_proto(required(value.calls, "SavedStatementFacts.calls")?)?,
            ordinals,
        )?,
        constructor: value
            .constructor
            .map(|constructor| restore_constructor(constructor, ordinals))
            .transpose()?,
        synchronous_calls: value
            .synchronous_calls
            .map(|calls| SavedSynchronousCalls::from_proto(calls)?.restore(ordinals, purities))
            .transpose()?,
        synchronous_prefix_effects: value
            .synchronous_prefix_effects
            .map(|effects| -> Result<_> {
                Ok((
                    restore_bindings(BTreeSet::from_proto(effects.reads)?, ordinals)?,
                    restore_bindings(BTreeSet::from_proto(effects.rebinds)?, ordinals)?,
                ))
            })
            .transpose()?,
        at_init_unresolved_sources: restore_bindings(
            BTreeSet::from_proto(value.at_init_unresolved_sources)?,
            ordinals,
        )?,
        at_init_unresolved_inline_fn: required(
            value.at_init_unresolved_inline_fn,
            "SavedStatementFacts.at_init_unresolved_inline_fn",
        )?,
        first_order_unresolved_sources: restore_bindings(
            BTreeSet::from_proto(value.first_order_unresolved_sources)?,
            ordinals,
        )?,
        first_order_unresolved_inline_fn: required(
            value.first_order_unresolved_inline_fn,
            "SavedStatementFacts.first_order_unresolved_inline_fn",
        )?,
        declares_direct_function: required(
            value.declares_direct_function,
            "SavedStatementFacts.declares_direct_function",
        )?,
        global_writes: BTreeSet::from_proto(value.global_writes)?,
        global_reads: BTreeSet::from_proto(value.global_reads)?,
        havoc: BTreeSet::from_proto(value.havoc)?,
        trusted_summary_discharged: BTreeSet::from_proto(value.trusted_summary_discharged)?,
        heap_contents: restore_heap_cells(
            required(value.heap_contents, "SavedStatementFacts.heap_contents")?,
            ordinals,
        )?,
        heap_one_shot_allocations: restore_effect_cells(
            BTreeSet::from_proto(value.heap_one_shot_allocations)?,
            ordinals,
        )?,
        deferred_heap_allocations: restore_effect_cells(
            BTreeSet::from_proto(value.deferred_heap_allocations)?,
            ordinals,
        )?,
        heap_native_integrity_uses: Vec::from_proto(value.heap_native_integrity_uses)?,
        purity: purities.restore_value(required(value.purity, "SavedStatementFacts.purity")?)?,
        kind: StatementKind::from_proto(required(value.kind, "SavedStatementFacts.kind")?)?,
    })
}

/// Synchronous execution facts with every runtime binding converted explicitly.
struct SavedSynchronousCalls<'a> {
    initializer_binding_effects:
        Cow<'a, Option<Box<crate::reports::schema::InitializerBindingEffectsUse>>>,
    initializer_external_effects:
        Cow<'a, Option<crate::reports::schema::InitializerExternalEffectsUse>>,
    completed_initialization:
        Cow<'a, Option<super::sync_effects::noninvoking_initialization::CompletedInitialization>>,
    initialization_coverage_refusal:
        Cow<'a, Option<crate::facts::OwnerInitializationCoverageRefusalReason>>,
    root_attempt: Option<crate::facts::RootAttempt>,
    evaluation_effects: Cow<'a, super::evaluation_effects::EvaluationEffects>,
    local_record_factory_uses: Cow<'a, [LocalRecordFactoryUse]>,
    local_record_factory_rejections: Cow<'a, [LocalRecordFactoryRejection]>,
    local_record_factory_purity: Cow<'a, Option<super::LocalRecordFactoryPurityCertificate>>,
    proven_private_own_field_reads: Cow<'a, [(SourceUnitOrdinal, swc_common::Span)]>,
    local_record_factory_heap_call: Option<swc_common::Span>,
    reads: Vec<(SavedBinding, SourceUnitOrdinal)>,
    rebinds: Vec<(SavedBinding, SourceUnitOrdinal)>,
    uninitialized_reads: BTreeSet<SavedBinding>,
    fallback_only: SavedFallbackOnlyEffects,
    complete: bool,
    binding_effect_coverage: SavedBindingEffectCoverage,
    settled_array_elements: BTreeMap<SavedBinding, super::SettledElements>,
    global_object_aliases: Option<BTreeSet<SavedBinding>>,
    heap_contents: proto::SavedHeapContentCells,
    non_assumed_heap_accesses: proto::SavedHeapContentAccesses,
    precision_losses: Cow<'a, [ExecutionPrecisionLoss]>,
    native_integrity_checks: Cow<'a, [crate::reports::schema::NativeIntegrityCheck]>,
    native_prototype_property_checks:
        Cow<'a, [crate::reports::schema::NativePrototypePropertyCheck]>,
    native_prefix_observations: Cow<'a, [crate::reports::schema::NativePrefixObservation]>,
    native_integrity_uses: Cow<'a, [crate::reports::schema::NativeIntegrityUse]>,
    native_operation_input_uses: Cow<'a, [NativeOperationInputUse]>,
    imported_primitive_array_spread_input_uses:
        Cow<'a, [crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse]>,
    inference_uses: Cow<'a, [crate::analysis_provenance::InferenceUse]>,
    callback_factory_uses: Cow<'a, [super::CallbackFactoryUseFact]>,
    returned_record_uses: Cow<'a, [super::ReturnedRecordUseFact]>,
    author_host_contract_uses: Cow<'a, [ReentryFreeOperationUse]>,
    purity_disagreement: Option<SavedPurityDisagreement>,
}

struct SavedPurityDisagreement {
    prepared: proto::SavedPurity,
    completed: proto::SavedPurity,
}

impl<'a> SavedSynchronousCalls<'a> {
    fn snapshot(
        calls: &'a SynchronousCalls,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        Ok(Self {
            initializer_binding_effects: Cow::Borrowed(&calls.initializer_binding_effects),
            initializer_external_effects: Cow::Borrowed(&calls.initializer_external_effects),
            completed_initialization: Cow::Borrowed(&calls.completed_initialization),
            initialization_coverage_refusal: Cow::Borrowed(&calls.initialization_coverage_refusal),
            root_attempt: calls.root_attempt,
            evaluation_effects: Cow::Borrowed(&calls.evaluation_effects),
            local_record_factory_uses: Cow::Borrowed(&calls.local_record_factory_uses),
            local_record_factory_rejections: Cow::Borrowed(&calls.local_record_factory_rejections),
            local_record_factory_purity: Cow::Borrowed(&calls.local_record_factory_purity),
            proven_private_own_field_reads: Cow::Borrowed(&calls.proven_private_own_field_reads),
            local_record_factory_heap_call: calls.local_record_factory_heap_call,
            reads: calls
                .reads
                .iter()
                .map(|(binding, ordinal)| Ok((ordinals.snapshot_binding(binding)?, *ordinal)))
                .collect::<Result<_>>()?,
            rebinds: calls
                .rebinds
                .iter()
                .map(|(binding, ordinal)| Ok((ordinals.snapshot_binding(binding)?, *ordinal)))
                .collect::<Result<_>>()?,
            uninitialized_reads: snapshot_bindings(&calls.uninitialized_reads, ordinals)?,
            fallback_only: SavedFallbackOnlyEffects::snapshot(&calls.fallback_only, ordinals)?,
            complete: calls.complete,
            binding_effect_coverage: calls.binding_effect_coverage.snapshot(ordinals)?,
            settled_array_elements: calls
                .settled_array_elements
                .iter()
                .map(|(binding, elements)| {
                    Ok((ordinals.snapshot_binding(binding)?, elements.clone()))
                })
                .collect::<Result<_>>()?,
            global_object_aliases: calls
                .global_object_aliases
                .as_ref()
                .map(|aliases| snapshot_bindings(aliases, ordinals))
                .transpose()?,
            heap_contents: snapshot_heap_cells(&calls.heap_contents, ordinals)?,
            non_assumed_heap_accesses: snapshot_heap_accesses(
                &calls.non_assumed_heap_accesses,
                ordinals,
            )?,
            precision_losses: Cow::Borrowed(&calls.precision_losses),
            native_integrity_checks: Cow::Borrowed(&calls.native_integrity_checks),
            native_prototype_property_checks: Cow::Borrowed(
                &calls.native_prototype_property_checks,
            ),
            native_prefix_observations: Cow::Borrowed(&calls.native_prefix_observations),
            native_integrity_uses: Cow::Borrowed(&calls.native_integrity_uses),
            native_operation_input_uses: Cow::Borrowed(&calls.native_operation_input_uses),
            imported_primitive_array_spread_input_uses: Cow::Borrowed(
                &calls.imported_primitive_array_spread_input_uses,
            ),
            inference_uses: Cow::Borrowed(&calls.inference_uses),
            callback_factory_uses: Cow::Borrowed(&calls.callback_factory_uses),
            returned_record_uses: Cow::Borrowed(&calls.returned_record_uses),
            author_host_contract_uses: Cow::Borrowed(&calls.author_host_contract_uses),
            purity_disagreement: calls
                .purity_disagreement
                .as_ref()
                .map(|disagreement| -> Result<_> {
                    Ok(SavedPurityDisagreement {
                        prepared: purities.snapshot_value(&disagreement.prepared)?,
                        completed: purities.snapshot_value(&disagreement.completed)?,
                    })
                })
                .transpose()?,
        })
    }

    fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
    ) -> Result<SynchronousCalls> {
        Ok(SynchronousCalls {
            initializer_binding_effects: self.initializer_binding_effects.into_owned(),
            initializer_external_effects: self.initializer_external_effects.into_owned(),
            completed_initialization: self.completed_initialization.into_owned(),
            initialization_coverage_refusal: self.initialization_coverage_refusal.into_owned(),
            root_attempt: self.root_attempt,
            evaluation_effects: self.evaluation_effects.into_owned(),
            purity_disagreement: self
                .purity_disagreement
                .map(|disagreement| -> Result<_> {
                    Ok(crate::reports::schema::PurityCompletionDisagreement {
                        prepared: purities.restore_value(disagreement.prepared)?,
                        completed: purities.restore_value(disagreement.completed)?,
                    })
                })
                .transpose()?,
            local_record_factory_uses: self.local_record_factory_uses.into_owned(),
            local_record_factory_rejections: self.local_record_factory_rejections.into_owned(),
            local_record_factory_purity: self.local_record_factory_purity.into_owned(),
            proven_private_own_field_reads: self.proven_private_own_field_reads.into_owned(),
            local_record_factory_heap_call: self.local_record_factory_heap_call,
            reads: self
                .reads
                .into_iter()
                .map(|(binding, ordinal)| Ok((ordinals.restore_binding(binding)?, ordinal)))
                .collect::<Result<_>>()?,
            rebinds: self
                .rebinds
                .into_iter()
                .map(|(binding, ordinal)| Ok((ordinals.restore_binding(binding)?, ordinal)))
                .collect::<Result<_>>()?,
            uninitialized_reads: restore_bindings(self.uninitialized_reads, ordinals)?,
            fallback_only: self.fallback_only.restore(ordinals)?,
            complete: self.complete,
            binding_effect_coverage: self.binding_effect_coverage.restore(ordinals)?,
            settled_array_elements: self
                .settled_array_elements
                .into_iter()
                .map(|(binding, elements)| Ok((ordinals.restore_binding(binding)?, elements)))
                .collect::<Result<_>>()?,
            global_object_aliases: self
                .global_object_aliases
                .map(|aliases| restore_bindings(aliases, ordinals))
                .transpose()?,
            heap_contents: restore_heap_cells(self.heap_contents, ordinals)?,
            non_assumed_heap_accesses: restore_heap_accesses(
                self.non_assumed_heap_accesses,
                ordinals,
            )?,
            precision_losses: self.precision_losses.into_owned(),
            native_integrity_checks: self.native_integrity_checks.into_owned(),
            native_prototype_property_checks: self.native_prototype_property_checks.into_owned(),
            native_prefix_observations: self.native_prefix_observations.into_owned(),
            native_integrity_uses: self.native_integrity_uses.into_owned(),
            native_operation_input_uses: self.native_operation_input_uses.into_owned(),
            imported_primitive_array_spread_input_uses: self
                .imported_primitive_array_spread_input_uses
                .into_owned(),
            inference_uses: self.inference_uses.into_owned(),
            callback_factory_uses: self.callback_factory_uses.into_owned(),
            returned_record_uses: self.returned_record_uses.into_owned(),
            author_host_contract_uses: self.author_host_contract_uses.into_owned(),
        })
    }
}

impl ProtoConvert<proto::SavedBindingEffects> for PositionBucketed<BTreeSet<SavedBinding>> {
    fn to_proto(&self) -> Result<proto::SavedBindingEffects> {
        Ok(proto::SavedBindingEffects {
            eager: self.eager.to_proto()?,
            lazy: self.lazy.to_proto()?,
            first_order_lazy: self.first_order_lazy.to_proto()?,
        })
    }
    fn from_proto(value: proto::SavedBindingEffects) -> Result<Self> {
        Ok(Self {
            eager: BTreeSet::from_proto(value.eager)?,
            lazy: BTreeSet::from_proto(value.lazy)?,
            first_order_lazy: BTreeSet::from_proto(value.first_order_lazy)?,
        })
    }
}

impl ProtoConvert<proto::SavedSynchronousPrefixEffects>
    for (BTreeSet<SavedBinding>, BTreeSet<SavedBinding>)
{
    fn to_proto(&self) -> Result<proto::SavedSynchronousPrefixEffects> {
        Ok(proto::SavedSynchronousPrefixEffects {
            reads: self.0.to_proto()?,
            rebinds: self.1.to_proto()?,
        })
    }
    fn from_proto(value: proto::SavedSynchronousPrefixEffects) -> Result<Self> {
        Ok((
            BTreeSet::from_proto(value.reads)?,
            BTreeSet::from_proto(value.rebinds)?,
        ))
    }
}

impl ProtoConvert<proto::SavedAssumedBindingEffect> for (SavedBinding, AssumedEffectReason) {
    fn to_proto(&self) -> Result<proto::SavedAssumedBindingEffect> {
        Ok(proto::SavedAssumedBindingEffect {
            binding: Some(self.0.to_proto()?),
            reason: Some(self.1.to_proto()?),
        })
    }
    fn from_proto(value: proto::SavedAssumedBindingEffect) -> Result<Self> {
        Ok((
            SavedBinding::from_proto(required(
                value.binding,
                "SavedAssumedBindingEffect.binding",
            )?)?,
            AssumedEffectReason::from_proto(required(
                value.reason,
                "SavedAssumedBindingEffect.reason",
            )?)?,
        ))
    }
}

impl ProtoConvert<proto::SavedBindingEffectSite> for (SavedBinding, SourceUnitOrdinal) {
    fn to_proto(&self) -> Result<proto::SavedBindingEffectSite> {
        Ok(proto::SavedBindingEffectSite {
            binding: Some(self.0.to_proto()?),
            source_unit: Some(self.1.to_proto()?),
        })
    }
    fn from_proto(value: proto::SavedBindingEffectSite) -> Result<Self> {
        Ok((
            SavedBinding::from_proto(required(value.binding, "SavedBindingEffectSite.binding")?)?,
            SourceUnitOrdinal::from_proto(required(
                value.source_unit,
                "SavedBindingEffectSite.source_unit",
            )?)?,
        ))
    }
}

impl ProtoConvert<proto::SavedPrivateOwnFieldRead> for (SourceUnitOrdinal, swc_common::Span) {
    fn to_proto(&self) -> Result<proto::SavedPrivateOwnFieldRead> {
        Ok(proto::SavedPrivateOwnFieldRead {
            source_unit: Some(self.0.to_proto()?),
            span: Some(self.1.to_proto()?),
        })
    }
    fn from_proto(value: proto::SavedPrivateOwnFieldRead) -> Result<Self> {
        Ok((
            SourceUnitOrdinal::from_proto(required(
                value.source_unit,
                "SavedPrivateOwnFieldRead.source_unit",
            )?)?,
            swc_common::Span::from_proto(required(value.span, "SavedPrivateOwnFieldRead.span")?)?,
        ))
    }
}

fn settled_array_elements_from_proto(
    entries: Vec<proto::SavedSettledArrayElements>,
) -> Result<BTreeMap<SavedBinding, super::SettledElements>> {
    let mut values = BTreeMap::new();
    for entry in entries {
        let binding = SavedBinding::from_proto(required(
            entry.binding,
            "SavedSettledArrayElements.binding",
        )?)?;
        let elements = super::SettledElements::from_proto(required(
            entry.elements,
            "SavedSettledArrayElements.elements",
        )?)?;
        ensure!(
            values.insert(binding, elements).is_none(),
            "duplicate settled-array binding"
        );
    }
    Ok(values)
}

impl ProtoConvert<proto::SavedEffectCell> for SavedEffectCell {
    fn to_proto(&self) -> Result<proto::SavedEffectCell> {
        use proto::saved_effect_cell::{OwnProperty, Value};
        Ok(proto::SavedEffectCell {
            value: Some(match self {
                Self::Binding(binding) => Value::Binding(binding.to_proto()?),
                Self::GlobalProp(property) => Value::GlobalProp(property.clone()),
                Self::ArrayContents(origin) => Value::ArrayContents(*origin),
                Self::SetContents(origin) => Value::SetContents(*origin),
                Self::ObjectShape(origin) => Value::ObjectShape(*origin),
                Self::OwnProperty { origin, property } => Value::OwnProperty(OwnProperty {
                    origin: Some(*origin),
                    property: Some(property.clone()),
                }),
            }),
        })
    }

    fn from_proto(value: proto::SavedEffectCell) -> Result<Self> {
        use proto::saved_effect_cell::Value;
        Ok(match required(value.value, "SavedEffectCell.value")? {
            Value::Binding(value) => Self::Binding(SavedBinding::from_proto(value)?),
            Value::GlobalProp(value) => Self::GlobalProp(value),
            Value::ArrayContents(value) => Self::ArrayContents(value),
            Value::SetContents(value) => Self::SetContents(value),
            Value::ObjectShape(value) => Self::ObjectShape(value),
            Value::OwnProperty(value) => Self::OwnProperty {
                origin: required(value.origin, "SavedEffectCell.own_property.origin")?,
                property: required(value.property, "SavedEffectCell.own_property.property")?,
            },
        })
    }
}

impl ProtoConvert<proto::SavedFallbackOnlyEffects> for SavedFallbackOnlyEffects {
    fn to_proto(&self) -> Result<proto::SavedFallbackOnlyEffects> {
        Ok(proto::SavedFallbackOnlyEffects {
            reads: self.reads.to_proto()?,
            rebinds: self.rebinds.to_proto()?,
            uninitialized_reads: self.uninitialized_reads.to_proto()?,
        })
    }

    fn from_proto(value: proto::SavedFallbackOnlyEffects) -> Result<Self> {
        Ok(Self {
            reads: Vec::from_proto(value.reads)?,
            rebinds: Vec::from_proto(value.rebinds)?,
            uninitialized_reads: Vec::from_proto(value.uninitialized_reads)?,
        })
    }
}

impl<'a> ProtoConvert<proto::SavedSynchronousCalls> for SavedSynchronousCalls<'a> {
    fn to_proto(&self) -> Result<proto::SavedSynchronousCalls> {
        Ok(proto::SavedSynchronousCalls {
            initializer_binding_effects: self.initializer_binding_effects.to_proto()?,
            initializer_external_effects: self.initializer_external_effects.to_proto()?,
            completed_initialization: self.completed_initialization.to_proto()?,
            initialization_coverage_refusal: self.initialization_coverage_refusal.to_proto()?,
            root_attempt: self.root_attempt.to_proto()?,
            evaluation_effects: Some(self.evaluation_effects.to_proto()?),
            local_record_factory_uses: self.local_record_factory_uses.to_proto()?,
            local_record_factory_rejections: self.local_record_factory_rejections.to_proto()?,
            local_record_factory_purity: self.local_record_factory_purity.to_proto()?,
            proven_private_own_field_reads: self.proven_private_own_field_reads.to_proto()?,
            local_record_factory_heap_call: self.local_record_factory_heap_call.to_proto()?,
            reads: self.reads.to_proto()?,
            rebinds: self.rebinds.to_proto()?,
            uninitialized_reads: self.uninitialized_reads.to_proto()?,
            fallback_only: Some(self.fallback_only.to_proto()?),
            complete: Some(self.complete),
            binding_effect_coverage: Some(self.binding_effect_coverage.to_proto()?),
            settled_array_elements: self
                .settled_array_elements
                .iter()
                .map(|(binding, elements)| {
                    Ok(proto::SavedSettledArrayElements {
                        binding: Some(binding.to_proto()?),
                        elements: Some(elements.to_proto()?),
                    })
                })
                .collect::<Result<_>>()?,
            global_object_aliases: self
                .global_object_aliases
                .as_ref()
                .map(|bindings| -> Result<_> {
                    Ok(proto::SavedGlobalObjectAliases {
                        bindings: bindings.to_proto()?,
                    })
                })
                .transpose()?,
            heap_contents: Some(self.heap_contents.clone()),
            non_assumed_heap_accesses: Some(self.non_assumed_heap_accesses.clone()),
            precision_losses: self.precision_losses.to_proto()?,
            native_integrity_checks: self.native_integrity_checks.to_proto()?,
            native_prototype_property_checks: self.native_prototype_property_checks.to_proto()?,
            native_prefix_observations: self.native_prefix_observations.to_proto()?,
            native_integrity_uses: self.native_integrity_uses.to_proto()?,
            native_operation_input_uses: self.native_operation_input_uses.to_proto()?,
            imported_primitive_array_spread_input_uses: self
                .imported_primitive_array_spread_input_uses
                .to_proto()?,
            inference_uses: self.inference_uses.to_proto()?,
            callback_factory_uses: self.callback_factory_uses.to_proto()?,
            returned_record_uses: self.returned_record_uses.to_proto()?,
            author_host_contract_uses: self.author_host_contract_uses.to_proto()?,
            purity_disagreement: self.purity_disagreement.to_proto()?,
        })
    }

    fn from_proto(value: proto::SavedSynchronousCalls) -> Result<Self> {
        Ok(Self {
            initializer_binding_effects: Cow::from_proto(value.initializer_binding_effects)?,
            initializer_external_effects: Cow::from_proto(value.initializer_external_effects)?,
            completed_initialization: Cow::from_proto(value.completed_initialization)?,
            initialization_coverage_refusal: Cow::from_proto(
                value.initialization_coverage_refusal,
            )?,
            root_attempt: Option::from_proto(value.root_attempt)?,
            evaluation_effects: Cow::from_proto(required(
                value.evaluation_effects,
                "SavedSynchronousCalls.evaluation_effects",
            )?)?,
            local_record_factory_uses: Cow::from_proto(value.local_record_factory_uses)?,
            local_record_factory_rejections: Cow::from_proto(
                value.local_record_factory_rejections,
            )?,
            local_record_factory_purity: Cow::from_proto(value.local_record_factory_purity)?,
            proven_private_own_field_reads: Cow::from_proto(value.proven_private_own_field_reads)?,
            local_record_factory_heap_call: Option::from_proto(
                value.local_record_factory_heap_call,
            )?,
            reads: Vec::from_proto(value.reads)?,
            rebinds: Vec::from_proto(value.rebinds)?,
            uninitialized_reads: BTreeSet::from_proto(value.uninitialized_reads)?,
            fallback_only: SavedFallbackOnlyEffects::from_proto(required(
                value.fallback_only,
                "SavedSynchronousCalls.fallback_only",
            )?)?,
            complete: required(value.complete, "SavedSynchronousCalls.complete")?,
            binding_effect_coverage: SavedBindingEffectCoverage::from_proto(required(
                value.binding_effect_coverage,
                "SavedSynchronousCalls.binding_effect_coverage",
            )?)?,
            settled_array_elements: settled_array_elements_from_proto(
                value.settled_array_elements,
            )?,
            global_object_aliases: value
                .global_object_aliases
                .map(|value| BTreeSet::from_proto(value.bindings))
                .transpose()?,
            heap_contents: required(value.heap_contents, "SavedSynchronousCalls.heap_contents")?,
            non_assumed_heap_accesses: required(
                value.non_assumed_heap_accesses,
                "SavedSynchronousCalls.non_assumed_heap_accesses",
            )?,
            precision_losses: Cow::from_proto(value.precision_losses)?,
            native_integrity_checks: Cow::from_proto(value.native_integrity_checks)?,
            native_prototype_property_checks: Cow::from_proto(
                value.native_prototype_property_checks,
            )?,
            native_prefix_observations: Cow::from_proto(value.native_prefix_observations)?,
            native_integrity_uses: Cow::from_proto(value.native_integrity_uses)?,
            native_operation_input_uses: Cow::from_proto(value.native_operation_input_uses)?,
            imported_primitive_array_spread_input_uses: Cow::from_proto(
                value.imported_primitive_array_spread_input_uses,
            )?,
            inference_uses: Cow::from_proto(value.inference_uses)?,
            callback_factory_uses: Cow::from_proto(value.callback_factory_uses)?,
            returned_record_uses: Cow::from_proto(value.returned_record_uses)?,
            author_host_contract_uses: Cow::from_proto(value.author_host_contract_uses)?,
            purity_disagreement: Option::from_proto(value.purity_disagreement)?,
        })
    }
}

impl ProtoConvert<proto::SavedPurityDisagreement> for SavedPurityDisagreement {
    fn to_proto(&self) -> Result<proto::SavedPurityDisagreement> {
        Ok(proto::SavedPurityDisagreement {
            prepared: Some(self.prepared.clone()),
            completed: Some(self.completed.clone()),
        })
    }

    fn from_proto(value: proto::SavedPurityDisagreement) -> Result<Self> {
        Ok(Self {
            prepared: required(value.prepared, "SavedPurityDisagreement.prepared")?,
            completed: required(value.completed, "SavedPurityDisagreement.completed")?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProofDependencies;
    use crate::purity::{PurityReason, PurityRule};
    use crate::reports::schema::PurityCompletionDisagreement;
    use std::sync::Arc;
    use swc_common::{BytePos, Span};

    #[test]
    fn saved_synchronous_calls_require_authority_and_effect_metadata() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let mut purities = PuritySnapshot::default();
        let calls = SynchronousCalls::default();
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
            .expect("default facts snapshot");
        use crate::saved_proto::ProtoConvert;
        let wire = saved.to_proto().expect("typed snapshot");
        assert!(SavedSynchronousCalls::from_proto(wire.clone()).is_ok());
        for field in [
            "binding_effect_coverage",
            "complete",
            "fallback_only",
            "evaluation_effects",
            "heap_contents",
            "non_assumed_heap_accesses",
        ] {
            let mut missing = wire.clone();
            match field {
                "binding_effect_coverage" => missing.binding_effect_coverage = None,
                "complete" => missing.complete = None,
                "fallback_only" => missing.fallback_only = None,
                "evaluation_effects" => missing.evaluation_effects = None,
                "heap_contents" => missing.heap_contents = None,
                "non_assumed_heap_accesses" => missing.non_assumed_heap_accesses = None,
                _ => unreachable!(),
            }
            assert!(
                SavedSynchronousCalls::from_proto(missing).is_err(),
                "omitted {field} must not weaken effects"
            );
        }
    }

    #[test]
    fn saved_chunk_rejects_incomplete_effects_and_unknown_statement_kinds() {
        let module = raw_js_test_support::parse("const value = 1;\n");
        let analysis = analyze_chunk(
            &module,
            &crate::analysis_inputs::AnalysisInputs::default(),
            None,
        );
        let ordinals = ContextOrdinals::new([&module]);
        let mut purities = PuritySnapshot::default();
        let wire = snapshot_chunk_facts(&analysis, &ordinals, &mut purities).unwrap();
        let restored_purities = RestoredPurities::restore(purities.finish()).unwrap();
        assert!(restore_chunk_facts(wire.clone(), &ordinals, &restored_purities).is_ok());
        for field in [
            "ordinal",
            "heap_contents",
            "purity",
            "kind",
            "reads",
            "duplicate_binding",
            "unknown_opacity",
            "export_linkage",
        ] {
            let mut invalid = wire.clone();
            let fact = &mut invalid.facts[0];
            match field {
                "ordinal" => fact.ordinal = None,
                "heap_contents" => fact.heap_contents = None,
                "purity" => fact.purity = None,
                "kind" => fact.kind = Some(0),
                "reads" => fact.reads = None,
                "duplicate_binding" => {
                    let bindings = &mut fact.declared;
                    bindings.push(bindings[0].clone());
                }
                "unknown_opacity" => fact.havoc.push(i32::MAX),
                "export_linkage" => fact.export_linkage = Some(0),
                _ => unreachable!(),
            }
            assert!(
                restore_chunk_facts(invalid, &ordinals, &restored_purities).is_err(),
                "invalid {field} must not weaken effects"
            );
        }
    }

    #[test]
    fn optional_empty_prefixes_and_global_aliases_keep_presence() {
        let module = raw_js_test_support::parse("const value = 1;");
        let analysis = analyze_chunk(
            &module,
            &crate::analysis_inputs::AnalysisInputs::default(),
            None,
        );
        let ordinals = ContextOrdinals::new([&module]);
        let mut purities = PuritySnapshot::default();
        let mut wire = snapshot_chunk_facts(&analysis, &ordinals, &mut purities).unwrap();
        let restored_purities = RestoredPurities::restore(purities.finish()).unwrap();
        wire.facts[0].synchronous_prefix_effects = None;
        let absent = analysis_codec::encode_protobuf(&wire).unwrap();
        let restored = restore_chunk_facts(wire.clone(), &ordinals, &restored_purities).unwrap();
        assert!(restored.facts[0].synchronous_prefix_effects.is_none());
        wire.facts[0].synchronous_prefix_effects =
            Some(proto::SavedSynchronousPrefixEffects::default());
        let present = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_ne!(absent, present);
        let restored = restore_chunk_facts(
            analysis_codec::decode_protobuf(&present).unwrap(),
            &ordinals,
            &restored_purities,
        )
        .unwrap();
        let (reads, rebinds) = restored.facts[0]
            .synchronous_prefix_effects
            .as_ref()
            .unwrap();
        assert!(reads.is_empty() && rebinds.is_empty());

        let mut purities = PuritySnapshot::default();
        let calls = SynchronousCalls::default();
        let mut wire = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
            .unwrap()
            .to_proto()
            .unwrap();
        wire.global_object_aliases = None;
        let absent = analysis_codec::encode_protobuf(&wire).unwrap();
        assert!(
            SavedSynchronousCalls::from_proto(wire.clone())
                .unwrap()
                .global_object_aliases
                .is_none()
        );
        wire.global_object_aliases = Some(proto::SavedGlobalObjectAliases::default());
        let present = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_ne!(absent, present);
        let restored =
            SavedSynchronousCalls::from_proto(analysis_codec::decode_protobuf(&present).unwrap())
                .unwrap();
        assert!(restored.global_object_aliases.as_ref().unwrap().is_empty());
    }

    #[test]
    fn fact_collections_reject_duplicate_bindings_and_keep_zero_oneof_values() {
        let binding = proto::SavedBinding {
            symbol: Some("value".into()),
            context_ordinal: Some(0),
        };
        let duplicate = vec![binding.clone(), binding.clone()];
        assert!(
            PositionBucketed::<BTreeSet<SavedBinding>>::from_proto(proto::SavedBindingEffects {
                eager: duplicate.clone(),
                ..proto::SavedBindingEffects::default()
            })
            .is_err()
        );
        for (reads, rebinds) in [(duplicate.clone(), vec![]), (vec![], duplicate.clone())] {
            assert!(
                <(BTreeSet<SavedBinding>, BTreeSet<SavedBinding>)>::from_proto(
                    proto::SavedSynchronousPrefixEffects { reads, rebinds }
                )
                .is_err()
            );
        }
        assert!(BTreeSet::<SavedBinding>::from_proto(duplicate).is_err());
        let entry = proto::SavedSettledArrayElements {
            binding: Some(binding),
            elements: Some(proto::SettledElements {
                value: Some(proto::settled_elements::Value::Primitive(Default::default())),
            }),
        };
        assert!(settled_array_elements_from_proto(vec![entry.clone(), entry]).is_err());
        assert!(
            settled_array_elements_from_proto(vec![proto::SavedSettledArrayElements::default()])
                .is_err()
        );
        for cell in [
            SavedEffectCell::GlobalProp(String::new()),
            SavedEffectCell::ArrayContents(0),
            SavedEffectCell::SetContents(0),
            SavedEffectCell::ObjectShape(0),
            SavedEffectCell::OwnProperty {
                origin: 0,
                property: String::new(),
            },
        ] {
            let wire = cell.to_proto().unwrap();
            let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
            assert_eq!(
                SavedEffectCell::from_proto(analysis_codec::decode_protobuf(&bytes).unwrap())
                    .unwrap(),
                cell
            );
        }
        assert!(SavedEffectCell::from_proto(proto::SavedEffectCell::default()).is_err());
        assert!(
            <(SavedBinding, SourceUnitOrdinal)>::from_proto(proto::SavedBindingEffectSite {
                binding: None,
                source_unit: Some(0)
            })
            .is_err()
        );
        assert!(
            <(SourceUnitOrdinal, Span)>::from_proto(proto::SavedPrivateOwnFieldRead {
                source_unit: Some(0),
                span: Some(proto::SourceSpan {
                    lo: Some(8),
                    hi: Some(3)
                })
            })
            .is_err()
        );
    }

    #[test]
    fn saved_effect_pairs_require_fields_and_preserve_zero_ordinals_and_spans() {
        let binding = SavedBinding::from_parts("value".into(), 0);
        let assumed = (
            binding.clone(),
            AssumedEffectReason::OpaqueCallbackInvocation,
        );
        let wire = assumed.to_proto().unwrap();
        let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_eq!(
            <(SavedBinding, AssumedEffectReason)>::from_proto(
                analysis_codec::decode_protobuf(&bytes).unwrap()
            )
            .unwrap(),
            assumed
        );
        for missing_binding in [true, false] {
            let mut missing = wire.clone();
            if missing_binding {
                missing.binding = None;
            } else {
                missing.reason = None;
            }
            assert!(<(SavedBinding, AssumedEffectReason)>::from_proto(missing).is_err());
        }

        let site = (binding, SourceUnitOrdinal(0));
        let wire = site.to_proto().unwrap();
        assert_eq!(wire.source_unit, Some(0));
        let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_eq!(
            <(SavedBinding, SourceUnitOrdinal)>::from_proto(
                analysis_codec::decode_protobuf(&bytes).unwrap()
            )
            .unwrap(),
            site
        );
        for missing_binding in [true, false] {
            let mut missing = wire.clone();
            if missing_binding {
                missing.binding = None;
            } else {
                missing.source_unit = None;
            }
            assert!(<(SavedBinding, SourceUnitOrdinal)>::from_proto(missing).is_err());
        }

        let read = (SourceUnitOrdinal(0), Span::new(BytePos(0), BytePos(0)));
        let wire = read.to_proto().unwrap();
        assert_eq!(wire.source_unit, Some(0));
        assert_eq!(wire.span.as_ref().unwrap().lo, Some(0));
        assert_eq!(wire.span.as_ref().unwrap().hi, Some(0));
        let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_eq!(
            <(SourceUnitOrdinal, Span)>::from_proto(
                analysis_codec::decode_protobuf(&bytes).unwrap()
            )
            .unwrap(),
            read
        );
        for missing_source_unit in [true, false] {
            let mut missing = wire;
            if missing_source_unit {
                missing.source_unit = None;
            } else {
                missing.span = None;
            }
            assert!(<(SourceUnitOrdinal, Span)>::from_proto(missing).is_err());
        }
    }

    #[test]
    fn empty_chunk_facts_and_zero_await_ordinal_have_distinct_meanings() {
        let empty = proto::SavedChunkFactAnalysis::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let purities = RestoredPurities::restore(proto::SavedPurityGraph::default()).unwrap();
        let restored = restore_chunk_facts(empty.clone(), &ordinals, &purities).unwrap();
        assert!(restored.facts.is_empty());
        assert!(restored.top_level_await.is_none());
        let present = proto::SavedChunkFactAnalysis {
            top_level_await: Some(0),
            ..empty.clone()
        };
        assert_ne!(
            analysis_codec::encode_protobuf(&empty).unwrap(),
            analysis_codec::encode_protobuf(&present).unwrap()
        );
        assert_eq!(
            restore_chunk_facts(present, &ordinals, &purities)
                .unwrap()
                .top_level_await,
            Some(SourceUnitOrdinal(0))
        );
    }

    // A disagreement denotes a classifier invariant failure, so a normal
    // source fixture must not manufacture one just to exercise its transport.
    #[test]
    fn disagreement_verdicts_preserve_spans_and_shared_causes() {
        let cause = Arc::new(Purity::pure());
        let span = Span::new(BytePos(5), BytePos(13));
        let precision_loss_span = Span::new(BytePos(17), BytePos(23));
        let calls = SynchronousCalls {
            purity_disagreement: Some(PurityCompletionDisagreement {
                prepared: Purity::Pure {
                    dependencies: ProofDependencies::default(),
                },
                completed: Purity::NotPure {
                    reasons: vec![PurityReason {
                        rule: PurityRule::ImpureFunctionCall,
                        source_location: None,
                        detail: None,
                        author_guidance: None,
                        cause_ref: None,
                        cause: Some(cause.clone()),
                        span,
                    }],
                },
            }),
            precision_losses: vec![crate::execution_evidence::ExecutionPrecisionLoss {
                reason:
                    crate::execution_evidence::ExecutionPrecisionLossReason::RecursiveInvocation,
                reach: crate::execution_evidence::HavocReach::Bounded,
                statement_ordinal: SourceUnitOrdinal(3),
                source_location: None,
                span: precision_loss_span,
            }],
            ..SynchronousCalls::default()
        };
        let report = serde_json::to_value(&calls.precision_losses[0])
            .expect("precision-loss report serializes");
        assert!(report.get("span").is_none());
        let mut snapshot = PuritySnapshot::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut snapshot)
            .expect("execution facts snapshot");
        let cause_id = snapshot.snapshot(&cause).expect("snapshot cause");
        let graph = snapshot.finish();
        use crate::saved_proto::{ProtoConvert, proto};
        let graph_bytes = analysis_codec::encode_protobuf(&graph).expect("graph encode");
        let facts_bytes = analysis_codec::encode_protobuf(&saved.to_proto().expect("typed facts"))
            .expect("facts encode");
        let wire: proto::SavedPurityGraph =
            analysis_codec::decode_protobuf(&graph_bytes).expect("graph decode");
        let purities = RestoredPurities::restore(wire).expect("valid graph");
        let wire: proto::SavedSynchronousCalls =
            analysis_codec::decode_protobuf(&facts_bytes).expect("facts decode");
        let saved = SavedSynchronousCalls::from_proto(wire).expect("complete facts");
        let calls = saved
            .restore(&ordinals, &purities)
            .expect("valid execution facts");
        assert_eq!(calls.precision_losses[0].span, precision_loss_span);
        let disagreement = calls.purity_disagreement.expect("preserved disagreement");
        assert_eq!(disagreement.prepared, Purity::pure());
        let Purity::NotPure { reasons } = disagreement.completed else {
            panic!("preserved impure completion");
        };
        assert_eq!(reasons[0].span, span);
        assert!(Arc::ptr_eq(
            reasons[0].cause.as_ref().expect("preserved cause"),
            &purities.get(cause_id).expect("shared node"),
        ));
    }
}
