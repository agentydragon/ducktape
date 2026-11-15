//! Saved per-chunk analysis. Snapshot into generated protobuf while preserving
//! explicit binding, effect-cell, and shared-purity conversions.

use std::collections::BTreeMap;

use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::{Result, ensure};
use source_identity::SourceUnitOrdinal;

use super::statement_facts::{
    ConstructorFacts, FallbackOnlyEffects, HeapContentAccesses, HeapContentCells, SynchronousCalls,
};
use super::*;
use crate::execution_evidence::AssumedEffectReason;
use crate::facts::sync_effects::BindingEffectCoverage;
use crate::purity::saved::{PuritySnapshot, RestoredPurities};
use crate::saved_proto::{ProtoConvert, proto, required};

fn snapshot_bindings(
    bindings: &BTreeSet<Id>,
    ordinals: &ContextOrdinals,
) -> Result<Vec<proto::SavedBinding>> {
    let mut bindings = bindings
        .iter()
        .map(|binding| ordinals.snapshot_binding(binding)?.to_proto())
        .collect::<Result<Vec<_>>>()?;
    bindings.sort();
    Ok(bindings)
}

fn restore_bindings(
    bindings: Vec<proto::SavedBinding>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<Id>> {
    BTreeSet::<SavedBinding>::from_proto(bindings)?
        .into_iter()
        .map(|binding| ordinals.restore_binding(binding))
        .collect()
}

fn snapshot_position_bucketed(
    buckets: &PositionBucketed<BTreeSet<Id>>,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedBindingEffects> {
    Ok(proto::SavedBindingEffects {
        eager: snapshot_bindings(&buckets.eager, ordinals)?,
        lazy: snapshot_bindings(&buckets.lazy, ordinals)?,
        first_order_lazy: snapshot_bindings(&buckets.first_order_lazy, ordinals)?,
    })
}

fn restore_position_bucketed(
    buckets: proto::SavedBindingEffects,
    ordinals: &ContextOrdinals,
) -> Result<PositionBucketed<BTreeSet<Id>>> {
    Ok(PositionBucketed {
        eager: restore_bindings(buckets.eager, ordinals)?,
        lazy: restore_bindings(buckets.lazy, ordinals)?,
        first_order_lazy: restore_bindings(buckets.first_order_lazy, ordinals)?,
    })
}

fn snapshot_binding_effect_sites(
    sites: &BTreeSet<(Id, SourceUnitOrdinal)>,
    ordinals: &ContextOrdinals,
) -> Result<Vec<proto::SavedBindingEffectSite>> {
    let mut sites = sites
        .iter()
        .map(|(binding, source_unit)| Ok((ordinals.snapshot_binding(binding)?, *source_unit)))
        .collect::<Result<Vec<_>>>()?;
    sites.sort();
    sites
        .into_iter()
        .map(|(binding, source_unit)| {
            Ok(proto::SavedBindingEffectSite {
                binding: Some(binding.to_proto()?),
                source_unit: Some(source_unit.to_proto()?),
            })
        })
        .collect()
}

fn restore_binding_effect_sites(
    sites: Vec<proto::SavedBindingEffectSite>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<(Id, SourceUnitOrdinal)>> {
    let mut restored = BTreeSet::new();
    for site in sites {
        let site = (
            ordinals.restore_binding(SavedBinding::from_proto(required(
                site.binding,
                "SavedBindingEffectSite.binding",
            )?)?)?,
            SourceUnitOrdinal::from_proto(required(
                site.source_unit,
                "SavedBindingEffectSite.source_unit",
            )?)?,
        );
        ensure!(restored.insert(site), "duplicate saved binding effect site");
    }
    Ok(restored)
}

fn snapshot_synchronous_prefix_effects(
    effects: &(BTreeSet<Id>, BTreeSet<Id>),
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedSynchronousPrefixEffects> {
    Ok(proto::SavedSynchronousPrefixEffects {
        reads: snapshot_bindings(&effects.0, ordinals)?,
        rebinds: snapshot_bindings(&effects.1, ordinals)?,
    })
}

fn restore_synchronous_prefix_effects(
    value: proto::SavedSynchronousPrefixEffects,
    ordinals: &ContextOrdinals,
) -> Result<(BTreeSet<Id>, BTreeSet<Id>)> {
    Ok((
        restore_bindings(value.reads, ordinals)?,
        restore_bindings(value.rebinds, ordinals)?,
    ))
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

fn snapshot_constructor(
    facts: &ConstructorFacts,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedConstructorFacts> {
    Ok(proto::SavedConstructorFacts {
        reads: snapshot_bindings(&facts.reads, ordinals)?,
        rebinds: snapshot_bindings(&facts.rebinds, ordinals)?,
        calls: snapshot_bindings(&facts.calls, ordinals)?,
        unresolved_sources: snapshot_bindings(&facts.unresolved_sources, ordinals)?,
        unresolved_inline_fn: Some(facts.unresolved_inline_fn),
    })
}

fn restore_constructor(
    value: proto::SavedConstructorFacts,
    ordinals: &ContextOrdinals,
) -> Result<ConstructorFacts> {
    Ok(ConstructorFacts {
        reads: restore_bindings(value.reads, ordinals)?,
        rebinds: restore_bindings(value.rebinds, ordinals)?,
        calls: restore_bindings(value.calls, ordinals)?,
        unresolved_sources: restore_bindings(value.unresolved_sources, ordinals)?,
        unresolved_inline_fn: required(
            value.unresolved_inline_fn,
            "SavedConstructorFacts.unresolved_inline_fn",
        )?,
    })
}

fn snapshot_heap_cells(
    cells: &HeapContentCells,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedHeapContentCells> {
    Ok(proto::SavedHeapContentCells {
        allocations: snapshot_effect_cells(&cells.allocations, ordinals)?,
        reads: snapshot_effect_cells(&cells.reads, ordinals)?,
        writes: snapshot_effect_cells(&cells.writes, ordinals)?,
        opaque: Some(cells.opaque),
    })
}

fn restore_heap_cells(
    value: proto::SavedHeapContentCells,
    ordinals: &ContextOrdinals,
) -> Result<HeapContentCells> {
    Ok(HeapContentCells {
        allocations: restore_effect_cells(value.allocations, ordinals)?,
        reads: restore_effect_cells(value.reads, ordinals)?,
        writes: restore_effect_cells(value.writes, ordinals)?,
        opaque: required(value.opaque, "SavedHeapContentCells.opaque")?,
    })
}

fn snapshot_heap_accesses(
    accesses: &HeapContentAccesses,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedHeapContentAccesses> {
    Ok(proto::SavedHeapContentAccesses {
        reads: snapshot_effect_cells(&accesses.reads, ordinals)?,
        writes: snapshot_effect_cells(&accesses.writes, ordinals)?,
    })
}

fn restore_heap_accesses(
    value: proto::SavedHeapContentAccesses,
    ordinals: &ContextOrdinals,
) -> Result<HeapContentAccesses> {
    Ok(HeapContentAccesses {
        reads: restore_effect_cells(value.reads, ordinals)?,
        writes: restore_effect_cells(value.writes, ordinals)?,
    })
}

fn snapshot_effect_cells(
    cells: &BTreeSet<EffectCell>,
    ordinals: &ContextOrdinals,
) -> Result<Vec<proto::SavedEffectCell>> {
    let mut cells = cells
        .iter()
        .map(|cell| {
            use proto::saved_effect_cell::{OwnProperty, Value};
            Ok(proto::SavedEffectCell {
                value: Some(match cell {
                    EffectCell::Binding(binding) => {
                        Value::Binding(ordinals.snapshot_binding(binding)?.to_proto()?)
                    }
                    EffectCell::GlobalProp(property) => Value::GlobalProp(property.clone()),
                    EffectCell::ArrayContents(origin) => Value::ArrayContents(*origin),
                    EffectCell::SetContents(origin) => Value::SetContents(*origin),
                    EffectCell::ObjectShape(origin) => Value::ObjectShape(*origin),
                    EffectCell::OwnProperty { origin, property } => {
                        Value::OwnProperty(OwnProperty {
                            origin: Some(*origin),
                            property: Some(property.clone()),
                        })
                    }
                }),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    cells.sort();
    Ok(cells)
}

fn restore_effect_cells(
    cells: Vec<proto::SavedEffectCell>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeSet<EffectCell>> {
    use proto::saved_effect_cell::Value;
    let mut restored = BTreeSet::new();
    for cell in cells {
        let cell = match required(cell.value, "SavedEffectCell.value")? {
            Value::Binding(binding) => {
                EffectCell::Binding(ordinals.restore_binding(SavedBinding::from_proto(binding)?)?)
            }
            Value::GlobalProp(property) => EffectCell::GlobalProp(property),
            Value::ArrayContents(origin) => EffectCell::ArrayContents(origin),
            Value::SetContents(origin) => EffectCell::SetContents(origin),
            Value::ObjectShape(origin) => EffectCell::ObjectShape(origin),
            Value::OwnProperty(value) => EffectCell::OwnProperty {
                origin: required(value.origin, "SavedEffectCell.own_property.origin")?,
                property: required(value.property, "SavedEffectCell.own_property.property")?,
            },
        };
        ensure!(restored.insert(cell), "duplicate saved effect cell");
    }
    Ok(restored)
}

fn snapshot_fallback_only_effects(
    effects: &FallbackOnlyEffects,
    ordinals: &ContextOrdinals,
) -> Result<proto::SavedFallbackOnlyEffects> {
    fn snapshot_reasons(
        reasons: &BTreeMap<Id, AssumedEffectReason>,
        ordinals: &ContextOrdinals,
    ) -> Result<Vec<proto::SavedAssumedBindingEffect>> {
        reasons
            .iter()
            .map(|(binding, reason)| Ok((ordinals.snapshot_binding(binding)?, reason)))
            .collect::<Result<Vec<_>>>()
            .map(|mut reasons| {
                reasons.sort_by(|left, right| left.0.cmp(&right.0));
                reasons
                    .into_iter()
                    .map(|(binding, reason)| {
                        Ok(proto::SavedAssumedBindingEffect {
                            binding: Some(binding.to_proto()?),
                            reason: Some(reason.to_proto()?),
                        })
                    })
                    .collect()
            })?
    }

    Ok(proto::SavedFallbackOnlyEffects {
        reads: snapshot_reasons(&effects.reads, ordinals)?,
        rebinds: snapshot_reasons(&effects.rebinds, ordinals)?,
        uninitialized_reads: snapshot_reasons(&effects.uninitialized_reads, ordinals)?,
    })
}

fn restore_fallback_only_effects(
    effects: proto::SavedFallbackOnlyEffects,
    ordinals: &ContextOrdinals,
) -> Result<FallbackOnlyEffects> {
    fn restore_reasons(
        reasons: Vec<proto::SavedAssumedBindingEffect>,
        ordinals: &ContextOrdinals,
    ) -> Result<BTreeMap<Id, AssumedEffectReason>> {
        let mut restored = BTreeMap::new();
        for entry in reasons {
            let binding = ordinals.restore_binding(SavedBinding::from_proto(required(
                entry.binding,
                "SavedAssumedBindingEffect.binding",
            )?)?)?;
            let reason = AssumedEffectReason::from_proto(required(
                entry.reason,
                "SavedAssumedBindingEffect.reason",
            )?)?;
            ensure!(
                restored.insert(binding, reason).is_none(),
                "duplicate saved fallback-only binding"
            );
        }
        Ok(restored)
    }

    Ok(FallbackOnlyEffects {
        reads: restore_reasons(effects.reads, ordinals)?,
        rebinds: restore_reasons(effects.rebinds, ordinals)?,
        uninitialized_reads: restore_reasons(effects.uninitialized_reads, ordinals)?,
    })
}

fn snapshot_settled_array_elements(
    elements: &BTreeMap<Id, SettledElements>,
    ordinals: &ContextOrdinals,
) -> Result<Vec<proto::SavedSettledArrayElements>> {
    let mut elements = elements
        .iter()
        .map(|(binding, elements)| Ok((ordinals.snapshot_binding(binding)?, elements)))
        .collect::<Result<Vec<_>>>()?;
    elements.sort_by(|left, right| left.0.cmp(&right.0));
    elements
        .into_iter()
        .map(|(binding, elements)| {
            Ok(proto::SavedSettledArrayElements {
                binding: Some(binding.to_proto()?),
                elements: Some(elements.to_proto()?),
            })
        })
        .collect()
}

fn restore_settled_array_elements(
    elements: Vec<proto::SavedSettledArrayElements>,
    ordinals: &ContextOrdinals,
) -> Result<BTreeMap<Id, SettledElements>> {
    let mut restored = BTreeMap::new();
    for entry in elements {
        let binding = ordinals.restore_binding(SavedBinding::from_proto(required(
            entry.binding,
            "SavedSettledArrayElements.binding",
        )?)?)?;
        let elements = SettledElements::from_proto(required(
            entry.elements,
            "SavedSettledArrayElements.elements",
        )?)?;
        ensure!(
            restored.insert(binding, elements).is_none(),
            "duplicate settled-array binding"
        );
    }
    Ok(restored)
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
        export_linkage: fact.export_linkage.map(|value| value as i32),
        stable_iterable_producers: fact.stable_iterable_producers.to_proto()?,
        source_location: fact.source_location.to_proto()?,
        callback_factory_barriers: fact.callback_factory_barriers.to_proto()?,
        declared: snapshot_bindings(&fact.declared, ordinals)?,
        local_bindings: snapshot_bindings(&fact.local_bindings, ordinals)?,
        var_declared: snapshot_bindings(&fact.var_declared, ordinals)?,
        var_initializers: snapshot_bindings(&fact.var_initializers, ordinals)?,
        reads: Some(snapshot_position_bucketed(&fact.reads, ordinals)?),
        rebinds: Some(snapshot_position_bucketed(&fact.rebinds, ordinals)?),
        member_write_roots: snapshot_bindings(&fact.member_write_roots, ordinals)?,
        local_effects: snapshot_bindings(&fact.local_effects, ordinals)?,
        calls: Some(snapshot_position_bucketed(&fact.calls, ordinals)?),
        constructor: fact
            .constructor
            .as_ref()
            .map(|constructor| snapshot_constructor(constructor, ordinals))
            .transpose()?,
        synchronous_calls: fact
            .synchronous_calls
            .as_ref()
            .map(|calls| snapshot_synchronous_calls(calls, ordinals, purities))
            .transpose()?,
        synchronous_prefix_effects: fact
            .synchronous_prefix_effects
            .as_ref()
            .map(|effects| snapshot_synchronous_prefix_effects(effects, ordinals))
            .transpose()?,
        at_init_unresolved_sources: snapshot_bindings(&fact.at_init_unresolved_sources, ordinals)?,
        at_init_unresolved_inline_fn: Some(fact.at_init_unresolved_inline_fn),
        first_order_unresolved_sources: snapshot_bindings(
            &fact.first_order_unresolved_sources,
            ordinals,
        )?,
        first_order_unresolved_inline_fn: Some(fact.first_order_unresolved_inline_fn),
        declares_direct_function: Some(fact.declares_direct_function),
        global_writes: fact.global_writes.to_proto()?,
        global_reads: fact.global_reads.to_proto()?,
        havoc: fact.havoc.iter().map(|value| *value as i32).collect(),
        trusted_summary_discharged: fact
            .trusted_summary_discharged
            .iter()
            .map(|value| *value as i32)
            .collect(),
        heap_contents: Some(snapshot_heap_cells(&fact.heap_contents, ordinals)?),
        heap_one_shot_allocations: snapshot_effect_cells(
            &fact.heap_one_shot_allocations,
            ordinals,
        )?,
        deferred_heap_allocations: snapshot_effect_cells(
            &fact.deferred_heap_allocations,
            ordinals,
        )?,
        heap_native_integrity_uses: fact.heap_native_integrity_uses.to_proto()?,
        purity: Some(purities.snapshot_value(&fact.purity)?),
        kind: Some(fact.kind as i32),
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
        export_linkage: value
            .export_linkage
            .map(ExportLinkage::try_from)
            .transpose()?,
        stable_iterable_producers: BTreeSet::from_proto(value.stable_iterable_producers)?,
        source_location: Option::from_proto(value.source_location)?,
        callback_factory_barriers: Vec::from_proto(value.callback_factory_barriers)?,
        declared: restore_bindings(value.declared, ordinals)?,
        local_bindings: restore_bindings(value.local_bindings, ordinals)?,
        var_declared: restore_bindings(value.var_declared, ordinals)?,
        var_initializers: restore_bindings(value.var_initializers, ordinals)?,
        reads: restore_position_bucketed(
            required(value.reads, "SavedStatementFacts.reads")?,
            ordinals,
        )?,
        rebinds: restore_position_bucketed(
            required(value.rebinds, "SavedStatementFacts.rebinds")?,
            ordinals,
        )?,
        member_write_roots: restore_bindings(value.member_write_roots, ordinals)?,
        local_effects: restore_bindings(value.local_effects, ordinals)?,
        calls: restore_position_bucketed(
            required(value.calls, "SavedStatementFacts.calls")?,
            ordinals,
        )?,
        constructor: value
            .constructor
            .map(|constructor| restore_constructor(constructor, ordinals))
            .transpose()?,
        synchronous_calls: value
            .synchronous_calls
            .map(|calls| restore_synchronous_calls(calls, ordinals, purities))
            .transpose()?,
        synchronous_prefix_effects: value
            .synchronous_prefix_effects
            .map(|effects| restore_synchronous_prefix_effects(effects, ordinals))
            .transpose()?,
        at_init_unresolved_sources: restore_bindings(value.at_init_unresolved_sources, ordinals)?,
        at_init_unresolved_inline_fn: required(
            value.at_init_unresolved_inline_fn,
            "SavedStatementFacts.at_init_unresolved_inline_fn",
        )?,
        first_order_unresolved_sources: restore_bindings(
            value.first_order_unresolved_sources,
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
        havoc: value
            .havoc
            .into_iter()
            .map(|value| Ok(DataflowOpacityCause::try_from(value)?))
            .collect::<Result<_>>()?,
        trusted_summary_discharged: value
            .trusted_summary_discharged
            .into_iter()
            .map(|value| Ok(DataflowOpacityCause::try_from(value)?))
            .collect::<Result<_>>()?,
        heap_contents: restore_heap_cells(
            required(value.heap_contents, "SavedStatementFacts.heap_contents")?,
            ordinals,
        )?,
        heap_one_shot_allocations: restore_effect_cells(value.heap_one_shot_allocations, ordinals)?,
        deferred_heap_allocations: restore_effect_cells(value.deferred_heap_allocations, ordinals)?,
        heap_native_integrity_uses: Vec::from_proto(value.heap_native_integrity_uses)?,
        purity: purities.restore_value(required(value.purity, "SavedStatementFacts.purity")?)?,
        kind: StatementKind::try_from(required(value.kind, "SavedStatementFacts.kind")?)?,
    })
}

fn snapshot_synchronous_calls<'a>(
    calls: &'a SynchronousCalls,
    ordinals: &ContextOrdinals,
    purities: &mut PuritySnapshot<'a>,
) -> Result<proto::SavedSynchronousCalls> {
    Ok(proto::SavedSynchronousCalls {
        initializer_binding_effects: calls.initializer_binding_effects.to_proto()?,
        initializer_external_effects: calls.initializer_external_effects.to_proto()?,
        completed_initialization: calls.completed_initialization.to_proto()?,
        initialization_coverage_refusal: calls
            .initialization_coverage_refusal
            .map(|value| value as i32),
        root_attempt: calls.root_attempt.to_proto()?,
        evaluation_effects: Some(calls.evaluation_effects.to_proto()?),
        local_record_factory_uses: calls.local_record_factory_uses.to_proto()?,
        local_record_factory_rejections: calls.local_record_factory_rejections.to_proto()?,
        local_record_factory_purity: calls.local_record_factory_purity.to_proto()?,
        proven_private_own_field_reads: calls.proven_private_own_field_reads.to_proto()?,
        local_record_factory_heap_call: calls.local_record_factory_heap_call.to_proto()?,
        reads: snapshot_binding_effect_sites(&calls.reads, ordinals)?,
        rebinds: snapshot_binding_effect_sites(&calls.rebinds, ordinals)?,
        uninitialized_reads: snapshot_bindings(&calls.uninitialized_reads, ordinals)?,
        fallback_only: Some(snapshot_fallback_only_effects(
            &calls.fallback_only,
            ordinals,
        )?),
        complete: Some(calls.complete),
        binding_effect_coverage: Some(calls.binding_effect_coverage.snapshot_proto(ordinals)?),
        settled_array_elements: snapshot_settled_array_elements(
            &calls.settled_array_elements,
            ordinals,
        )?,
        global_object_aliases: calls
            .global_object_aliases
            .as_ref()
            .map(|bindings| -> Result<_> {
                Ok(proto::SavedGlobalObjectAliases {
                    bindings: snapshot_bindings(bindings, ordinals)?,
                })
            })
            .transpose()?,
        heap_contents: Some(snapshot_heap_cells(&calls.heap_contents, ordinals)?),
        non_assumed_heap_accesses: Some(snapshot_heap_accesses(
            &calls.non_assumed_heap_accesses,
            ordinals,
        )?),
        precision_losses: calls.precision_losses.to_proto()?,
        native_integrity_checks: calls.native_integrity_checks.to_proto()?,
        native_prototype_property_checks: calls.native_prototype_property_checks.to_proto()?,
        native_prefix_observations: calls.native_prefix_observations.to_proto()?,
        native_integrity_uses: calls.native_integrity_uses.to_proto()?,
        native_operation_input_uses: calls.native_operation_input_uses.to_proto()?,
        imported_primitive_array_spread_input_uses: calls
            .imported_primitive_array_spread_input_uses
            .to_proto()?,
        inference_uses: calls.inference_uses.to_proto()?,
        callback_factory_uses: calls.callback_factory_uses.to_proto()?,
        returned_record_uses: calls.returned_record_uses.to_proto()?,
        author_host_contract_uses: calls.author_host_contract_uses.to_proto()?,
        purity_disagreement: calls
            .purity_disagreement
            .as_ref()
            .map(|disagreement| -> Result<_> {
                Ok(proto::SavedPurityDisagreement {
                    prepared: Some(purities.snapshot_value(&disagreement.prepared)?),
                    completed: Some(purities.snapshot_value(&disagreement.completed)?),
                })
            })
            .transpose()?,
    })
}

fn restore_synchronous_calls(
    value: proto::SavedSynchronousCalls,
    ordinals: &ContextOrdinals,
    purities: &RestoredPurities,
) -> Result<SynchronousCalls> {
    Ok(SynchronousCalls {
        initializer_binding_effects: Option::from_proto(value.initializer_binding_effects)?,
        initializer_external_effects: Option::from_proto(value.initializer_external_effects)?,
        completed_initialization: Option::from_proto(value.completed_initialization)?,
        initialization_coverage_refusal: value
            .initialization_coverage_refusal
            .map(crate::facts::OwnerInitializationCoverageRefusalReason::try_from)
            .transpose()?,
        root_attempt: Option::from_proto(value.root_attempt)?,
        evaluation_effects: crate::facts::evaluation_effects::EvaluationEffects::from_proto(
            required(
                value.evaluation_effects,
                "SavedSynchronousCalls.evaluation_effects",
            )?,
        )?,
        purity_disagreement: value
            .purity_disagreement
            .map(|disagreement| -> Result<_> {
                Ok(crate::reports::schema::PurityCompletionDisagreement {
                    prepared: purities.restore_value(required(
                        disagreement.prepared,
                        "SavedPurityDisagreement.prepared",
                    )?)?,
                    completed: purities.restore_value(required(
                        disagreement.completed,
                        "SavedPurityDisagreement.completed",
                    )?)?,
                })
            })
            .transpose()?,
        local_record_factory_uses: Vec::from_proto(value.local_record_factory_uses)?,
        local_record_factory_rejections: Vec::from_proto(value.local_record_factory_rejections)?,
        local_record_factory_purity: Option::from_proto(value.local_record_factory_purity)?,
        proven_private_own_field_reads: Vec::from_proto(value.proven_private_own_field_reads)?,
        local_record_factory_heap_call: Option::from_proto(value.local_record_factory_heap_call)?,
        reads: restore_binding_effect_sites(value.reads, ordinals)?,
        rebinds: restore_binding_effect_sites(value.rebinds, ordinals)?,
        uninitialized_reads: restore_bindings(value.uninitialized_reads, ordinals)?,
        fallback_only: restore_fallback_only_effects(
            required(value.fallback_only, "SavedSynchronousCalls.fallback_only")?,
            ordinals,
        )?,
        complete: required(value.complete, "SavedSynchronousCalls.complete")?,
        binding_effect_coverage: BindingEffectCoverage::restore_proto(
            required(
                value.binding_effect_coverage,
                "SavedSynchronousCalls.binding_effect_coverage",
            )?,
            ordinals,
        )?,
        settled_array_elements: restore_settled_array_elements(
            value.settled_array_elements,
            ordinals,
        )?,
        global_object_aliases: value
            .global_object_aliases
            .map(|aliases| restore_bindings(aliases.bindings, ordinals))
            .transpose()?,
        heap_contents: restore_heap_cells(
            required(value.heap_contents, "SavedSynchronousCalls.heap_contents")?,
            ordinals,
        )?,
        non_assumed_heap_accesses: restore_heap_accesses(
            required(
                value.non_assumed_heap_accesses,
                "SavedSynchronousCalls.non_assumed_heap_accesses",
            )?,
            ordinals,
        )?,
        precision_losses: Vec::from_proto(value.precision_losses)?,
        native_integrity_checks: Vec::from_proto(value.native_integrity_checks)?,
        native_prototype_property_checks: Vec::from_proto(value.native_prototype_property_checks)?,
        native_prefix_observations: Vec::from_proto(value.native_prefix_observations)?,
        native_integrity_uses: Vec::from_proto(value.native_integrity_uses)?,
        native_operation_input_uses: Vec::from_proto(value.native_operation_input_uses)?,
        imported_primitive_array_spread_input_uses: Vec::from_proto(
            value.imported_primitive_array_spread_input_uses,
        )?,
        inference_uses: Vec::from_proto(value.inference_uses)?,
        callback_factory_uses: Vec::from_proto(value.callback_factory_uses)?,
        returned_record_uses: Vec::from_proto(value.returned_record_uses)?,
        author_host_contract_uses: Vec::from_proto(value.author_host_contract_uses)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProofDependencies;
    use crate::facts::OwnerInitializationCoverageRefusalReason as Reason;
    use crate::purity::{PurityReason, PurityRule};
    use crate::reports::schema::PurityCompletionDisagreement;
    use std::sync::Arc;
    use swc_common::{BytePos, GLOBALS, Globals, Mark, Span, SyntaxContext};

    #[test]
    fn generated_initialization_refusal_reason_keeps_serde_names() {
        let reasons = [
            (Reason::UnsupportedOwnerKind, "unsupported_owner_kind"),
            (Reason::ExportedOwner, "exported_owner"),
            (Reason::SplitDeclaration, "split_declaration"),
            (Reason::UnsupportedBindingShape, "unsupported_binding_shape"),
            (Reason::MissingInitializer, "missing_initializer"),
            (Reason::UnsupportedInitializer, "unsupported_initializer"),
            (Reason::OwnerBindingMismatch, "owner_binding_mismatch"),
            (
                Reason::InitializerResultUnproved,
                "initializer_result_unproved",
            ),
            (Reason::UnprovedFieldStore, "unproved_field_store"),
            (Reason::ImportedBindingStore, "imported_binding_store"),
            (Reason::ConstBindingStore, "const_binding_store"),
            (
                Reason::UninitializedBindingStore,
                "uninitialized_binding_store",
            ),
            (Reason::CapturedBindingStore, "captured_binding_store"),
            (Reason::ExportedBindingStore, "exported_binding_store"),
            (
                Reason::UnprovedStatementOperation,
                "unproved_statement_operation",
            ),
            (
                Reason::CallbackCapturesLiveCell,
                "callback_captures_live_cell",
            ),
            (Reason::UnknownCall, "unknown_call"),
            (Reason::PrecisionLoss, "precision_loss"),
            (Reason::UninitializedRead, "uninitialized_read"),
            (Reason::IncompleteExecution, "incomplete_execution"),
            (Reason::BindingNotInitialized, "binding_not_initialized"),
            (Reason::NoExecutionSummary, "no_execution_summary"),
            (Reason::RefusalUnavailable, "refusal_unavailable"),
        ];
        for (reason, name) in reasons {
            let json = serde_json::to_string(&reason).unwrap();
            assert_eq!(json, format!("\"{name}\""));
            assert_eq!(serde_json::from_str::<Reason>(&json).unwrap(), reason);
        }

        assert_eq!(Reason::try_from(0).unwrap(), Reason::UnsupportedOwnerKind);
        assert!(Reason::try_from(i32::MAX).is_err());
    }

    #[test]
    fn saved_synchronous_calls_require_authority_and_effect_metadata() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let mut purities = PuritySnapshot::default();
        let calls = SynchronousCalls::default();
        let wire = snapshot_synchronous_calls(&calls, &ordinals, &mut purities)
            .expect("default facts snapshot");
        let restored_purities = RestoredPurities::restore(purities.finish()).unwrap();
        assert!(restore_synchronous_calls(wire.clone(), &ordinals, &restored_purities).is_ok());
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
                restore_synchronous_calls(missing, &ordinals, &restored_purities).is_err(),
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
            "missing_kind",
            "unknown_kind",
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
                "missing_kind" => fact.kind = None,
                "unknown_kind" => fact.kind = Some(i32::MAX),
                "reads" => fact.reads = None,
                "duplicate_binding" => {
                    let bindings = &mut fact.declared;
                    bindings.push(bindings[0].clone());
                }
                "unknown_opacity" => fact.havoc.push(i32::MAX),
                "export_linkage" => fact.export_linkage = Some(i32::MAX),
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
        let mut wire = snapshot_synchronous_calls(&calls, &ordinals, &mut purities).unwrap();
        let restored_purities = RestoredPurities::restore(purities.finish()).unwrap();
        wire.global_object_aliases = None;
        let absent = analysis_codec::encode_protobuf(&wire).unwrap();
        assert!(
            restore_synchronous_calls(wire.clone(), &ordinals, &restored_purities)
                .unwrap()
                .global_object_aliases
                .is_none()
        );
        wire.global_object_aliases = Some(proto::SavedGlobalObjectAliases::default());
        let present = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_ne!(absent, present);
        let restored = restore_synchronous_calls(
            analysis_codec::decode_protobuf(&present).unwrap(),
            &ordinals,
            &restored_purities,
        )
        .unwrap();
        assert!(restored.global_object_aliases.as_ref().unwrap().is_empty());
    }

    #[test]
    fn saved_binding_and_effect_sets_ignore_process_local_context_order() {
        fn saved_bytes(reverse_context_creation: bool) -> (bool, Vec<u8>, Vec<u8>) {
            GLOBALS.set(&Globals::new(), || {
                let new_context = || SyntaxContext::empty().apply_mark(Mark::new());
                let (context_a, context_b) = if reverse_context_creation {
                    let context_b = new_context();
                    let context_a = new_context();
                    (context_a, context_b)
                } else {
                    (new_context(), new_context())
                };
                let bindings =
                    BTreeSet::from([("same".into(), context_a), ("same".into(), context_b)]);
                let process_local_order_starts_with_a =
                    bindings.iter().next().unwrap().1 == context_a;
                let mut ordinals = ContextOrdinals::new([]);
                swc_ecma_visit::Visit::visit_syntax_context(&mut ordinals, &context_a);
                swc_ecma_visit::Visit::visit_syntax_context(&mut ordinals, &context_b);

                let binding_bytes = analysis_codec::encode_protobuf(&proto::SavedBindingEffects {
                    eager: snapshot_bindings(&bindings, &ordinals).unwrap(),
                    ..proto::SavedBindingEffects::default()
                })
                .unwrap();
                let cells = BTreeSet::from([
                    EffectCell::Binding(("same".into(), context_a)),
                    EffectCell::Binding(("same".into(), context_b)),
                ]);
                let effect_bytes =
                    analysis_codec::encode_protobuf(&proto::SavedHeapContentAccesses {
                        reads: snapshot_effect_cells(&cells, &ordinals).unwrap(),
                        ..proto::SavedHeapContentAccesses::default()
                    })
                    .unwrap();
                (
                    process_local_order_starts_with_a,
                    binding_bytes,
                    effect_bytes,
                )
            })
        }

        let (forward_starts_with_a, forward_bindings, forward_effects) = saved_bytes(false);
        let (reverse_starts_with_a, reverse_bindings, reverse_effects) = saved_bytes(true);
        assert!(forward_starts_with_a);
        assert!(!reverse_starts_with_a);
        assert_eq!(forward_bindings, reverse_bindings);
        assert_eq!(forward_effects, reverse_effects);
    }

    #[test]
    fn fact_collections_reject_duplicate_bindings_and_keep_zero_oneof_values() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let binding = proto::SavedBinding {
            symbol: Some("value".into()),
            context_ordinal: Some(0),
        };
        let duplicate = vec![binding.clone(), binding.clone()];
        assert!(restore_bindings(duplicate.clone(), &ordinals).is_err());
        assert!(
            restore_position_bucketed(
                proto::SavedBindingEffects {
                    eager: duplicate.clone(),
                    ..proto::SavedBindingEffects::default()
                },
                &ordinals,
            )
            .is_err()
        );
        for (reads, rebinds) in [(duplicate.clone(), vec![]), (vec![], duplicate.clone())] {
            assert!(
                restore_synchronous_prefix_effects(
                    proto::SavedSynchronousPrefixEffects { reads, rebinds },
                    &ordinals,
                )
                .is_err()
            );
        }
        let site = proto::SavedBindingEffectSite {
            binding: Some(binding.clone()),
            source_unit: Some(0),
        };
        assert!(restore_binding_effect_sites(vec![site.clone(), site], &ordinals).is_err());
        use proto::saved_effect_cell::Value;
        let cell = proto::SavedEffectCell {
            value: Some(Value::GlobalProp("duplicate".into())),
        };
        assert!(restore_effect_cells(vec![cell.clone(), cell], &ordinals).is_err());
        let fallback = proto::SavedAssumedBindingEffect {
            binding: Some(binding.clone()),
            reason: Some(
                AssumedEffectReason::OpaqueCallbackInvocation
                    .to_proto()
                    .unwrap(),
            ),
        };
        assert!(
            restore_fallback_only_effects(
                proto::SavedFallbackOnlyEffects {
                    reads: vec![fallback.clone(), fallback],
                    ..proto::SavedFallbackOnlyEffects::default()
                },
                &ordinals,
            )
            .is_err()
        );
        let entry = proto::SavedSettledArrayElements {
            binding: Some(binding),
            elements: Some(proto::SettledElements {
                value: Some(proto::settled_elements::Value::Primitive(Default::default())),
            }),
        };
        assert!(restore_settled_array_elements(vec![entry.clone(), entry], &ordinals).is_err());
        assert!(
            restore_settled_array_elements(
                vec![proto::SavedSettledArrayElements::default()],
                &ordinals,
            )
            .is_err()
        );
        for cell in [
            EffectCell::GlobalProp(String::new()),
            EffectCell::ArrayContents(0),
            EffectCell::SetContents(0),
            EffectCell::ObjectShape(0),
            EffectCell::OwnProperty {
                origin: 0,
                property: String::new(),
            },
        ] {
            let wire = snapshot_effect_cells(&BTreeSet::from([cell.clone()]), &ordinals)
                .unwrap()
                .pop()
                .unwrap();
            let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
            assert_eq!(
                restore_effect_cells(
                    vec![analysis_codec::decode_protobuf(&bytes).unwrap()],
                    &ordinals
                )
                .unwrap(),
                BTreeSet::from([cell])
            );
        }
        assert!(restore_effect_cells(vec![proto::SavedEffectCell::default()], &ordinals).is_err());
        assert!(
            restore_binding_effect_sites(
                vec![proto::SavedBindingEffectSite {
                    binding: None,
                    source_unit: Some(0)
                }],
                &ordinals,
            )
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
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let binding = ("value".into(), SyntaxContext::empty());
        let effects = FallbackOnlyEffects {
            reads: BTreeMap::from([(
                binding.clone(),
                AssumedEffectReason::OpaqueCallbackInvocation,
            )]),
            ..FallbackOnlyEffects::default()
        };
        let wire = snapshot_fallback_only_effects(&effects, &ordinals).unwrap();
        let encoded = analysis_codec::encode_protobuf(&wire).unwrap();
        assert_eq!(
            restore_fallback_only_effects(
                analysis_codec::decode_protobuf(&encoded).unwrap(),
                &ordinals,
            )
            .unwrap()
            .reads,
            effects.reads
        );
        for missing_binding in [true, false] {
            let mut missing = wire.clone();
            if missing_binding {
                missing.reads[0].binding = None;
            } else {
                missing.reads[0].reason = None;
            }
            assert!(restore_fallback_only_effects(missing, &ordinals).is_err());
        }

        let sites = BTreeSet::from([(binding, SourceUnitOrdinal(0))]);
        let wire = snapshot_binding_effect_sites(&sites, &ordinals).unwrap();
        assert_eq!(wire[0].source_unit, Some(0));
        let bytes = analysis_codec::encode_protobuf(&wire[0]).unwrap();
        assert_eq!(
            restore_binding_effect_sites(
                vec![analysis_codec::decode_protobuf(&bytes).unwrap()],
                &ordinals,
            )
            .unwrap(),
            sites
        );
        for missing_binding in [true, false] {
            let mut missing = wire[0].clone();
            if missing_binding {
                missing.binding = None;
            } else {
                missing.source_unit = None;
            }
            assert!(restore_binding_effect_sites(vec![missing], &ordinals).is_err());
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
        let wire = snapshot_synchronous_calls(&calls, &ordinals, &mut snapshot)
            .expect("execution facts snapshot");
        let cause_id = snapshot.snapshot(&cause).expect("snapshot cause");
        let graph = snapshot.finish();
        use crate::saved_proto::proto;
        let graph_bytes = analysis_codec::encode_protobuf(&graph).expect("graph encode");
        let facts_bytes = analysis_codec::encode_protobuf(&wire).expect("facts encode");
        let wire: proto::SavedPurityGraph =
            analysis_codec::decode_protobuf(&graph_bytes).expect("graph decode");
        let purities = RestoredPurities::restore(wire).expect("valid graph");
        let wire: proto::SavedSynchronousCalls =
            analysis_codec::decode_protobuf(&facts_bytes).expect("facts decode");
        let calls =
            restore_synchronous_calls(wire, &ordinals, &purities).expect("valid execution facts");
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
