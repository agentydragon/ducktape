//! Read-only initialization effects for report-only translation validation.
//!
//! Recorded accesses and domain coverage are different facts. In particular,
//! synchronous execution completeness and purity do not certify the absence
//! of general property, callee-global, or host effects.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

use super::evaluation_effects::{GlobalAccessKind, GlobalMayAccess};
use super::{DataflowOpacityCause, EffectCell, StatementFacts, StatementKind};
use crate::reports::schema::SourceLocation;
use crate::reports::schema::{AssumedEffectReason, ExecutionPrecisionLoss};

#[derive(Debug, Clone, Serialize)]
pub struct InitializationEffectEvidence {
    #[serde(flatten)]
    basis: InitializationEffectBasis,
    /// Indexed may-accesses stay separate from the cell's existing evidence.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    global_may_accesses: Vec<GlobalMayAccess>,
    #[serde(skip_serializing_if = "zero")]
    global_may_access_count: usize,
    #[serde(skip_serializing_if = "not_truncated")]
    global_may_accesses_truncated: bool,
}

fn zero(value: &usize) -> bool {
    *value == 0
}
fn not_truncated(value: &bool) -> bool {
    !*value
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum InitializationEffectBasis {
    Observed,
    Fallback {
        reason: AssumedEffectReason,
    },
    /// Composed heap summaries do not retain per-access execution provenance.
    UnclassifiedCalleeHeap,
    /// Canonical runtime may-accesses, including reordered/conditional lanes.
    /// These never certify that an operation definitely executes.
    IndexedMayAccess,
}

impl InitializationEffectEvidence {
    pub fn is_observed(&self) -> bool {
        matches!(self.basis, InitializationEffectBasis::Observed)
    }

    /// Bound only the presentation; retain the exact source-operation count.
    pub fn sampled(&self, limit: usize) -> Self {
        Self {
            basis: self.basis.clone(),
            global_may_accesses: self
                .global_may_accesses
                .iter()
                .take(limit)
                .cloned()
                .collect(),
            global_may_access_count: self.global_may_access_count,
            global_may_accesses_truncated: self.global_may_accesses_truncated
                || self.global_may_access_count > limit,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UnknownEffectCoverage {
    GlobalProperties,
    GeneralHeap,
    HostEffects,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OwnerInitializationCoverageRefusalReason {
    UnsupportedOwnerKind,
    ExportedOwner,
    SplitDeclaration,
    UnsupportedBindingShape,
    MissingInitializer,
    UnsupportedInitializer,
    OwnerBindingMismatch,
    InitializerResultUnproved,
    UnknownCall,
    PrecisionLoss,
    UninitializedRead,
    IncompleteExecution,
    BindingNotInitialized,
    NoExecutionSummary,
    RefusalUnavailable,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerInitializationCoverageRefusal {
    pub reason: OwnerInitializationCoverageRefusalReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
}

#[derive(Debug, Clone)]
pub struct InitializationEffects {
    pub ordinal: SourceUnitOrdinal,
    pub reads: BTreeMap<EffectCell, InitializationEffectEvidence>,
    pub writes: BTreeMap<EffectCell, InitializationEffectEvidence>,
    pub allocates: BTreeSet<EffectCell>,
    /// Missing semantic domains; independent of legacy timing opacity.
    pub unknown_coverage: BTreeSet<UnknownEffectCoverage>,
    pub dataflow_opacity: BTreeSet<DataflowOpacityCause>,
    pub incomplete_execution: bool,
    pub execution_losses: Vec<ExecutionPrecisionLoss>,
    pub opaque_heap: bool,
    pub owner_coverage_refusal: Option<OwnerInitializationCoverageRefusal>,
}

impl InitializationEffects {
    pub fn has_havoc(&self) -> bool {
        !self.unknown_coverage.is_empty()
            || !self.dataflow_opacity.is_empty()
            || self.incomplete_execution
            || !self.execution_losses.is_empty()
            || self.opaque_heap
    }

    pub fn is_empty(&self) -> bool {
        self.reads.is_empty()
            && self.writes.is_empty()
            && self.allocates.is_empty()
            && !self.has_havoc()
    }
}

impl StatementFacts {
    /// Shared with the binding timeline: module instantiation is not an
    /// evaluation-time write. A variable initialized with a function remains
    /// an evaluation-time initializer.
    pub(crate) fn initializes_bindings_at_evaluation(&self) -> bool {
        !matches!(self.kind, StatementKind::Import | StatementKind::FnDecl)
            && !(self.kind == StatementKind::Export && self.declares_direct_function)
    }

    pub fn initialization_effects(&self) -> InitializationEffects {
        let mut effects = InitializationEffects {
            ordinal: self.ordinal,
            reads: BTreeMap::new(),
            writes: BTreeMap::new(),
            allocates: self.heap_contents.allocations.clone(),
            unknown_coverage: BTreeSet::new(),
            dataflow_opacity: self.havoc.clone(),
            incomplete_execution: false,
            execution_losses: Vec::new(),
            opaque_heap: self.heap_contents.opaque,
            owner_coverage_refusal: None,
        };
        // These statement kinds execute no body at module evaluation. In
        // particular their lazy reads and export heap opacity are irrelevant.
        if !self.initializes_bindings_at_evaluation() {
            effects.allocates.clear();
            effects.dataflow_opacity.clear();
            effects.opaque_heap = false;
            return effects;
        }
        if !self.synchronous_calls.as_ref().is_some_and(|calls| {
            calls
                .completed_initialization
                .as_ref()
                .is_some_and(|proof| proof.covers(self.ordinal))
        }) {
            effects.unknown_coverage.extend([
                UnknownEffectCoverage::GlobalProperties,
                UnknownEffectCoverage::GeneralHeap,
                UnknownEffectCoverage::HostEffects,
            ]);
        }
        for binding in &self.reads.eager {
            record(
                &mut effects.reads,
                EffectCell::Binding(binding.clone()),
                None,
            );
        }
        for binding in self
            .declared
            .difference(&self.var_declared)
            .chain(&self.var_initializers)
            .chain(&self.rebinds.eager)
        {
            record(
                &mut effects.writes,
                EffectCell::Binding(binding.clone()),
                None,
            );
        }
        for key in &self.global_reads {
            record(
                &mut effects.reads,
                EffectCell::GlobalProp(key.clone()),
                None,
            );
        }
        for key in &self.global_writes {
            record(
                &mut effects.writes,
                EffectCell::GlobalProp(key.clone()),
                None,
            );
        }
        for cell in &self.heap_contents.reads {
            record(&mut effects.reads, cell.clone(), None);
        }
        for cell in &self.heap_contents.writes {
            record(&mut effects.writes, cell.clone(), None);
        }
        if let Some(calls) = &self.synchronous_calls {
            for (site, evidence) in &calls.evaluation_effects.global_accesses {
                let target = match site.access {
                    GlobalAccessKind::Read => &mut effects.reads,
                    GlobalAccessKind::Write => &mut effects.writes,
                };
                let entry = target.entry(EffectCell::GlobalProp(site.property.clone()));
                let previous = entry.or_insert_with(|| InitializationEffectEvidence {
                    basis: InitializationEffectBasis::IndexedMayAccess,
                    global_may_accesses: Vec::new(),
                    global_may_access_count: 0,
                    global_may_accesses_truncated: false,
                });
                previous.global_may_accesses.push(GlobalMayAccess {
                    site: site.clone(),
                    evidence: evidence.clone(),
                });
                previous.global_may_access_count += 1;
            }
            effects.incomplete_execution = !calls.complete;
            effects.execution_losses = calls.precision_losses.clone();
            effects.opaque_heap |= calls.heap_contents.opaque;
            effects
                .allocates
                .extend(calls.heap_contents.allocations.iter().cloned());
            for (binding, _) in &calls.reads {
                record(
                    &mut effects.reads,
                    EffectCell::Binding(binding.clone()),
                    calls.fallback_read(binding),
                );
            }
            for binding in &calls.uninitialized_reads {
                record(
                    &mut effects.reads,
                    EffectCell::Binding(binding.clone()),
                    calls.fallback_uninitialized_read(binding),
                );
            }
            for (binding, _) in &calls.rebinds {
                record(
                    &mut effects.writes,
                    EffectCell::Binding(binding.clone()),
                    calls.fallback_rebind(binding),
                );
            }
            for (target, cells) in [
                (&mut effects.reads, &calls.heap_contents.reads),
                (&mut effects.writes, &calls.heap_contents.writes),
            ] {
                for cell in cells {
                    target
                        .entry(cell.clone())
                        .or_insert_with(|| InitializationEffectEvidence {
                            basis: InitializationEffectBasis::UnclassifiedCalleeHeap,
                            global_may_accesses: Vec::new(),
                            global_may_access_count: 0,
                            global_may_accesses_truncated: false,
                        });
                }
            }
        } else {
            effects.incomplete_execution = true;
        }
        if !effects.unknown_coverage.is_empty() {
            let reason = match &self.synchronous_calls {
                Some(calls) => calls
                    .initialization_coverage_refusal
                    .unwrap_or(OwnerInitializationCoverageRefusalReason::RefusalUnavailable),
                None => OwnerInitializationCoverageRefusalReason::NoExecutionSummary,
            };
            effects.owner_coverage_refusal = Some(OwnerInitializationCoverageRefusal {
                reason,
                source_location: self.source_location.clone(),
            });
        }
        effects
    }
}

fn record(
    target: &mut BTreeMap<EffectCell, InitializationEffectEvidence>,
    cell: EffectCell,
    fallback: Option<&AssumedEffectReason>,
) {
    let evidence = fallback.map_or(InitializationEffectBasis::Observed, |reason| {
        InitializationEffectBasis::Fallback {
            reason: reason.clone(),
        }
    });
    target
        .entry(cell)
        .and_modify(|previous| {
            if matches!(evidence, InitializationEffectBasis::Observed) {
                previous.basis = evidence.clone();
            }
        })
        .or_insert(InitializationEffectEvidence {
            basis: evidence,
            global_may_accesses: Vec::new(),
            global_may_access_count: 0,
            global_may_accesses_truncated: false,
        });
}
