//! Saved per-chunk analysis. Verdicts are explicit saved data; unchanged
//! non-purity facts retain the existing codec and borrow their storage on write.

use std::borrow::Cow;
use std::collections::BTreeMap;

use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

use super::statement_facts::{
    ConstructorFacts, ExportLinkage, FallbackOnlyEffects, HeapContentAccesses, HeapContentCells,
    SynchronousCalls,
};
use super::*;
use crate::facts::sync_effects::SavedBindingEffectCoverage;
use crate::purity::saved::{PuritySnapshot, RestoredPurities, SavedPurity};
use crate::reports::saved::{
    SavedExecutionPrecisionLoss, SavedLocalRecordFactoryRejection, SavedLocalRecordFactoryUse,
    SavedNativeOperationInputUse, SavedReentryFreeOperationUse,
};

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

#[derive(Serialize, Deserialize)]
struct SavedConstructorFacts {
    reads: BTreeSet<SavedBinding>,
    rebinds: BTreeSet<SavedBinding>,
    calls: BTreeSet<SavedBinding>,
    unresolved_sources: BTreeSet<SavedBinding>,
    unresolved_inline_fn: bool,
}

impl SavedConstructorFacts {
    fn snapshot(facts: &ConstructorFacts, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            reads: snapshot_bindings(&facts.reads, ordinals)?,
            rebinds: snapshot_bindings(&facts.rebinds, ordinals)?,
            calls: snapshot_bindings(&facts.calls, ordinals)?,
            unresolved_sources: snapshot_bindings(&facts.unresolved_sources, ordinals)?,
            unresolved_inline_fn: facts.unresolved_inline_fn,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ConstructorFacts> {
        Ok(ConstructorFacts {
            reads: restore_bindings(self.reads, ordinals)?,
            rebinds: restore_bindings(self.rebinds, ordinals)?,
            calls: restore_bindings(self.calls, ordinals)?,
            unresolved_sources: restore_bindings(self.unresolved_sources, ordinals)?,
            unresolved_inline_fn: self.unresolved_inline_fn,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum SavedEffectCell {
    Binding(SavedBinding),
    LexicalCell {
        origin: u32,
        instance: LexicalCellId,
    },
    GlobalProp(String),
    ExecutionProgress,
    ArrayContents(u32),
    SetContents(u32),
    GlobalSymbolRegistry,
    ObjectShape(u32),
    DescriptorMapContents(u32),
    OwnProperty {
        origin: u32,
        property: String,
    },
    PrivateSlot {
        origin: u32,
        class: u32,
        name: String,
    },
}

impl SavedEffectCell {
    fn snapshot(cell: &EffectCell, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(match cell {
            EffectCell::Binding(binding) => Self::Binding(ordinals.snapshot_binding(binding)?),
            EffectCell::LexicalCell { origin, instance } => Self::LexicalCell {
                origin: *origin,
                instance: *instance,
            },
            EffectCell::GlobalProp(property) => Self::GlobalProp(property.clone()),
            EffectCell::ExecutionProgress => Self::ExecutionProgress,
            EffectCell::ArrayContents(origin) => Self::ArrayContents(*origin),
            EffectCell::SetContents(origin) => Self::SetContents(*origin),
            EffectCell::GlobalSymbolRegistry => Self::GlobalSymbolRegistry,
            EffectCell::ObjectShape(origin) => Self::ObjectShape(*origin),
            EffectCell::DescriptorMapContents(origin) => Self::DescriptorMapContents(*origin),
            EffectCell::OwnProperty { origin, property } => Self::OwnProperty {
                origin: *origin,
                property: property.clone(),
            },
            EffectCell::PrivateSlot {
                origin,
                class,
                name,
            } => Self::PrivateSlot {
                origin: *origin,
                class: *class,
                name: name.clone(),
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<EffectCell> {
        Ok(match self {
            Self::Binding(binding) => EffectCell::Binding(ordinals.restore_binding(binding)?),
            Self::LexicalCell { origin, instance } => EffectCell::LexicalCell { origin, instance },
            Self::GlobalProp(property) => EffectCell::GlobalProp(property),
            Self::ExecutionProgress => EffectCell::ExecutionProgress,
            Self::ArrayContents(origin) => EffectCell::ArrayContents(origin),
            Self::SetContents(origin) => EffectCell::SetContents(origin),
            Self::GlobalSymbolRegistry => EffectCell::GlobalSymbolRegistry,
            Self::ObjectShape(origin) => EffectCell::ObjectShape(origin),
            Self::DescriptorMapContents(origin) => EffectCell::DescriptorMapContents(origin),
            Self::OwnProperty { origin, property } => EffectCell::OwnProperty { origin, property },
            Self::PrivateSlot {
                origin,
                class,
                name,
            } => EffectCell::PrivateSlot {
                origin,
                class,
                name,
            },
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedHeapContentCells {
    allocations: BTreeSet<SavedEffectCell>,
    reads: BTreeSet<SavedEffectCell>,
    writes: BTreeSet<SavedEffectCell>,
    opaque: bool,
    observes_external_heap: bool,
}

impl SavedHeapContentCells {
    fn snapshot(cells: &HeapContentCells, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            allocations: snapshot_effect_cells(&cells.allocations, ordinals)?,
            reads: snapshot_effect_cells(&cells.reads, ordinals)?,
            writes: snapshot_effect_cells(&cells.writes, ordinals)?,
            opaque: cells.opaque,
            observes_external_heap: cells.observes_external_heap,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<HeapContentCells> {
        Ok(HeapContentCells {
            allocations: restore_effect_cells(self.allocations, ordinals)?,
            reads: restore_effect_cells(self.reads, ordinals)?,
            writes: restore_effect_cells(self.writes, ordinals)?,
            opaque: self.opaque,
            observes_external_heap: self.observes_external_heap,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedHeapContentAccesses {
    reads: BTreeSet<SavedEffectCell>,
    writes: BTreeSet<SavedEffectCell>,
}

impl SavedHeapContentAccesses {
    fn snapshot(accesses: &HeapContentAccesses, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            reads: snapshot_effect_cells(&accesses.reads, ordinals)?,
            writes: snapshot_effect_cells(&accesses.writes, ordinals)?,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<HeapContentAccesses> {
        Ok(HeapContentAccesses {
            reads: restore_effect_cells(self.reads, ordinals)?,
            writes: restore_effect_cells(self.writes, ordinals)?,
        })
    }
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

#[derive(Serialize, Deserialize)]
struct SavedFallbackOnlyEffects {
    reads: BTreeMap<SavedBinding, crate::execution_evidence::AssumedEffectReason>,
    rebinds: BTreeMap<SavedBinding, crate::execution_evidence::AssumedEffectReason>,
    uninitialized_reads: BTreeMap<SavedBinding, crate::execution_evidence::AssumedEffectReason>,
}

impl SavedFallbackOnlyEffects {
    fn snapshot(effects: &FallbackOnlyEffects, ordinals: &ContextOrdinals) -> Result<Self> {
        fn snapshot_reasons(
            reasons: &BTreeMap<Id, crate::execution_evidence::AssumedEffectReason>,
            ordinals: &ContextOrdinals,
        ) -> Result<BTreeMap<SavedBinding, crate::execution_evidence::AssumedEffectReason>>
        {
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
            reasons: BTreeMap<SavedBinding, crate::execution_evidence::AssumedEffectReason>,
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

#[derive(Serialize, Deserialize)]
pub struct SavedChunkFactAnalysis<'a> {
    facts: Vec<SavedStatementFacts<'a>>,
    top_level_await: Option<SourceUnitOrdinal>,
    redundant_purity_hints: Cow<'a, [RedundantPurityHint]>,
    redundant_pure_member_hints: Cow<'a, [RedundantPureMemberHint]>,
}

impl<'a> SavedChunkFactAnalysis<'a> {
    pub fn snapshot(
        analysis: &'a ChunkFactAnalysis,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        Ok(Self {
            facts: analysis
                .facts
                .iter()
                .map(|fact| SavedStatementFacts::snapshot(fact, ordinals, purities))
                .collect::<Result<_>>()?,
            top_level_await: analysis.top_level_await,
            redundant_purity_hints: Cow::Borrowed(&analysis.redundant_purity_hints),
            redundant_pure_member_hints: Cow::Borrowed(&analysis.redundant_pure_member_hints),
        })
    }

    pub fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
    ) -> Result<ChunkFactAnalysis> {
        Ok(ChunkFactAnalysis {
            facts: self
                .facts
                .into_iter()
                .map(|fact| fact.restore(ordinals, purities))
                .collect::<Result<_>>()?,
            top_level_await: self.top_level_await,
            redundant_purity_hints: self.redundant_purity_hints.into_owned(),
            redundant_pure_member_hints: self.redundant_pure_member_hints.into_owned(),
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedStatementFacts<'a> {
    ordinal: SourceUnitOrdinal,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    export_linkage: Option<ExportLinkage>,
    stable_iterable_producers: Cow<'a, BTreeSet<SourceUnitOrdinal>>,
    source_location: Cow<'a, Option<SourceLocation>>,
    callback_factory_barriers: Cow<'a, [CallbackFactoryBarrierFact]>,
    declared: BTreeSet<SavedBinding>,
    local_bindings: BTreeSet<SavedBinding>,
    var_declared: BTreeSet<SavedBinding>,
    var_initializers: BTreeSet<SavedBinding>,
    reads: PositionBucketed<BTreeSet<SavedBinding>>,
    rebinds: PositionBucketed<BTreeSet<SavedBinding>>,
    member_write_roots: BTreeSet<SavedBinding>,
    local_effects: BTreeSet<SavedBinding>,
    calls: PositionBucketed<BTreeSet<SavedBinding>>,
    constructor: Option<SavedConstructorFacts>,
    synchronous_calls: Option<SavedSynchronousCalls<'a>>,
    at_init_unresolved_sources: BTreeSet<SavedBinding>,
    at_init_unresolved_inline_fn: bool,
    first_order_unresolved_sources: BTreeSet<SavedBinding>,
    first_order_unresolved_inline_fn: bool,
    declares_direct_function: bool,
    global_writes: Cow<'a, BTreeSet<String>>,
    global_reads: Cow<'a, BTreeSet<String>>,
    havoc: Cow<'a, BTreeSet<DataflowOpacityCause>>,
    heap_contents: SavedHeapContentCells,
    heap_one_shot_allocation: Option<SavedEffectCell>,
    deferred_heap_allocations: BTreeSet<SavedEffectCell>,
    heap_native_integrity_uses: Cow<'a, [crate::reports::schema::NativeIntegrityUse]>,
    purity: SavedPurity,
    kind: StatementKind,
}

impl<'a> SavedStatementFacts<'a> {
    fn snapshot(
        fact: &'a StatementFacts,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        Ok(Self {
            ordinal: fact.ordinal,
            export_linkage: fact.export_linkage,
            stable_iterable_producers: Cow::Borrowed(&fact.stable_iterable_producers),
            source_location: Cow::Borrowed(&fact.source_location),
            callback_factory_barriers: Cow::Borrowed(&fact.callback_factory_barriers),
            declared: snapshot_bindings(&fact.declared, ordinals)?,
            local_bindings: snapshot_bindings(&fact.local_bindings, ordinals)?,
            var_declared: snapshot_bindings(&fact.var_declared, ordinals)?,
            var_initializers: snapshot_bindings(&fact.var_initializers, ordinals)?,
            reads: snapshot_position_bucketed(&fact.reads, ordinals)?,
            rebinds: snapshot_position_bucketed(&fact.rebinds, ordinals)?,
            member_write_roots: snapshot_bindings(&fact.member_write_roots, ordinals)?,
            local_effects: snapshot_bindings(&fact.local_effects, ordinals)?,
            calls: snapshot_position_bucketed(&fact.calls, ordinals)?,
            constructor: fact
                .constructor
                .as_ref()
                .map(|constructor| SavedConstructorFacts::snapshot(constructor, ordinals))
                .transpose()?,
            synchronous_calls: fact
                .synchronous_calls
                .as_ref()
                .map(|calls| SavedSynchronousCalls::snapshot(calls, ordinals, purities))
                .transpose()?,
            at_init_unresolved_sources: snapshot_bindings(
                &fact.at_init_unresolved_sources,
                ordinals,
            )?,
            at_init_unresolved_inline_fn: fact.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: snapshot_bindings(
                &fact.first_order_unresolved_sources,
                ordinals,
            )?,
            first_order_unresolved_inline_fn: fact.first_order_unresolved_inline_fn,
            declares_direct_function: fact.declares_direct_function,
            global_writes: Cow::Borrowed(&fact.global_writes),
            global_reads: Cow::Borrowed(&fact.global_reads),
            havoc: Cow::Borrowed(&fact.havoc),
            heap_contents: SavedHeapContentCells::snapshot(&fact.heap_contents, ordinals)?,
            heap_one_shot_allocation: fact
                .heap_one_shot_allocation
                .as_ref()
                .map(|cell| SavedEffectCell::snapshot(cell, ordinals))
                .transpose()?,
            deferred_heap_allocations: fact
                .deferred_heap_allocations
                .iter()
                .map(|cell| SavedEffectCell::snapshot(cell, ordinals))
                .collect::<Result<_>>()?,
            heap_native_integrity_uses: Cow::Borrowed(&fact.heap_native_integrity_uses),
            purity: purities.snapshot_value(&fact.purity),
            kind: fact.kind,
        })
    }

    fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
    ) -> Result<StatementFacts> {
        Ok(StatementFacts {
            ordinal: self.ordinal,
            export_linkage: self.export_linkage,
            stable_iterable_producers: self.stable_iterable_producers.into_owned(),
            source_location: self.source_location.into_owned(),
            callback_factory_barriers: self.callback_factory_barriers.into_owned(),
            declared: restore_bindings(self.declared, ordinals)?,
            local_bindings: restore_bindings(self.local_bindings, ordinals)?,
            var_declared: restore_bindings(self.var_declared, ordinals)?,
            var_initializers: restore_bindings(self.var_initializers, ordinals)?,
            reads: restore_position_bucketed(self.reads, ordinals)?,
            rebinds: restore_position_bucketed(self.rebinds, ordinals)?,
            member_write_roots: restore_bindings(self.member_write_roots, ordinals)?,
            local_effects: restore_bindings(self.local_effects, ordinals)?,
            calls: restore_position_bucketed(self.calls, ordinals)?,
            constructor: self
                .constructor
                .map(|constructor| constructor.restore(ordinals))
                .transpose()?,
            synchronous_calls: self
                .synchronous_calls
                .map(|calls| calls.restore(ordinals, purities))
                .transpose()?,
            at_init_unresolved_sources: restore_bindings(
                self.at_init_unresolved_sources,
                ordinals,
            )?,
            at_init_unresolved_inline_fn: self.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: restore_bindings(
                self.first_order_unresolved_sources,
                ordinals,
            )?,
            first_order_unresolved_inline_fn: self.first_order_unresolved_inline_fn,
            declares_direct_function: self.declares_direct_function,
            global_writes: self.global_writes.into_owned(),
            global_reads: self.global_reads.into_owned(),
            havoc: self.havoc.into_owned(),
            heap_contents: self.heap_contents.restore(ordinals)?,
            heap_one_shot_allocation: self
                .heap_one_shot_allocation
                .map(|cell| cell.restore(ordinals))
                .transpose()?,
            deferred_heap_allocations: self
                .deferred_heap_allocations
                .into_iter()
                .map(|cell| cell.restore(ordinals))
                .collect::<Result<_>>()?,
            heap_native_integrity_uses: self.heap_native_integrity_uses.into_owned(),
            purity: purities.restore_value(self.purity)?,
            kind: self.kind,
        })
    }
}

/// Synchronous execution facts with every runtime binding converted explicitly.
#[derive(Serialize, Deserialize)]
struct SavedSynchronousCalls<'a> {
    initializer_binding_effects:
        Cow<'a, Option<Box<crate::reports::schema::InitializerBindingEffectsUse>>>,
    completed_initialization:
        Cow<'a, Option<super::sync_effects::noninvoking_initialization::CompletedInitialization>>,
    #[serde(default)]
    root_normal_completion:
        Cow<'a, Option<super::sync_effects::noninvoking_initialization::RootNormalCompletion>>,
    initialization_coverage_refusal:
        Cow<'a, Option<crate::facts::OwnerInitializationCoverageRefusalReason>>,
    evaluation_effects: Cow<'a, super::evaluation_effects::EvaluationEffects>,
    local_record_factory_uses: Vec<SavedLocalRecordFactoryUse<'a>>,
    local_record_factory_rejections: Vec<SavedLocalRecordFactoryRejection<'a>>,
    local_record_factory_purity: Cow<'a, Option<super::LocalRecordFactoryPurityCertificate>>,
    reads: BTreeSet<(SavedBinding, SourceUnitOrdinal)>,
    rebinds: BTreeSet<(SavedBinding, SourceUnitOrdinal)>,
    uninitialized_reads: BTreeSet<SavedBinding>,
    fallback_only: SavedFallbackOnlyEffects,
    complete: bool,
    binding_effect_coverage: SavedBindingEffectCoverage<'a>,
    settled_array_elements: BTreeMap<SavedBinding, super::SettledElements>,
    global_object_aliases: Option<BTreeSet<SavedBinding>>,
    heap_contents: SavedHeapContentCells,
    non_assumed_heap_accesses: SavedHeapContentAccesses,
    precision_losses: Vec<SavedExecutionPrecisionLoss<'a>>,
    native_integrity_checks: Cow<'a, [crate::reports::schema::NativeIntegrityCheck]>,
    native_prototype_property_checks:
        Cow<'a, [crate::reports::schema::NativePrototypePropertyCheck]>,
    native_prefix_observations: Cow<'a, [crate::reports::schema::NativePrefixObservation]>,
    native_integrity_uses: Cow<'a, [crate::reports::schema::NativeIntegrityUse]>,
    native_operation_input_uses: Vec<SavedNativeOperationInputUse<'a>>,
    inference_uses: Cow<'a, [crate::analysis_provenance::InferenceUse]>,
    callback_factory_uses: Cow<'a, [super::CallbackFactoryUseFact]>,
    returned_record_uses: Cow<'a, [super::ReturnedRecordUseFact]>,
    author_host_contract_uses: Vec<SavedReentryFreeOperationUse<'a>>,
    purity_disagreement: Option<SavedPurityDisagreement>,
}

#[derive(Serialize, Deserialize)]
struct SavedPurityDisagreement {
    prepared: SavedPurity,
    completed: SavedPurity,
}

impl<'a> SavedSynchronousCalls<'a> {
    fn snapshot(
        calls: &'a SynchronousCalls,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        Ok(Self {
            initializer_binding_effects: Cow::Borrowed(&calls.initializer_binding_effects),
            completed_initialization: Cow::Borrowed(&calls.completed_initialization),
            root_normal_completion: Cow::Borrowed(&calls.root_normal_completion),
            initialization_coverage_refusal: Cow::Borrowed(&calls.initialization_coverage_refusal),
            evaluation_effects: Cow::Borrowed(&calls.evaluation_effects),
            local_record_factory_uses: calls
                .local_record_factory_uses
                .iter()
                .map(SavedLocalRecordFactoryUse::snapshot)
                .collect(),
            local_record_factory_rejections: calls
                .local_record_factory_rejections
                .iter()
                .map(SavedLocalRecordFactoryRejection::snapshot)
                .collect(),
            local_record_factory_purity: Cow::Borrowed(&calls.local_record_factory_purity),
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
            heap_contents: SavedHeapContentCells::snapshot(&calls.heap_contents, ordinals)?,
            non_assumed_heap_accesses: SavedHeapContentAccesses::snapshot(
                &calls.non_assumed_heap_accesses,
                ordinals,
            )?,
            precision_losses: calls
                .precision_losses
                .iter()
                .map(SavedExecutionPrecisionLoss::snapshot)
                .collect(),
            native_integrity_checks: Cow::Borrowed(&calls.native_integrity_checks),
            native_prototype_property_checks: Cow::Borrowed(
                &calls.native_prototype_property_checks,
            ),
            native_prefix_observations: Cow::Borrowed(&calls.native_prefix_observations),
            native_integrity_uses: Cow::Borrowed(&calls.native_integrity_uses),
            native_operation_input_uses: calls
                .native_operation_input_uses
                .iter()
                .map(SavedNativeOperationInputUse::snapshot)
                .collect(),
            inference_uses: Cow::Borrowed(&calls.inference_uses),
            callback_factory_uses: Cow::Borrowed(&calls.callback_factory_uses),
            returned_record_uses: Cow::Borrowed(&calls.returned_record_uses),
            author_host_contract_uses: calls
                .author_host_contract_uses
                .iter()
                .map(SavedReentryFreeOperationUse::snapshot)
                .collect(),
            purity_disagreement: calls.purity_disagreement.as_ref().map(|disagreement| {
                SavedPurityDisagreement {
                    prepared: purities.snapshot_value(&disagreement.prepared),
                    completed: purities.snapshot_value(&disagreement.completed),
                }
            }),
        })
    }

    fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
    ) -> Result<SynchronousCalls> {
        Ok(SynchronousCalls {
            initializer_binding_effects: self.initializer_binding_effects.into_owned(),
            completed_initialization: self.completed_initialization.into_owned(),
            root_normal_completion: self.root_normal_completion.into_owned(),
            initialization_coverage_refusal: self.initialization_coverage_refusal.into_owned(),
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
            local_record_factory_uses: self
                .local_record_factory_uses
                .into_iter()
                .map(SavedLocalRecordFactoryUse::restore)
                .collect(),
            local_record_factory_rejections: self
                .local_record_factory_rejections
                .into_iter()
                .map(SavedLocalRecordFactoryRejection::restore)
                .collect(),
            local_record_factory_purity: self.local_record_factory_purity.into_owned(),
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
            heap_contents: self.heap_contents.restore(ordinals)?,
            non_assumed_heap_accesses: self.non_assumed_heap_accesses.restore(ordinals)?,
            precision_losses: self
                .precision_losses
                .into_iter()
                .map(SavedExecutionPrecisionLoss::restore)
                .collect(),
            native_integrity_checks: self.native_integrity_checks.into_owned(),
            native_prototype_property_checks: self.native_prototype_property_checks.into_owned(),
            native_prefix_observations: self.native_prefix_observations.into_owned(),
            native_integrity_uses: self.native_integrity_uses.into_owned(),
            native_operation_input_uses: self
                .native_operation_input_uses
                .into_iter()
                .map(SavedNativeOperationInputUse::restore)
                .collect(),
            inference_uses: self.inference_uses.into_owned(),
            callback_factory_uses: self.callback_factory_uses.into_owned(),
            returned_record_uses: self.returned_record_uses.into_owned(),
            author_host_contract_uses: self
                .author_host_contract_uses
                .into_iter()
                .map(SavedReentryFreeOperationUse::restore)
                .collect(),
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
    fn saved_synchronous_calls_require_binding_effect_authority() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let mut purities = PuritySnapshot::default();
        let calls = SynchronousCalls::default();
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
            .expect("default facts snapshot");
        let mut encoded = serde_json::to_value(saved).expect("saved facts serialize");
        serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded.clone())
            .expect("complete saved facts deserialize");
        encoded
            .as_object_mut()
            .expect("saved synchronous facts are a struct")
            .remove("binding_effect_coverage");
        assert!(serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded).is_err());
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
        let cause_id = snapshot.snapshot(&cause);
        let graph = snapshot.finish();
        let bytes = rmp_serde::to_vec_named(&(graph, saved)).expect("ordinary encode");
        let (graph, saved): (
            crate::purity::saved::SavedPurityGraph,
            SavedSynchronousCalls<'_>,
        ) = rmp_serde::from_slice(&bytes).expect("ordinary decode");
        let purities = graph.restore().expect("valid graph");
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
