//! Saved per-chunk analysis. Verdicts are explicit saved data; unchanged
//! non-purity facts retain the existing codec and borrow their storage on write.

use std::borrow::Cow;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

use super::statement_facts::{ConstructorFacts, HeapContentCells, SynchronousCalls};
use super::*;
use crate::purity::saved::{PuritySnapshot, RestoredPurities, SavedPurity};
use crate::reports::saved::{
    SavedExecutionPrecisionLoss, SavedLocalRecordFactoryRejection, SavedLocalRecordFactoryUse,
    SavedNativeOperationInputUse, SavedReentryFreeOperationUse,
};

#[derive(Serialize, Deserialize)]
pub struct SavedChunkFactAnalysis<'a> {
    facts: Vec<SavedStatementFacts<'a>>,
    top_level_await: Option<SourceUnitOrdinal>,
    redundant_purity_hints: Cow<'a, [RedundantPurityHint]>,
    redundant_pure_member_hints: Cow<'a, [RedundantPureMemberHint]>,
}

impl<'a> SavedChunkFactAnalysis<'a> {
    pub fn snapshot(analysis: &'a ChunkFactAnalysis, purities: &mut PuritySnapshot<'a>) -> Self {
        Self {
            facts: analysis
                .facts
                .iter()
                .map(|fact| SavedStatementFacts::snapshot(fact, purities))
                .collect(),
            top_level_await: analysis.top_level_await,
            redundant_purity_hints: Cow::Borrowed(&analysis.redundant_purity_hints),
            redundant_pure_member_hints: Cow::Borrowed(&analysis.redundant_pure_member_hints),
        }
    }

    pub fn restore(self, purities: &RestoredPurities) -> Result<ChunkFactAnalysis> {
        Ok(ChunkFactAnalysis {
            facts: self
                .facts
                .into_iter()
                .map(|fact| fact.restore(purities))
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
    stable_iterable_producers: Cow<'a, BTreeSet<SourceUnitOrdinal>>,
    source_location: Cow<'a, Option<SourceLocation>>,
    callback_factory_barriers: Cow<'a, [CallbackFactoryBarrierFact]>,
    declared: Cow<'a, BTreeSet<Id>>,
    local_bindings: Cow<'a, BTreeSet<Id>>,
    var_declared: Cow<'a, BTreeSet<Id>>,
    var_initializers: Cow<'a, BTreeSet<Id>>,
    reads: Cow<'a, PositionBucketed<BTreeSet<Id>>>,
    rebinds: Cow<'a, PositionBucketed<BTreeSet<Id>>>,
    member_write_roots: Cow<'a, BTreeSet<Id>>,
    local_effects: Cow<'a, BTreeSet<Id>>,
    calls: Cow<'a, PositionBucketed<BTreeSet<Id>>>,
    constructor: Cow<'a, Option<ConstructorFacts>>,
    synchronous_calls: Option<SavedSynchronousCalls<'a>>,
    at_init_unresolved_sources: Cow<'a, BTreeSet<Id>>,
    at_init_unresolved_inline_fn: bool,
    first_order_unresolved_sources: Cow<'a, BTreeSet<Id>>,
    first_order_unresolved_inline_fn: bool,
    declares_direct_function: bool,
    global_writes: Cow<'a, BTreeSet<String>>,
    global_reads: Cow<'a, BTreeSet<String>>,
    havoc: Cow<'a, BTreeSet<DataflowOpacityCause>>,
    heap_contents: Cow<'a, HeapContentCells>,
    heap_one_shot_allocation: Cow<'a, Option<EffectCell>>,
    deferred_heap_allocations: Cow<'a, BTreeSet<EffectCell>>,
    heap_native_integrity_uses: Cow<'a, [crate::reports::schema::NativeIntegrityUse]>,
    purity: SavedPurity,
    kind: StatementKind,
}

impl<'a> SavedStatementFacts<'a> {
    fn snapshot(fact: &'a StatementFacts, purities: &mut PuritySnapshot<'a>) -> Self {
        Self {
            ordinal: fact.ordinal,
            stable_iterable_producers: Cow::Borrowed(&fact.stable_iterable_producers),
            source_location: Cow::Borrowed(&fact.source_location),
            callback_factory_barriers: Cow::Borrowed(&fact.callback_factory_barriers),
            declared: Cow::Borrowed(&fact.declared),
            local_bindings: Cow::Borrowed(&fact.local_bindings),
            var_declared: Cow::Borrowed(&fact.var_declared),
            var_initializers: Cow::Borrowed(&fact.var_initializers),
            reads: Cow::Borrowed(&fact.reads),
            rebinds: Cow::Borrowed(&fact.rebinds),
            member_write_roots: Cow::Borrowed(&fact.member_write_roots),
            local_effects: Cow::Borrowed(&fact.local_effects),
            calls: Cow::Borrowed(&fact.calls),
            constructor: Cow::Borrowed(&fact.constructor),
            synchronous_calls: fact
                .synchronous_calls
                .as_ref()
                .map(|calls| SavedSynchronousCalls::snapshot(calls, purities)),
            at_init_unresolved_sources: Cow::Borrowed(&fact.at_init_unresolved_sources),
            at_init_unresolved_inline_fn: fact.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: Cow::Borrowed(&fact.first_order_unresolved_sources),
            first_order_unresolved_inline_fn: fact.first_order_unresolved_inline_fn,
            declares_direct_function: fact.declares_direct_function,
            global_writes: Cow::Borrowed(&fact.global_writes),
            global_reads: Cow::Borrowed(&fact.global_reads),
            havoc: Cow::Borrowed(&fact.havoc),
            heap_contents: Cow::Borrowed(&fact.heap_contents),
            heap_one_shot_allocation: Cow::Borrowed(&fact.heap_one_shot_allocation),
            deferred_heap_allocations: Cow::Borrowed(&fact.deferred_heap_allocations),
            heap_native_integrity_uses: Cow::Borrowed(&fact.heap_native_integrity_uses),
            purity: purities.snapshot_value(&fact.purity),
            kind: fact.kind,
        }
    }

    fn restore(self, purities: &RestoredPurities) -> Result<StatementFacts> {
        Ok(StatementFacts {
            ordinal: self.ordinal,
            stable_iterable_producers: self.stable_iterable_producers.into_owned(),
            source_location: self.source_location.into_owned(),
            callback_factory_barriers: self.callback_factory_barriers.into_owned(),
            declared: self.declared.into_owned(),
            local_bindings: self.local_bindings.into_owned(),
            var_declared: self.var_declared.into_owned(),
            var_initializers: self.var_initializers.into_owned(),
            reads: self.reads.into_owned(),
            rebinds: self.rebinds.into_owned(),
            member_write_roots: self.member_write_roots.into_owned(),
            local_effects: self.local_effects.into_owned(),
            calls: self.calls.into_owned(),
            constructor: self.constructor.into_owned(),
            synchronous_calls: self
                .synchronous_calls
                .map(|calls| calls.restore(purities))
                .transpose()?,
            at_init_unresolved_sources: self.at_init_unresolved_sources.into_owned(),
            at_init_unresolved_inline_fn: self.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: self.first_order_unresolved_sources.into_owned(),
            first_order_unresolved_inline_fn: self.first_order_unresolved_inline_fn,
            declares_direct_function: self.declares_direct_function,
            global_writes: self.global_writes.into_owned(),
            global_reads: self.global_reads.into_owned(),
            havoc: self.havoc.into_owned(),
            heap_contents: self.heap_contents.into_owned(),
            heap_one_shot_allocation: self.heap_one_shot_allocation.into_owned(),
            deferred_heap_allocations: self.deferred_heap_allocations.into_owned(),
            heap_native_integrity_uses: self.heap_native_integrity_uses.into_owned(),
            purity: purities.restore_value(self.purity)?,
            kind: self.kind,
        })
    }
}

/// Non-purity execution facts retain their existing representation. The
/// runtime serializer always omits the disagreement; both saved verdicts
/// explicitly participate in the same cause graph as statement purity.
#[derive(Serialize, Deserialize)]
struct SavedSynchronousCalls<'a> {
    execution: Cow<'a, SynchronousCalls>,
    purity_disagreement: Option<SavedPurityDisagreement>,
    local_record_factory_uses: Vec<SavedLocalRecordFactoryUse<'a>>,
    local_record_factory_rejections: Vec<SavedLocalRecordFactoryRejection<'a>>,
    precision_losses: Vec<SavedExecutionPrecisionLoss<'a>>,
    native_operation_input_uses: Vec<SavedNativeOperationInputUse<'a>>,
    author_host_contract_uses: Vec<SavedReentryFreeOperationUse<'a>>,
}

#[derive(Serialize, Deserialize)]
struct SavedPurityDisagreement {
    prepared: SavedPurity,
    completed: SavedPurity,
}

impl<'a> SavedSynchronousCalls<'a> {
    fn snapshot(calls: &'a SynchronousCalls, purities: &mut PuritySnapshot<'a>) -> Self {
        Self {
            execution: Cow::Borrowed(calls),
            purity_disagreement: calls.purity_disagreement.as_ref().map(|disagreement| {
                SavedPurityDisagreement {
                    prepared: purities.snapshot_value(&disagreement.prepared),
                    completed: purities.snapshot_value(&disagreement.completed),
                }
            }),
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
            precision_losses: calls
                .precision_losses
                .iter()
                .map(SavedExecutionPrecisionLoss::snapshot)
                .collect(),
            native_operation_input_uses: calls
                .native_operation_input_uses
                .iter()
                .map(SavedNativeOperationInputUse::snapshot)
                .collect(),
            author_host_contract_uses: calls
                .author_host_contract_uses
                .iter()
                .map(SavedReentryFreeOperationUse::snapshot)
                .collect(),
        }
    }

    fn restore(self, purities: &RestoredPurities) -> Result<SynchronousCalls> {
        let mut calls = self.execution.into_owned();
        calls.local_record_factory_uses = self
            .local_record_factory_uses
            .into_iter()
            .map(SavedLocalRecordFactoryUse::restore)
            .collect();
        calls.local_record_factory_rejections = self
            .local_record_factory_rejections
            .into_iter()
            .map(SavedLocalRecordFactoryRejection::restore)
            .collect();
        calls.precision_losses = self
            .precision_losses
            .into_iter()
            .map(SavedExecutionPrecisionLoss::restore)
            .collect();
        calls.native_operation_input_uses = self
            .native_operation_input_uses
            .into_iter()
            .map(SavedNativeOperationInputUse::restore)
            .collect();
        calls.author_host_contract_uses = self
            .author_host_contract_uses
            .into_iter()
            .map(SavedReentryFreeOperationUse::restore)
            .collect();
        calls.purity_disagreement = self
            .purity_disagreement
            .map(|disagreement| -> Result<_> {
                Ok(crate::reports::schema::PurityCompletionDisagreement {
                    prepared: purities.restore_value(disagreement.prepared)?,
                    completed: purities.restore_value(disagreement.completed)?,
                })
            })
            .transpose()?;
        Ok(calls)
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
            precision_losses: vec![crate::reports::schema::ExecutionPrecisionLoss {
                reason: crate::reports::schema::ExecutionPrecisionLossReason::RecursiveInvocation,
                reach: crate::reports::schema::HavocReach::Bounded,
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
        let saved = SavedSynchronousCalls::snapshot(&calls, &mut snapshot);
        let cause_id = snapshot.snapshot(&cause);
        let graph = snapshot.finish();
        let bytes = rmp_serde::to_vec_named(&(graph, saved)).expect("ordinary encode");
        let (graph, saved): (
            crate::purity::saved::SavedPurityGraph,
            SavedSynchronousCalls<'_>,
        ) = rmp_serde::from_slice(&bytes).expect("ordinary decode");
        let purities = graph.restore().expect("valid graph");
        let calls = saved.restore(&purities).expect("valid execution facts");
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
