//! Saved per-chunk analysis. Verdicts are explicit saved data; unchanged
//! non-purity facts retain the existing codec and borrow their storage on write.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use analysis_codec::{ContextOrdinals, SavedBinding, SavedChunk, SavedModuleCell};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

use super::abstract_domain::value::{
    ClassDefinitionId, HeapAddress, HeapModuleScope, Kind, ProviderCallableId,
};
#[cfg(test)]
use super::abstract_domain::value::{LocalAnalysisScopeId, Value};
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

fn validate_provider_closure_namespace(
    closure: Option<&crate::analysis_inputs::ResolvedProviderDependencyClosure>,
    ordinals: &ContextOrdinals,
) -> Result<()> {
    let Some(closure) = closure else {
        return Ok(());
    };
    ensure!(
        closure.is_well_formed(),
        "saved provider closure is internally inconsistent"
    );
    for module in &closure.modules {
        ordinals.restore_chunk(SavedChunk(module.clone()))?;
    }
    Ok(())
}

fn validate_host_dependency_namespace(
    dependencies: Option<&BTreeSet<crate::analysis_inputs::AdmittedHostModuleDependency>>,
    ordinals: &ContextOrdinals,
) -> Result<()> {
    for dependency in dependencies.into_iter().flatten() {
        ordinals.restore_chunk(SavedChunk(dependency.source_module.clone()))?;
        ensure!(
            dependency.is_well_formed(),
            "saved admitted host dependency is internally inconsistent"
        );
    }
    Ok(())
}

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

fn require_allocation_kind(address: HeapAddress, expected: Kind, cell: &str) -> Result<()> {
    ensure!(
        matches!(address, HeapAddress::AllocationFamily { kind, .. } if kind == expected),
        "{cell} requires a {expected:?} allocation-family address"
    );
    Ok(())
}

fn require_array_contents_address(address: HeapAddress) -> Result<()> {
    ensure!(
        matches!(
            address,
            HeapAddress::AllocationFamily {
                kind: Kind::Object | Kind::Array,
                ..
            }
        ),
        "array contents require an object- or array-family address"
    );
    Ok(())
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
pub(crate) struct SavedClassDefinitionId {
    module: SavedChunk,
    origin: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) struct SavedProviderCallableId {
    module: SavedChunk,
    origin: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "family", rename_all = "snake_case")]
pub(crate) enum SavedHeapAddress {
    GlobalObject,
    AllocationFamily {
        module: SavedChunk,
        origin: u32,
        kind: Kind,
    },
    ConstructorInstances {
        class: SavedClassDefinitionId,
    },
    ClassObject {
        class: SavedClassDefinitionId,
    },
    ProviderAllocationFamily {
        consumer: SavedChunk,
        call_origin: u32,
        producer: SavedProviderCallableId,
        slot: u32,
        kind: Kind,
    },
}

impl SavedHeapAddress {
    pub(crate) fn snapshot(address: HeapAddress, ordinals: &ContextOrdinals<'_>) -> Result<Self> {
        let save_module = |module: HeapModuleScope| -> Result<SavedChunk> {
            match module {
                HeapModuleScope::Chunk(module) => ordinals.snapshot_chunk(module),
                HeapModuleScope::LocalAnalysis(_) => {
                    anyhow::bail!(
                        "local-analysis heap addresses cannot cross the saved chunk boundary"
                    )
                }
            }
        };
        Ok(match address {
            HeapAddress::GlobalObject => Self::GlobalObject,
            HeapAddress::AllocationFamily {
                module,
                origin,
                kind,
            } => {
                ensure!(
                    kind != Kind::Class,
                    "class definitions cannot use allocation-family addresses"
                );
                Self::AllocationFamily {
                    module: save_module(module)?,
                    origin,
                    kind,
                }
            }
            HeapAddress::ConstructorInstances { class } => Self::ConstructorInstances {
                class: SavedClassDefinitionId {
                    module: save_module(class.module)?,
                    origin: class.origin,
                },
            },
            HeapAddress::ClassObject { class } => Self::ClassObject {
                class: SavedClassDefinitionId {
                    module: save_module(class.module)?,
                    origin: class.origin,
                },
            },
            HeapAddress::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                slot,
                kind,
                ..
            } => {
                ensure!(
                    kind != Kind::Class,
                    "class definitions cannot use provider allocation-family addresses"
                );
                Self::ProviderAllocationFamily {
                    consumer: save_module(consumer)?,
                    call_origin,
                    producer: SavedProviderCallableId {
                        module: save_module(producer.module)?,
                        origin: producer.origin,
                    },
                    slot,
                    kind,
                }
            }
        })
    }

    pub(crate) fn restore(self, ordinals: &ContextOrdinals<'_>) -> Result<HeapAddress> {
        let restore_module = |module: SavedChunk| ordinals.restore_chunk(module);
        Ok(match self {
            Self::GlobalObject => HeapAddress::GlobalObject,
            Self::AllocationFamily {
                module,
                origin,
                kind,
            } => {
                ensure!(
                    kind != Kind::Class,
                    "class definitions cannot use allocation-family addresses"
                );
                HeapAddress::AllocationFamily {
                    module: HeapModuleScope::Chunk(restore_module(module)?),
                    origin,
                    kind,
                }
            }
            Self::ConstructorInstances { class } => {
                HeapAddress::constructor_instances(ClassDefinitionId {
                    module: HeapModuleScope::Chunk(restore_module(class.module)?),
                    origin: class.origin,
                })
            }
            Self::ClassObject { class } => HeapAddress::ClassObject {
                class: ClassDefinitionId {
                    module: HeapModuleScope::Chunk(restore_module(class.module)?),
                    origin: class.origin,
                },
            },
            Self::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                slot,
                kind,
            } => {
                ensure!(
                    kind != Kind::Class,
                    "class definitions cannot use provider allocation-family addresses"
                );
                HeapAddress::ProviderAllocationFamily {
                    consumer: HeapModuleScope::Chunk(restore_module(consumer)?),
                    call_origin,
                    producer: ProviderCallableId {
                        module: HeapModuleScope::Chunk(restore_module(producer.module)?),
                        origin: producer.origin,
                    },
                    producer_birth: None,
                    slot,
                    kind,
                }
            }
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
    ModuleCell(SavedModuleCell),
    GlobalProp(String),
    ExecutionProgress,
    ArrayContents(SavedHeapAddress),
    SetContents(SavedHeapAddress),
    GlobalSymbolRegistry,
    ObjectShape(SavedHeapAddress),
    DescriptorMapContents(SavedHeapAddress),
    OwnProperty {
        origin: SavedHeapAddress,
        property: String,
    },
    PrivateSlot {
        origin: SavedHeapAddress,
        class: SavedClassDefinitionId,
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
            EffectCell::ModuleCell(cell) => Self::ModuleCell(ordinals.snapshot_module_cell(*cell)?),
            EffectCell::GlobalProp(property) => Self::GlobalProp(property.clone()),
            EffectCell::ExecutionProgress => Self::ExecutionProgress,
            EffectCell::ArrayContents(origin) => {
                require_array_contents_address(*origin)?;
                Self::ArrayContents(SavedHeapAddress::snapshot(*origin, ordinals)?)
            }
            EffectCell::SetContents(origin) => {
                require_allocation_kind(*origin, Kind::Set, "Set contents")?;
                Self::SetContents(SavedHeapAddress::snapshot(*origin, ordinals)?)
            }
            EffectCell::GlobalSymbolRegistry => Self::GlobalSymbolRegistry,
            EffectCell::ObjectShape(origin) => {
                Self::ObjectShape(SavedHeapAddress::snapshot(*origin, ordinals)?)
            }
            EffectCell::DescriptorMapContents(origin) => {
                require_allocation_kind(*origin, Kind::Object, "descriptor-map contents")?;
                Self::DescriptorMapContents(SavedHeapAddress::snapshot(*origin, ordinals)?)
            }
            EffectCell::OwnProperty { origin, property } => Self::OwnProperty {
                origin: SavedHeapAddress::snapshot(*origin, ordinals)?,
                property: property.clone(),
            },
            EffectCell::PrivateSlot {
                origin,
                class,
                name,
            } => Self::PrivateSlot {
                origin: SavedHeapAddress::snapshot(*origin, ordinals)?,
                class: SavedClassDefinitionId {
                    module: match class.module {
                        HeapModuleScope::Chunk(module) => ordinals.snapshot_chunk(module)?,
                        HeapModuleScope::LocalAnalysis(_) => {
                            anyhow::bail!(
                                "local-analysis class identity cannot cross the saved chunk boundary"
                            )
                        }
                    },
                    origin: class.origin,
                },
                name: name.clone(),
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<EffectCell> {
        Ok(match self {
            Self::Binding(binding) => EffectCell::Binding(ordinals.restore_binding(binding)?),
            Self::LexicalCell { origin, instance } => EffectCell::LexicalCell { origin, instance },
            Self::ModuleCell(cell) => EffectCell::ModuleCell(ordinals.restore_module_cell(cell)?),
            Self::GlobalProp(property) => EffectCell::GlobalProp(property),
            Self::ExecutionProgress => EffectCell::ExecutionProgress,
            Self::ArrayContents(origin) => {
                let origin = origin.restore(ordinals)?;
                require_array_contents_address(origin)?;
                EffectCell::ArrayContents(origin)
            }
            Self::SetContents(origin) => {
                let origin = origin.restore(ordinals)?;
                require_allocation_kind(origin, Kind::Set, "Set contents")?;
                EffectCell::SetContents(origin)
            }
            Self::GlobalSymbolRegistry => EffectCell::GlobalSymbolRegistry,
            Self::ObjectShape(origin) => EffectCell::ObjectShape(origin.restore(ordinals)?),
            Self::DescriptorMapContents(origin) => {
                let origin = origin.restore(ordinals)?;
                require_allocation_kind(origin, Kind::Object, "descriptor-map contents")?;
                EffectCell::DescriptorMapContents(origin)
            }
            Self::OwnProperty { origin, property } => EffectCell::OwnProperty {
                origin: origin.restore(ordinals)?,
                property,
            },
            Self::PrivateSlot {
                origin,
                class,
                name,
            } => EffectCell::PrivateSlot {
                origin: origin.restore(ordinals)?,
                class: ClassDefinitionId {
                    module: HeapModuleScope::Chunk(ordinals.restore_chunk(class.module)?),
                    origin: class.origin,
                },
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

#[derive(Serialize, Deserialize)]
struct SavedHeapAccessProvenance {
    executed_operations: SavedHeapContentAccesses,
    assumed_operations: SavedHeapContentAccesses,
    conservative_summaries: SavedHeapContentAccesses,
}

impl SavedHeapAccessProvenance {
    fn snapshot(
        provenance: &super::HeapAccessProvenance,
        ordinals: &ContextOrdinals,
    ) -> Result<Self> {
        Ok(Self {
            executed_operations: SavedHeapContentAccesses::snapshot(
                &provenance.executed_operations,
                ordinals,
            )?,
            assumed_operations: SavedHeapContentAccesses::snapshot(
                &provenance.assumed_operations,
                ordinals,
            )?,
            conservative_summaries: SavedHeapContentAccesses::snapshot(
                &provenance.conservative_summaries,
                ordinals,
            )?,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<super::HeapAccessProvenance> {
        Ok(super::HeapAccessProvenance {
            executed_operations: self.executed_operations.restore(ordinals)?,
            assumed_operations: self.assumed_operations.restore(ordinals)?,
            conservative_summaries: self.conservative_summaries.restore(ordinals)?,
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
        canonical_source_sites: &BTreeSet<super::sync_effects::SourceOperationSite>,
    ) -> Result<ChunkFactAnalysis> {
        ensure!(
            self.canonical_source_operation_sites()? == *canonical_source_sites,
            "saved source operation table differs from the prepared source table"
        );
        let facts: Vec<_> = self
            .facts
            .into_iter()
            .map(|fact| fact.restore(ordinals, purities))
            .collect::<Result<_>>()?;
        for fact in &facts {
            if let Some(calls) = &fact.synchronous_calls {
                ensure!(
                    calls
                        .binding_effect_coverage
                        .belongs_to_source_owner(fact.ordinal)
                        && calls
                            .binding_effect_coverage
                            .references_source_sites(canonical_source_sites),
                    "saved execution receipt does not correspond to its prepared source owner and table"
                );
                for requirement in &calls.required_imported_primitive_reads {
                    ensure!(
                        requirement.source_site.as_ref().is_none_or(|site| {
                            site.owner() == requirement.source_owner
                                && canonical_source_sites.contains(site)
                        }) && requirement
                            .consumer_root
                            .is_none_or(|root| root == fact.ordinal),
                        "saved imported primitive read does not correspond to its prepared source owner, consumer root, and operation table"
                    );
                }
            }
        }
        Ok(ChunkFactAnalysis {
            facts,
            top_level_await: self.top_level_await,
            redundant_purity_hints: self.redundant_purity_hints.into_owned(),
            redundant_pure_member_hints: self.redundant_pure_member_hints.into_owned(),
        })
    }

    /// The saved source table is prepared from the parsed chunk independently
    /// of execution receipts, so program-summary restoration can validate
    /// provider receipts before it accepts their completion sidecars.
    pub(crate) fn canonical_source_operation_sites(
        &self,
    ) -> Result<BTreeSet<super::sync_effects::SourceOperationSite>> {
        let mut owners = BTreeSet::new();
        let mut sites = BTreeSet::new();
        for fact in &self.facts {
            ensure!(
                owners.insert(fact.ordinal),
                "duplicate saved statement-fact owner in canonical source table"
            );
            for site in fact.source_operation_sites.iter() {
                ensure!(
                    site.owner == fact.ordinal,
                    "saved source operation site owner does not match its statement"
                );
                ensure!(
                    sites.insert(site.clone()),
                    "duplicate saved source operation site in canonical source table"
                );
            }
        }
        Ok(sites)
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
    source_operation_sites: Cow<'a, [super::sync_effects::SourceOperationSite]>,
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
            source_operation_sites: Cow::Borrowed(&fact.source_operation_sites),
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
        let mut source_operation_sites = Vec::new();
        for site in self.source_operation_sites.into_owned() {
            ensure!(
                site.owner == self.ordinal,
                "saved source operation site owner does not match its statement"
            );
            ensure!(
                !source_operation_sites.contains(&site),
                "duplicate saved source operation site"
            );
            source_operation_sites.push(site);
        }
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
            source_operation_sites,
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
    #[serde(deserialize_with = "Deserialize::deserialize")]
    root_normal_completion:
        Cow<'a, Option<Box<super::sync_effects::noninvoking_initialization::RootNormalCompletion>>>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    root_outward_throw:
        Cow<'a, Option<Box<super::sync_effects::noninvoking_initialization::RootOutwardThrow>>>,
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
    heap_access_provenance: SavedHeapAccessProvenance,
    operation_support_receipts: Vec<super::sync_effects::SavedOperationSupportReceipt>,
    precision_losses: Vec<SavedExecutionPrecisionLoss<'a>>,
    native_integrity_checks: Cow<'a, [crate::reports::schema::NativeIntegrityCheck]>,
    native_prototype_property_checks:
        Cow<'a, [crate::reports::schema::NativePrototypePropertyCheck]>,
    native_prefix_observations: Cow<'a, [crate::reports::schema::NativePrefixObservation]>,
    native_integrity_uses: Cow<'a, [crate::reports::schema::NativeIntegrityUse]>,
    native_operation_input_uses: Vec<SavedNativeOperationInputUse<'a>>,
    imported_primitive_array_spread_input_uses:
        Cow<'a, [crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse]>,
    inference_uses: Cow<'a, [crate::analysis_provenance::InferenceUse]>,
    callback_factory_uses: Cow<'a, [super::CallbackFactoryUseFact]>,
    returned_record_uses: Cow<'a, [super::ReturnedRecordUseFact]>,
    author_host_contract_uses: Vec<SavedReentryFreeOperationUse<'a>>,
    purity_disagreement: Option<SavedPurityDisagreement>,
    required_initialized_module_cells: Vec<SavedModuleCell>,
    conditional_provider_targets: Vec<SavedConditionalProviderTargetRequirement>,
    required_imported_primitive_reads: Vec<SavedRequiredImportedPrimitiveRead>,
}

#[derive(Serialize, Deserialize)]
struct SavedConditionalProviderTargetRequirement {
    owner: SourceUnitOrdinal,
    call_span: swc_common::Span,
    source_binding: SavedBinding,
    defining_module: String,
    export: String,
    local: String,
    callable_key: u32,
    #[serde(default)]
    provider_closure: Option<crate::analysis_inputs::ResolvedProviderDependencyClosure>,
    #[serde(default)]
    source_initialized_host_dependencies:
        Option<BTreeSet<crate::analysis_inputs::AdmittedHostModuleDependency>>,
}

#[derive(Serialize, Deserialize)]
struct SavedRequiredImportedPrimitiveRead {
    source_owner: SourceUnitOrdinal,
    source_site: Option<super::sync_effects::SourceOperationSite>,
    consumer_root: Option<SourceUnitOrdinal>,
    source_binding: SavedBinding,
    source_specifier: String,
    imported_name: String,
    provider_module: String,
    provider_export: String,
    dependency_closure: BTreeSet<String>,
    #[serde(default)]
    provider_closure: Option<crate::analysis_inputs::ResolvedProviderDependencyClosure>,
    source_cells: BTreeSet<crate::analysis_inputs::PrimitiveExportCell>,
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
            root_outward_throw: Cow::Borrowed(&calls.root_outward_throw),
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
            heap_access_provenance: SavedHeapAccessProvenance::snapshot(
                &calls.heap_access_provenance,
                ordinals,
            )?,
            operation_support_receipts: calls
                .operation_support_receipts()
                .iter()
                .map(|receipt| {
                    super::sync_effects::SavedOperationSupportReceipt::snapshot(receipt, ordinals)
                })
                .collect::<Result<_>>()?,
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
            imported_primitive_array_spread_input_uses: Cow::Borrowed(
                &calls.imported_primitive_array_spread_input_uses,
            ),
            inference_uses: Cow::Borrowed(&calls.inference_uses),
            callback_factory_uses: Cow::Borrowed(&calls.callback_factory_uses),
            returned_record_uses: Cow::Borrowed(&calls.returned_record_uses),
            author_host_contract_uses: calls
                .author_host_contract_uses
                .iter()
                .map(SavedReentryFreeOperationUse::snapshot)
                .collect(),
            required_initialized_module_cells: calls
                .required_initialized_module_cells
                .iter()
                .map(|cell| ordinals.snapshot_module_cell(*cell))
                .collect::<Result<_>>()?,
            conditional_provider_targets: calls
                .conditional_provider_targets
                .iter()
                .map(|requirement| {
                    validate_provider_closure_namespace(
                        requirement.provider_closure.as_ref(),
                        ordinals,
                    )?;
                    validate_host_dependency_namespace(
                        requirement.source_initialized_host_dependencies.as_ref(),
                        ordinals,
                    )?;
                    Ok(SavedConditionalProviderTargetRequirement {
                        owner: requirement.owner,
                        call_span: requirement.call_span,
                        source_binding: ordinals.snapshot_binding(&requirement.source_binding)?,
                        defining_module: requirement.defining_module.clone(),
                        export: requirement.export.clone(),
                        local: requirement.local.clone(),
                        callable_key: requirement.callable_key,
                        provider_closure: requirement.provider_closure.clone(),
                        source_initialized_host_dependencies: requirement
                            .source_initialized_host_dependencies
                            .clone(),
                    })
                })
                .collect::<Result<_>>()?,
            required_imported_primitive_reads: calls
                .required_imported_primitive_reads
                .iter()
                .map(|requirement| {
                    validate_provider_closure_namespace(
                        requirement.provider_closure.as_ref(),
                        ordinals,
                    )?;
                    Ok(SavedRequiredImportedPrimitiveRead {
                        source_owner: requirement.source_owner,
                        source_site: requirement.source_site.clone(),
                        consumer_root: requirement.consumer_root,
                        source_binding: ordinals.snapshot_binding(&requirement.source_binding)?,
                        source_specifier: requirement.source_specifier.clone(),
                        imported_name: requirement.imported_name.clone(),
                        provider_module: requirement.provider_module.clone(),
                        provider_export: requirement.provider_export.clone(),
                        dependency_closure: requirement.dependency_closure.clone(),
                        provider_closure: requirement.provider_closure.clone(),
                        source_cells: requirement.source_cells.clone(),
                    })
                })
                .collect::<Result<_>>()?,
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
        let mut required_initialized_module_cells = BTreeSet::new();
        for saved in self.required_initialized_module_cells {
            let cell = ordinals.restore_module_cell(saved)?;
            ensure!(
                required_initialized_module_cells.insert(cell),
                "duplicate saved required initialized module cell {cell:?}"
            );
        }
        let conditional_provider_targets = self
            .conditional_provider_targets
            .into_iter()
            .map(|requirement| {
                validate_provider_closure_namespace(
                    requirement.provider_closure.as_ref(),
                    ordinals,
                )?;
                validate_host_dependency_namespace(
                    requirement.source_initialized_host_dependencies.as_ref(),
                    ordinals,
                )?;
                Ok(
                    super::statement_facts::ConditionalProviderTargetRequirement {
                        owner: requirement.owner,
                        call_span: requirement.call_span,
                        source_binding: ordinals.restore_binding(requirement.source_binding)?,
                        defining_module: requirement.defining_module,
                        export: requirement.export,
                        local: requirement.local,
                        callable_key: requirement.callable_key,
                        provider_closure: requirement.provider_closure,
                        source_initialized_host_dependencies: requirement
                            .source_initialized_host_dependencies,
                    },
                )
            })
            .collect::<Result<_>>()?;
        let mut required_imported_primitive_reads = Vec::new();
        for requirement in self.required_imported_primitive_reads {
            validate_provider_closure_namespace(requirement.provider_closure.as_ref(), ordinals)?;
            let restored = super::statement_facts::RequiredImportedPrimitiveRead {
                source_owner: requirement.source_owner,
                source_site: requirement.source_site,
                consumer_root: requirement.consumer_root,
                source_binding: ordinals.restore_binding(requirement.source_binding)?,
                source_specifier: requirement.source_specifier,
                imported_name: requirement.imported_name,
                provider_module: requirement.provider_module,
                provider_export: requirement.provider_export,
                dependency_closure: requirement.dependency_closure,
                provider_closure: requirement.provider_closure,
                source_cells: requirement.source_cells,
            };
            ensure!(
                !required_imported_primitive_reads.contains(&restored),
                "duplicate saved required imported primitive read for {:?}",
                restored.source_site
            );
            required_imported_primitive_reads.push(restored);
        }
        Ok(SynchronousCalls {
            initializer_binding_effects: self.initializer_binding_effects.into_owned(),
            completed_initialization: self.completed_initialization.into_owned(),
            root_normal_completion: self.root_normal_completion.into_owned(),
            root_outward_throw: self.root_outward_throw.into_owned(),
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
            heap_access_provenance: self.heap_access_provenance.restore(ordinals)?,
            operation_support_receipts: (!self.operation_support_receipts.is_empty())
                .then(|| {
                    self.operation_support_receipts
                        .into_iter()
                        .map(|receipt| receipt.restore(ordinals))
                        .collect::<Result<Vec<_>>>()
                        .map(Vec::into_boxed_slice)
                })
                .transpose()?,
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
            imported_primitive_array_spread_input_uses: self
                .imported_primitive_array_spread_input_uses
                .into_owned(),
            inference_uses: self.inference_uses.into_owned(),
            callback_factory_uses: self.callback_factory_uses.into_owned(),
            returned_record_uses: self.returned_record_uses.into_owned(),
            author_host_contract_uses: self
                .author_host_contract_uses
                .into_iter()
                .map(SavedReentryFreeOperationUse::restore)
                .collect(),
            required_initialized_module_cells,
            conditional_provider_targets,
            required_imported_primitive_reads,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProofDependencies;
    use crate::facts::abstract_domain::value::SourceHeapResolver;
    use crate::purity::{PurityReason, PurityRule};
    use crate::reports::schema::PurityCompletionDisagreement;
    use analysis_codec::{SavedChunk, SavedModuleCell};
    use source_identity::{ChunkId, ChunkTable, ModuleCellId};
    use std::sync::Arc;
    use swc_common::{BytePos, GLOBALS, Globals, Mark, Span, SyntaxContext};

    #[test]
    fn saved_synchronous_calls_require_binding_effect_authority() {
        let mut chunks = ChunkTable::default();
        let module = chunks.intern("module".to_owned());
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let mut purities = PuritySnapshot::default();
        let mut calls = SynchronousCalls::default();
        calls.record_operation_support(
            super::super::sync_effects::OperationSupportReceipt::pooled_object_shape(
                super::super::sync_effects::OperationSupportOperation::OwnDataRead,
                crate::facts::abstract_domain::value::HeapAddress::module_family(
                    module,
                    crate::facts::abstract_domain::value::Kind::Object,
                    3,
                ),
                &ProofDependencies::default(),
            ),
        );
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
            .expect("default facts snapshot");
        let mut encoded = serde_json::to_value(saved).expect("saved facts serialize");
        assert_eq!(
            encoded["operation_support_receipts"][0]["support"]["origin"]["module"], "module",
            "saved operation-support identity uses the checked source chunk name"
        );
        serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded.clone())
            .expect("complete saved facts deserialize");
        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("other".to_owned());
        let reader_module = reader_chunks.intern("module".to_owned());
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let decoded: SavedSynchronousCalls<'_> =
            serde_json::from_value(encoded.clone()).expect("saved facts decode");
        let restored = decoded
            .restore(
                &reader_ordinals,
                &PuritySnapshot::default().finish().restore().unwrap(),
            )
            .expect("saved support address restores by chunk name");
        assert_eq!(
            restored.operation_support_receipts()[0].projected_cell(),
            Some(crate::facts::EffectCell::ObjectShape(
                crate::facts::abstract_domain::value::HeapAddress::module_family(
                    reader_module,
                    crate::facts::abstract_domain::value::Kind::Object,
                    3,
                ),
            ))
        );
        encoded
            .as_object_mut()
            .expect("saved synchronous facts are a struct")
            .remove("binding_effect_coverage");
        assert!(serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded).is_err());

        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
            .expect("support receipt snapshot");
        let mut encoded = serde_json::to_value(saved).expect("saved facts serialize");
        encoded
            .as_object_mut()
            .expect("saved synchronous facts are a struct")
            .remove("operation_support_receipts");
        assert!(serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded).is_err());

        for field in [
            "root_normal_completion",
            "root_outward_throw",
            "required_initialized_module_cells",
            "conditional_provider_targets",
            "required_imported_primitive_reads",
            "heap_access_provenance",
        ] {
            let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut purities)
                .expect("control receipt snapshot");
            let mut encoded = serde_json::to_value(saved).expect("saved facts serialize");
            encoded
                .as_object_mut()
                .expect("saved synchronous facts are a struct")
                .remove(field);
            assert!(
                serde_json::from_value::<SavedSynchronousCalls<'_>>(encoded).is_err(),
                "missing {field} must not silently erase control evidence"
            );
        }
    }

    #[test]
    fn structural_completeness_does_not_restore_normal_or_abrupt_completion() {
        let chunks = ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let calls = SynchronousCalls {
            complete: true,
            ..SynchronousCalls::default()
        };
        let mut snapshot = PuritySnapshot::default();
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut snapshot)
            .expect("structural execution facts snapshot");
        let bytes = rmp_serde::to_vec_named(&saved).expect("saved execution facts encode");
        let saved: SavedSynchronousCalls<'_> =
            rmp_serde::from_slice(&bytes).expect("saved execution facts decode");
        let purities = snapshot
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let restored = saved
            .restore(&ordinals, &purities)
            .expect("structural execution facts restore");

        assert!(restored.complete);
        assert!(restored.root_normal_completion.is_none());
        assert!(restored.root_outward_throw.is_none());
    }

    #[test]
    fn heap_access_provenance_survives_saved_round_trip() {
        let mut chunks = ChunkTable::default();
        let module = chunks.intern("module".to_owned());
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let address = HeapAddress::module_family(module, Kind::Array, 4);
        let direct = EffectCell::ArrayContents(address);
        let summary = EffectCell::OwnProperty {
            origin: address,
            property: "length".to_owned(),
        };
        let mut calls = SynchronousCalls::default();
        calls.heap_contents.reads.insert(direct.clone());
        calls.heap_contents.writes.insert(summary.clone());
        calls
            .heap_access_provenance
            .executed_operations
            .reads
            .insert(direct.clone());
        calls
            .heap_access_provenance
            .assumed_operations
            .writes
            .insert(direct.clone());
        calls
            .heap_access_provenance
            .conservative_summaries
            .writes
            .insert(summary);

        let mut snapshot = PuritySnapshot::default();
        let saved = SavedSynchronousCalls::snapshot(&calls, &ordinals, &mut snapshot)
            .expect("heap provenance snapshot");
        let bytes = rmp_serde::to_vec_named(&saved).expect("heap provenance encode");
        let saved: SavedSynchronousCalls<'_> =
            rmp_serde::from_slice(&bytes).expect("heap provenance decode");
        let purities = snapshot.finish().restore().expect("empty purity restore");
        let restored = saved
            .restore(&ordinals, &purities)
            .expect("heap provenance restore");

        assert_eq!(restored.heap_contents, calls.heap_contents);
        assert_eq!(
            restored.heap_access_provenance,
            calls.heap_access_provenance
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
                    dependencies: Box::default(),
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
        let chunks = ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
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

    #[test]
    fn saved_module_cells_resolve_by_checked_chunk_name() {
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("static/provider".to_owned());
        writer_chunks.intern("static/other".to_owned());
        let writer_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let cell = EffectCell::ModuleCell(ModuleCellId {
            chunk: writer_provider,
            declaration_origin: 17,
        });
        let saved = SavedEffectCell::snapshot(&cell, &writer_ordinals).expect("snapshot cell");
        let bytes = rmp_serde::to_vec_named(&saved).expect("encode saved cell");

        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("static/other".to_owned());
        let reader_provider = reader_chunks.intern("static/provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let decoded: SavedEffectCell = rmp_serde::from_slice(&bytes).expect("decode saved cell");
        assert_eq!(
            decoded.restore(&reader_ordinals).expect("restore cell"),
            EffectCell::ModuleCell(ModuleCellId {
                chunk: reader_provider,
                declaration_origin: 17,
            }),
        );

        assert!(
            writer_ordinals
                .snapshot_chunk(ChunkId(writer_chunks.len()))
                .is_err(),
            "unknown runtime chunk IDs must refuse snapshot"
        );
        let unknown_name = SavedEffectCell::ModuleCell(SavedModuleCell {
            chunk: SavedChunk("static/missing".to_owned()),
            declaration_origin: 17,
        });
        assert!(
            unknown_name.restore(&reader_ordinals).is_err(),
            "unknown saved chunk names must refuse restore"
        );
    }

    #[test]
    fn saved_heap_effect_cells_preserve_qualified_addresses_and_reject_local_scopes() {
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("static/provider".to_owned());
        let writer_other = writer_chunks.intern("static/other".to_owned());
        let writer_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let class = ClassDefinitionId {
            module: HeapModuleScope::Chunk(writer_provider),
            origin: 23,
        };
        let provider_array = HeapAddress::module_family(writer_provider, Kind::Object, 17);
        let descriptor_map = HeapAddress::module_family(writer_provider, Kind::Object, 18);
        let provider_output = SourceHeapResolver::new(HeapModuleScope::Chunk(writer_provider))
            .provider_allocation(
                51,
                ProviderCallableId {
                    module: HeapModuleScope::Chunk(writer_other),
                    origin: 61,
                },
                2,
                Kind::Object,
            )
            .with_verified_producer_birth(77);
        let unverified_provider_output =
            SourceHeapResolver::new(HeapModuleScope::Chunk(writer_provider)).provider_allocation(
                51,
                ProviderCallableId {
                    module: HeapModuleScope::Chunk(writer_other),
                    origin: 61,
                },
                2,
                Kind::Object,
            );
        let mut forged_wire = serde_json::to_value(provider_output).expect("serialize address");
        assert!(
            forged_wire.get("producer_birth").is_none(),
            "runtime producer-birth proof is not serialized"
        );
        forged_wire["producer_birth"] = serde_json::json!(88);
        let wire_restored = serde_json::from_value::<HeapAddress>(forged_wire)
            .expect("wire codec ignores untrusted runtime-only proof fields");
        assert_eq!(wire_restored, unverified_provider_output);
        let another_birth = unverified_provider_output.with_verified_producer_birth(99);
        assert!(
            wire_restored.may_overlap_with_provider_family(another_birth),
            "forged serialized birth cannot narrow restored overlap"
        );
        assert_eq!(
            SavedHeapAddress::snapshot(provider_output, &writer_ordinals)
                .expect("snapshot verified provider output"),
            SavedHeapAddress::snapshot(unverified_provider_output, &writer_ordinals)
                .expect("snapshot unverified provider output"),
            "saved identities omit runtime-only birth proof"
        );
        let cells = [
            EffectCell::ArrayContents(provider_array),
            EffectCell::SetContents(HeapAddress::module_family(writer_provider, Kind::Set, 17)),
            EffectCell::ObjectShape(HeapAddress::ClassObject { class }),
            EffectCell::DescriptorMapContents(descriptor_map),
            EffectCell::OwnProperty {
                origin: provider_array,
                property: "field".into(),
            },
            EffectCell::PrivateSlot {
                origin: HeapAddress::constructor_instances(class),
                class,
                name: "privateField".into(),
            },
            EffectCell::ArrayContents(HeapAddress::module_family(writer_other, Kind::Object, 17)),
            EffectCell::ObjectShape(provider_output),
        ];
        let saved = cells
            .iter()
            .map(|cell| SavedEffectCell::snapshot(cell, &writer_ordinals).expect("save cell"))
            .collect::<Vec<_>>();
        let saved_provider_output = serde_json::to_value(&saved[7]).expect("serialize saved cell");
        assert!(
            !saved_provider_output.to_string().contains("producer_birth"),
            "saved cell codec has no runtime producer-birth field"
        );

        let mut reader_chunks = ChunkTable::default();
        let reader_other = reader_chunks.intern("static/other".to_owned());
        let reader_provider = reader_chunks.intern("static/provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        assert_ne!(writer_other, reader_other);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let native_array = HeapAddress::module_family(writer_provider, Kind::Array, 19);
        let saved_native_array =
            SavedEffectCell::snapshot(&EffectCell::ArrayContents(native_array), &writer_ordinals)
                .expect("save native Array contents");
        assert_eq!(
            saved_native_array
                .restore(&reader_ordinals)
                .expect("restore native Array contents"),
            EffectCell::ArrayContents(HeapAddress::module_family(reader_provider, Kind::Array, 19))
        );
        let restored = saved
            .into_iter()
            .map(|cell| {
                cell.restore(&reader_ordinals)
                    .expect("restore by chunk name")
            })
            .collect::<Vec<_>>();
        assert_eq!(
            restored[0],
            EffectCell::ArrayContents(HeapAddress::module_family(
                reader_provider,
                Kind::Object,
                17
            ))
        );
        assert_eq!(
            restored[1],
            EffectCell::SetContents(HeapAddress::module_family(reader_provider, Kind::Set, 17))
        );
        assert_eq!(
            restored[2],
            EffectCell::ObjectShape(HeapAddress::ClassObject {
                class: ClassDefinitionId {
                    module: HeapModuleScope::Chunk(reader_provider),
                    origin: 23
                }
            })
        );
        assert_eq!(
            restored[3],
            EffectCell::DescriptorMapContents(HeapAddress::module_family(
                reader_provider,
                Kind::Object,
                18
            ))
        );
        assert_eq!(
            restored[4],
            EffectCell::OwnProperty {
                origin: HeapAddress::module_family(reader_provider, Kind::Object, 17),
                property: "field".into()
            }
        );
        assert_eq!(
            restored[5],
            EffectCell::PrivateSlot {
                origin: HeapAddress::constructor_instances(ClassDefinitionId {
                    module: HeapModuleScope::Chunk(reader_provider),
                    origin: 23
                }),
                class: ClassDefinitionId {
                    module: HeapModuleScope::Chunk(reader_provider),
                    origin: 23
                },
                name: "privateField".into(),
            }
        );
        assert_eq!(
            restored[6],
            EffectCell::ArrayContents(HeapAddress::module_family(reader_other, Kind::Object, 17))
        );
        assert_eq!(
            restored[7],
            EffectCell::ObjectShape(
                SourceHeapResolver::new(HeapModuleScope::Chunk(reader_provider,))
                    .provider_allocation(
                        51,
                        ProviderCallableId {
                            module: HeapModuleScope::Chunk(reader_other),
                            origin: 61,
                        },
                        2,
                        Kind::Object,
                    )
            )
        );
        let EffectCell::ObjectShape(restored_provider_address) = &restored[7] else {
            unreachable!("expected restored provider output shape")
        };
        assert!(restored_provider_address.may_overlap_with_provider_family(
            unverified_provider_output.with_verified_producer_birth(78)
        ));
        assert_ne!(
            restored[0], restored[6],
            "same offsets in different chunks remain distinct"
        );

        let unknown_heap_chunk =
            SavedEffectCell::ArrayContents(SavedHeapAddress::AllocationFamily {
                module: SavedChunk("static/missing".to_owned()),
                origin: 17,
                kind: Kind::Object,
            });
        assert!(
            unknown_heap_chunk.restore(&reader_ordinals).is_err(),
            "unknown saved heap module names must be rejected"
        );
        let unknown_class_chunk = SavedEffectCell::PrivateSlot {
            origin: SavedHeapAddress::AllocationFamily {
                module: SavedChunk("static/provider".to_owned()),
                origin: 17,
                kind: Kind::Object,
            },
            class: SavedClassDefinitionId {
                module: SavedChunk("static/missing".to_owned()),
                origin: 23,
            },
            name: "privateField".into(),
        };
        assert!(
            unknown_class_chunk.restore(&reader_ordinals).is_err(),
            "unknown saved class modules must be rejected"
        );

        let class_as_allocation = SavedHeapAddress::AllocationFamily {
            module: SavedChunk("static/provider".to_owned()),
            origin: 23,
            kind: Kind::Class,
        };
        assert!(
            class_as_allocation
                .clone()
                .restore(&reader_ordinals)
                .is_err(),
            "saved allocation families cannot encode class definitions"
        );
        assert!(
            SavedHeapAddress::snapshot(
                HeapAddress::AllocationFamily {
                    module: HeapModuleScope::Chunk(reader_provider),
                    origin: 23,
                    kind: Kind::Class,
                },
                &reader_ordinals
            )
            .is_err(),
            "in-memory allocation families cannot encode class definitions"
        );
        let local_producer = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(reader_provider),
            call_origin: 51,
            producer: ProviderCallableId {
                module: HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()),
                origin: 61,
            },
            producer_birth: None,
            slot: 2,
            kind: Kind::Object,
        };
        assert!(
            SavedHeapAddress::snapshot(local_producer, &reader_ordinals).is_err(),
            "provider producers in local analysis scopes cannot cross the saved boundary"
        );
        let local_consumer =
            SourceHeapResolver::new(HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()))
                .provider_allocation(
                    51,
                    ProviderCallableId {
                        module: HeapModuleScope::Chunk(reader_provider),
                        origin: 61,
                    },
                    2,
                    Kind::Object,
                );
        assert!(
            SavedHeapAddress::snapshot(local_consumer, &reader_ordinals).is_err(),
            "provider consumers in local analysis scopes cannot cross the saved boundary"
        );
        let unknown_provider_module = SavedHeapAddress::ProviderAllocationFamily {
            consumer: SavedChunk("static/provider".to_owned()),
            call_origin: 51,
            producer: SavedProviderCallableId {
                module: SavedChunk("static/missing".to_owned()),
                origin: 61,
            },
            slot: 2,
            kind: Kind::Object,
        };
        assert!(
            unknown_provider_module.restore(&reader_ordinals).is_err(),
            "unknown producer module names must be rejected on restore"
        );
        let mut malformed_provider = serde_json::to_value(provider_output).expect("save recipe");
        malformed_provider["kind"] = serde_json::to_value(Kind::Class).expect("encode class");
        assert!(
            serde_json::from_value::<HeapAddress>(malformed_provider).is_err(),
            "provider recipes cannot encode class definitions"
        );
        let mut malformed_address = serde_json::to_value(provider_array).expect("save address");
        malformed_address["kind"] = serde_json::to_value(Kind::Class).expect("encode kind");
        assert!(
            serde_json::from_value::<HeapAddress>(malformed_address).is_err(),
            "direct typed-address decoding must reject class allocation families"
        );
        assert!(
            serde_json::to_value(Value::Alloc(Kind::Function, provider_array)).is_err(),
            "direct typed-value encoding must reject a mismatched allocation kind"
        );
        let mut malformed_value = serde_json::to_value(Value::Alloc(
            Kind::Function,
            HeapAddress::module_family(reader_provider, Kind::Function, 23),
        ))
        .expect("save value");
        malformed_value["Alloc"][0] = serde_json::to_value(Kind::Object).expect("encode kind");
        assert!(
            serde_json::from_value::<Value>(malformed_value).is_err(),
            "direct typed-value decoding must reject a mismatched allocation kind"
        );
        for cell in [
            SavedEffectCell::ArrayContents(SavedHeapAddress::ConstructorInstances {
                class: SavedClassDefinitionId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
            }),
            SavedEffectCell::ArrayContents(SavedHeapAddress::ProviderAllocationFamily {
                consumer: SavedChunk("static/other".to_owned()),
                call_origin: 31,
                producer: SavedProviderCallableId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
                slot: 0,
                kind: Kind::Object,
            }),
            SavedEffectCell::SetContents(SavedHeapAddress::ProviderAllocationFamily {
                consumer: SavedChunk("static/other".to_owned()),
                call_origin: 31,
                producer: SavedProviderCallableId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
                slot: 0,
                kind: Kind::Set,
            }),
            SavedEffectCell::DescriptorMapContents(SavedHeapAddress::ProviderAllocationFamily {
                consumer: SavedChunk("static/other".to_owned()),
                call_origin: 31,
                producer: SavedProviderCallableId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
                slot: 0,
                kind: Kind::Object,
            }),
            SavedEffectCell::SetContents(SavedHeapAddress::AllocationFamily {
                module: SavedChunk("static/provider".to_owned()),
                origin: 23,
                kind: Kind::Object,
            }),
            SavedEffectCell::DescriptorMapContents(SavedHeapAddress::ClassObject {
                class: SavedClassDefinitionId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
            }),
            SavedEffectCell::DescriptorMapContents(SavedHeapAddress::ConstructorInstances {
                class: SavedClassDefinitionId {
                    module: SavedChunk("static/provider".to_owned()),
                    origin: 23,
                },
            }),
        ] {
            assert!(
                cell.restore(&reader_ordinals).is_err(),
                "heap-content effects must retain their address category"
            );
        }

        let local_scope = HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh());
        let local_class = ClassDefinitionId {
            module: local_scope,
            origin: 23,
        };
        for cell in [
            EffectCell::ArrayContents(HeapAddress::in_scope(local_scope, Kind::Object, 17)),
            EffectCell::PrivateSlot {
                origin: HeapAddress::constructor_instances(local_class),
                class: local_class,
                name: "privateField".into(),
            },
        ] {
            assert!(
                SavedEffectCell::snapshot(&cell, &writer_ordinals).is_err(),
                "local analysis heap identities cannot cross the saved chunk boundary"
            );
        }
    }

    #[test]
    fn saved_global_object_address_needs_no_synthetic_chunk() {
        let chunks = ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);

        let saved = SavedHeapAddress::snapshot(HeapAddress::GlobalObject, &ordinals)
            .expect("the global singleton has no source namespace to resolve");
        assert_eq!(saved, SavedHeapAddress::GlobalObject);
        assert_eq!(
            saved.restore(&ordinals).expect("restore singleton address"),
            HeapAddress::GlobalObject
        );
    }

    #[test]
    fn saved_required_initialization_cells_use_checked_chunk_names() {
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("static/provider".to_owned());
        writer_chunks.intern("static/other".to_owned());
        let writer_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let mut calls = SynchronousCalls::default();
        calls
            .required_initialized_module_cells
            .insert(ModuleCellId {
                chunk: writer_provider,
                declaration_origin: 23,
            });
        let mut purity_snapshot = PuritySnapshot::default();
        let saved = SavedSynchronousCalls::snapshot(&calls, &writer_ordinals, &mut purity_snapshot)
            .expect("snapshot required initialization cell");
        let encoded = serde_json::to_value(saved).expect("serialize required cell");
        assert_eq!(
            encoded["required_initialized_module_cells"][0]["chunk"],
            "static/provider"
        );

        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("static/other".to_owned());
        let reader_provider = reader_chunks.intern("static/provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let restored_purities = PuritySnapshot::default().finish().restore().unwrap();
        let decoded: SavedSynchronousCalls<'_> =
            serde_json::from_value(encoded.clone()).expect("decode required cell");
        let restored = decoded
            .restore(&reader_ordinals, &restored_purities)
            .expect("restore by checked chunk name");
        assert_eq!(
            restored.required_initialized_module_cells,
            BTreeSet::from([ModuleCellId {
                chunk: reader_provider,
                declaration_origin: 23,
            }])
        );

        let mut duplicate = encoded.clone();
        let saved_cell = duplicate["required_initialized_module_cells"][0].clone();
        duplicate["required_initialized_module_cells"]
            .as_array_mut()
            .unwrap()
            .push(saved_cell);
        let decoded: SavedSynchronousCalls<'_> =
            serde_json::from_value(duplicate).expect("decode duplicate cells");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities)
                .is_err(),
            "duplicate saved required cells must be rejected"
        );

        let mut unknown = encoded;
        unknown["required_initialized_module_cells"][0]["chunk"] =
            serde_json::json!("static/missing");
        let decoded: SavedSynchronousCalls<'_> =
            serde_json::from_value(unknown).expect("decode unknown saved name");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities)
                .is_err(),
            "unknown saved chunk names must be rejected"
        );
    }

    #[test]
    fn saved_conditional_provider_target_preserves_source_binding_and_provider() {
        let mut writer_chunks = ChunkTable::default();
        writer_chunks.intern("static/other".to_owned());
        writer_chunks.intern("static/provider".to_owned());
        writer_chunks.intern("static/bridge".to_owned());
        let host_dependency = crate::analysis_inputs::AdmittedHostModuleDependency {
            source_module: "static/provider".to_owned(),
            specifier: "path".to_owned(),
            module: spec::HostModule::Node(spec::NodeBuiltinModule::Path),
            consumed_premise: crate::analysis_inputs::HostBehaviorKey::reentry_free_module(
                spec::HostModule::Node(spec::NodeBuiltinModule::Path),
            ),
        };
        let (encoded, source_binding) = GLOBALS.set(&Globals::new(), || {
            let context = SyntaxContext::empty().apply_mark(Mark::new());
            let writer_ordinals =
                ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
            let mut writer_ordinals = writer_ordinals;
            writer_ordinals.visit_syntax_context(&context);
            let source_binding = ("importedCall".into(), context);
            let requirement = super::super::statement_facts::ConditionalProviderTargetRequirement {
                owner: SourceUnitOrdinal(11),
                call_span: Span::new(BytePos(21), BytePos(29)),
                source_binding: source_binding.clone(),
                defining_module: "static/provider/logic.js".to_owned(),
                export: "call".to_owned(),
                local: "callImpl".to_owned(),
                callable_key: 701,
                provider_closure: Some(crate::analysis_inputs::ResolvedProviderDependencyClosure {
                    modules: BTreeSet::from([
                        "static/provider".to_owned(),
                        "static/bridge".to_owned(),
                    ]),
                    admitted_host_modules: BTreeSet::from([host_dependency.clone()]),
                }),
                source_initialized_host_dependencies: Some(BTreeSet::from([
                    host_dependency.clone()
                ])),
            };
            let mut calls = SynchronousCalls::default();
            calls.conditional_provider_targets.push(requirement);
            let mut purity_snapshot = PuritySnapshot::default();
            let saved =
                SavedSynchronousCalls::snapshot(&calls, &writer_ordinals, &mut purity_snapshot)
                    .expect("snapshot provider target");
            (
                serde_json::to_value(saved).expect("serialize provider target"),
                source_binding,
            )
        });
        assert_eq!(
            encoded["conditional_provider_targets"][0]["source_binding"]["symbol"],
            "importedCall"
        );
        assert_ne!(
            encoded["conditional_provider_targets"][0]["source_binding"]["context_ordinal"],
            0
        );
        assert_eq!(
            encoded["conditional_provider_targets"][0]["defining_module"],
            "static/provider/logic.js"
        );

        // Chunk ordinals are artifact-local. The stable module path and saved
        // source binding must survive reconstruction under a different table.
        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("static/provider".to_owned());
        reader_chunks.intern("static/other".to_owned());
        reader_chunks.intern("static/bridge".to_owned());
        GLOBALS.set(&Globals::new(), || {
            let _unrelated_context = SyntaxContext::empty().apply_mark(Mark::new());
            let reader_context = SyntaxContext::empty().apply_mark(Mark::new());
            assert_ne!(reader_context, source_binding.1);
            let reader_ordinals =
                ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
            let mut reader_ordinals = reader_ordinals;
            reader_ordinals.visit_syntax_context(&reader_context);
            let restored_purities = PuritySnapshot::default().finish().restore().unwrap();
            let mut legacy_encoded = encoded.clone();
            let legacy_target = legacy_encoded["conditional_provider_targets"][0]
                .as_object_mut()
                .expect("conditional provider target is a map");
            legacy_target.remove("provider_closure");
            legacy_target.remove("source_initialized_host_dependencies");
            let decoded: SavedSynchronousCalls<'_> =
                serde_json::from_value(legacy_encoded).expect("decode legacy provider target");
            let legacy_restored = decoded
                .restore(&reader_ordinals, &restored_purities)
                .expect("restore legacy provider target as incomplete");
            assert!(
                legacy_restored.conditional_provider_targets[0]
                    .provider_closure
                    .is_none()
            );
            assert!(
                legacy_restored.conditional_provider_targets[0]
                    .source_initialized_host_dependencies
                    .is_none()
            );

            let decoded: SavedSynchronousCalls<'_> =
                serde_json::from_value(encoded).expect("decode provider target");
            let restored = decoded
                .restore(&reader_ordinals, &restored_purities)
                .expect("restore provider target");
            assert_eq!(
                restored.conditional_provider_targets,
                vec![
                    super::super::statement_facts::ConditionalProviderTargetRequirement {
                        owner: SourceUnitOrdinal(11),
                        call_span: Span::new(BytePos(21), BytePos(29)),
                        source_binding: (source_binding.0, reader_context),
                        defining_module: "static/provider/logic.js".to_owned(),
                        export: "call".to_owned(),
                        local: "callImpl".to_owned(),
                        callable_key: 701,
                        provider_closure: Some(
                            crate::analysis_inputs::ResolvedProviderDependencyClosure {
                                modules: BTreeSet::from([
                                    "static/provider".to_owned(),
                                    "static/bridge".to_owned(),
                                ]),
                                admitted_host_modules: BTreeSet::from([host_dependency.clone(),]),
                            },
                        ),
                        source_initialized_host_dependencies: Some(BTreeSet::from([
                            host_dependency,
                        ])),
                    }
                ]
            );
        });
    }

    #[test]
    fn saved_imported_primitive_read_preserves_site_binding_and_provider_closure() {
        use super::super::sync_effects::{LocalBodyIdentity, OperationStep, SourceOperationSite};

        let mut writer_chunks = ChunkTable::default();
        writer_chunks.intern("static/provider".to_owned());
        writer_chunks.intern("static/bridge".to_owned());
        writer_chunks.intern("static/consumer".to_owned());
        let (encoded, source_binding, source_site) = GLOBALS.set(&Globals::new(), || {
            let context = SyntaxContext::empty().apply_mark(Mark::new());
            let writer_ordinals =
                ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
            let mut writer_ordinals = writer_ordinals;
            writer_ordinals.visit_syntax_context(&context);
            let source_binding = ("limit".into(), context);
            let source_site = SourceOperationSite {
                owner: SourceUnitOrdinal(11),
                body: LocalBodyIdentity::Callable {
                    key: 701,
                    owner: SourceUnitOrdinal(11),
                },
                span: Span::new(BytePos(21), BytePos(29)),
                ordinal: 17,
                step: OperationStep::Expression,
                control_test_ordinal: None,
            };
            let requirement = super::super::statement_facts::RequiredImportedPrimitiveRead {
                source_owner: SourceUnitOrdinal(11),
                source_site: Some(source_site.clone()),
                consumer_root: Some(SourceUnitOrdinal(3)),
                source_binding: source_binding.clone(),
                source_specifier: "./provider.js".to_owned(),
                imported_name: "limit".to_owned(),
                provider_module: "static/provider".to_owned(),
                provider_export: "limit".to_owned(),
                dependency_closure: BTreeSet::from([
                    "static/bridge".to_owned(),
                    "static/provider".to_owned(),
                ]),
                provider_closure: Some(crate::analysis_inputs::ResolvedProviderDependencyClosure {
                    modules: BTreeSet::from([
                        "static/bridge".to_owned(),
                        "static/provider".to_owned(),
                    ]),
                    admitted_host_modules: BTreeSet::new(),
                }),
                source_cells: BTreeSet::from([crate::analysis_inputs::PrimitiveExportCell {
                    module: "static/provider".to_owned(),
                    export: "limit".to_owned(),
                }]),
            };
            let mut calls = SynchronousCalls::default();
            calls.require_imported_primitive_read(requirement);
            calls.require_imported_primitive_read(
                super::super::statement_facts::RequiredImportedPrimitiveRead {
                    source_owner: SourceUnitOrdinal(11),
                    source_site: None,
                    consumer_root: None,
                    source_binding: source_binding.clone(),
                    source_specifier: "./provider.js".to_owned(),
                    imported_name: "limit".to_owned(),
                    provider_module: "static/provider".to_owned(),
                    provider_export: "limit".to_owned(),
                    dependency_closure: BTreeSet::from([
                        "static/bridge".to_owned(),
                        "static/provider".to_owned(),
                    ]),
                    provider_closure: None,
                    source_cells: BTreeSet::from([crate::analysis_inputs::PrimitiveExportCell {
                        module: "static/provider".to_owned(),
                        export: "limit".to_owned(),
                    }]),
                },
            );
            let mut purity_snapshot = PuritySnapshot::default();
            let saved =
                SavedSynchronousCalls::snapshot(&calls, &writer_ordinals, &mut purity_snapshot)
                    .expect("snapshot imported primitive read");
            (
                serde_json::to_value(saved).expect("serialize imported primitive read"),
                source_binding,
                source_site,
            )
        });
        assert_eq!(
            encoded["required_imported_primitive_reads"][0]["source_binding"]["symbol"],
            "limit"
        );
        assert_ne!(
            encoded["required_imported_primitive_reads"][0]["source_binding"]["context_ordinal"],
            0
        );
        assert_eq!(
            encoded["required_imported_primitive_reads"][0]["source_specifier"],
            "./provider.js"
        );
        assert_eq!(
            encoded["required_imported_primitive_reads"][0]["provider_module"],
            "static/provider"
        );
        assert!(encoded["required_imported_primitive_reads"][1]["source_site"].is_null());
        assert!(encoded["required_imported_primitive_reads"][1]["consumer_root"].is_null());

        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("static/consumer".to_owned());
        reader_chunks.intern("static/provider".to_owned());
        reader_chunks.intern("static/bridge".to_owned());
        GLOBALS.set(&Globals::new(), || {
            let _unrelated_context = SyntaxContext::empty().apply_mark(Mark::new());
            let reader_context = SyntaxContext::empty().apply_mark(Mark::new());
            assert_ne!(reader_context, source_binding.1);
            let mut reader_ordinals =
                ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
            reader_ordinals.visit_syntax_context(&reader_context);
            let restored_purities = PuritySnapshot::default().finish().restore().unwrap();
            let decoded: SavedSynchronousCalls<'_> =
                serde_json::from_value(encoded).expect("decode imported primitive read");
            let restored = decoded
                .restore(&reader_ordinals, &restored_purities)
                .expect("restore imported primitive read");
            assert_eq!(restored.required_imported_primitive_reads.len(), 2);
            let Some(requirement) = restored
                .required_imported_primitive_reads
                .iter()
                .find(|requirement| requirement.source_site.is_some())
            else {
                panic!("canonical imported primitive read is restored")
            };
            assert_eq!(
                requirement.source_binding,
                (source_binding.0.clone(), reader_context)
            );
            assert_eq!(requirement.source_site, Some(source_site));
            assert_eq!(requirement.consumer_root, Some(SourceUnitOrdinal(3)));
            assert_eq!(requirement.imported_name, "limit");
            assert_eq!(requirement.provider_export, "limit");
            assert_eq!(
                requirement.dependency_closure,
                BTreeSet::from(["static/bridge".to_owned(), "static/provider".to_owned(),])
            );
            let Some(missing_identity) = restored
                .required_imported_primitive_reads
                .iter()
                .find(|requirement| requirement.source_site.is_none())
            else {
                panic!("missing imported-read site remains explicit after restore")
            };
            assert_eq!(
                missing_identity.source_binding,
                (source_binding.0, reader_context)
            );
            assert_eq!(missing_identity.consumer_root, None);
        });
    }
}
