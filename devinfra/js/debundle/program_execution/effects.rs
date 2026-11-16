//! Canonical module coordinates for completed program effects. Hygiene is
//! encoded through ContextOrdinals only at the saved-artifact boundary.

use super::{RuntimeAllocationId, RuntimeBindingId, RuntimeSourceScope, RuntimeSourceUnitId};
use crate::execution_evidence::AssumedEffectReason;
use crate::facts::EffectCell;
use crate::facts::sync_effects::interpreter::RuntimeBindingEffects;
use crate::facts::{FallbackOnlyEffects, HeapContentAccesses, HeapContentCells};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(crate) struct ProgramBindingId<B = Id> {
    pub(crate) module: String,
    pub(crate) binding: B,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct ProgramSourceUnitId {
    pub module: String,
    pub ordinal: SourceUnitOrdinal,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(crate) struct ProgramAllocationEvent {
    pub(crate) owner: ProgramSourceUnitId,
    pub(crate) path: Vec<(crate::reports::schema::NativeProgramPointKind, usize)>,
    pub(crate) occurrence: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(crate) struct ProgramAllocationId {
    pub(crate) module: String,
    pub(crate) origin: u32,
    pub(crate) event: Option<ProgramAllocationEvent>,
}

/// What GetBindingValue observed at one particular read site. These states
/// describe the live module cell, so declaration identity alone never implies
/// that the binding was ready at that point in evaluation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum AvailabilityState {
    InitializedValue,
    InitializedUndefined,
    TemporalDeadZone,
    Unknown,
    /// A fallback hypothesized this read; it did not observe a live cell.
    Assumed,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(bound(deserialize = "B: Ord + Deserialize<'de>"))]
pub(crate) enum ProgramAvailabilityCell<B = Id> {
    Binding(ProgramBindingId<B>),
    Default(ProgramSourceUnitId),
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum RuntimeAvailabilityCell {
    Binding(RuntimeBindingId),
    Default(RuntimeSourceUnitId),
}

/// Anonymous default exports are cells with source-unit identity, rather than
/// invented source bindings. Both the declaration and its observers are
/// qualified; a resolved definition does not imply a normal read.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(bound(deserialize = "O: Ord + Deserialize<'de>"))]
pub(crate) struct DefaultExportReadEffects<O> {
    pub(crate) reads: BTreeMap<O, BTreeSet<O>>,
    pub(crate) uninitialized_reads: BTreeSet<O>,
    pub(crate) fallback_only: FallbackOnlyEffects<O>,
}

impl<O> Default for DefaultExportReadEffects<O> {
    fn default() -> Self {
        Self {
            reads: BTreeMap::new(),
            uninitialized_reads: BTreeSet::new(),
            fallback_only: FallbackOnlyEffects::default(),
        }
    }
}

/// `B` is an SWC Id in live facts and a SavedBinding in saved facts. The live
/// specialization is never serialized by the artifact writer.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(bound(deserialize = "B: Ord + Deserialize<'de>"))]
pub(crate) struct ProgramExecutionEffects<B = Id> {
    /// False when execution or any address-to-module mapping was unavailable.
    pub(crate) available: bool,
    pub(crate) default_exports: DefaultExportReadEffects<ProgramSourceUnitId>,
    /// Point-sensitive observations of live module cells. This has its own
    /// completeness bit so missing read identity cannot be mistaken for an
    /// empty, exact trace.
    #[serde(default)]
    pub(crate) availability_reads: BTreeMap<
        ProgramAvailabilityCell<B>,
        BTreeMap<ProgramSourceUnitId, BTreeSet<AvailabilityState>>,
    >,
    #[serde(default)]
    pub(crate) availability_effects_available: bool,
    /// Whether every runtime binding and source-owner coordinate was mapped.
    /// Defaults to false when loading older saved artifacts, where only the
    /// aggregate `available` bit was recorded.
    #[serde(default)]
    pub(crate) binding_effects_available: bool,
    /// Whether every runtime allocation and heap-cell coordinate was mapped.
    /// Kept separate from binding completeness so an unmapped binding cannot
    /// poison otherwise exact heap effects (and vice versa).
    #[serde(default)]
    pub(crate) heap_effects_available: bool,
    pub(crate) reads: BTreeMap<ProgramBindingId<B>, BTreeSet<ProgramSourceUnitId>>,
    pub(crate) rebinds: BTreeMap<ProgramBindingId<B>, BTreeSet<ProgramSourceUnitId>>,
    pub(crate) uninitialized_reads: BTreeSet<ProgramBindingId<B>>,
    pub(crate) fallback_only: FallbackOnlyEffects<ProgramBindingId<B>>,
    pub(crate) heap_contents: HeapContentCells<ProgramAllocationId, ProgramBindingId<B>>,
    pub(crate) heap_frontiers: BTreeMap<String, (bool, bool)>,
    pub(crate) observed_heap_accesses:
        HeapContentAccesses<ProgramAllocationId, ProgramBindingId<B>>,
    pub(crate) executed_owners: BTreeSet<ProgramSourceUnitId>,
}

impl<B> Default for ProgramExecutionEffects<B> {
    fn default() -> Self {
        Self {
            available: false,
            default_exports: DefaultExportReadEffects::default(),
            availability_reads: BTreeMap::new(),
            availability_effects_available: false,
            binding_effects_available: false,
            heap_effects_available: false,
            reads: BTreeMap::new(),
            rebinds: BTreeMap::new(),
            uninitialized_reads: BTreeSet::new(),
            fallback_only: Default::default(),
            heap_contents: Default::default(),
            heap_frontiers: BTreeMap::new(),
            observed_heap_accesses: Default::default(),
            executed_owners: BTreeSet::new(),
        }
    }
}

struct Normalizer<F, E> {
    module: F,
    event: E,
    complete: bool,
    binding_complete: bool,
    binding_owner_complete: bool,
    heap_complete: bool,
    availability_complete: bool,
}

impl<
    F: Fn(RuntimeSourceScope) -> Option<String>,
    E: Fn(super::RuntimeActivationId) -> Option<ProgramAllocationEvent>,
> Normalizer<F, E>
{
    fn module(&mut self, source: RuntimeSourceScope) -> Option<String> {
        let module = (self.module)(source);
        self.complete &= module.is_some();
        module
    }

    fn binding(&mut self, binding: &RuntimeBindingId) -> Option<ProgramBindingId> {
        let module = self.module(binding.source);
        self.binding_complete &= module.is_some();
        Some(ProgramBindingId {
            module: module?,
            binding: binding.binding.clone(),
        })
    }

    fn owner(&mut self, owner: RuntimeSourceUnitId) -> Option<ProgramSourceUnitId> {
        Some(ProgramSourceUnitId {
            module: self.module(owner.source)?,
            ordinal: owner.ordinal,
        })
    }

    fn binding_owner(&mut self, owner: RuntimeSourceUnitId) -> Option<ProgramSourceUnitId> {
        let module = self.module(owner.source);
        self.binding_owner_complete &= module.is_some();
        Some(ProgramSourceUnitId {
            module: module?,
            ordinal: owner.ordinal,
        })
    }

    fn availability_cell(
        &mut self,
        cell: &RuntimeAvailabilityCell,
    ) -> Option<ProgramAvailabilityCell> {
        match cell {
            RuntimeAvailabilityCell::Binding(binding) => {
                let module = self.module(binding.source);
                self.availability_complete &= module.is_some();
                Some(ProgramAvailabilityCell::Binding(ProgramBindingId {
                    module: module?,
                    binding: binding.binding.clone(),
                }))
            }
            RuntimeAvailabilityCell::Default(owner) => {
                let module = self.module(owner.source);
                self.availability_complete &= module.is_some();
                Some(ProgramAvailabilityCell::Default(ProgramSourceUnitId {
                    module: module?,
                    ordinal: owner.ordinal,
                }))
            }
        }
    }

    fn availability_owner(&mut self, owner: RuntimeSourceUnitId) -> Option<ProgramSourceUnitId> {
        let module = self.module(owner.source);
        self.availability_complete &= module.is_some();
        Some(ProgramSourceUnitId {
            module: module?,
            ordinal: owner.ordinal,
        })
    }

    fn allocation(&mut self, allocation: RuntimeAllocationId) -> Option<ProgramAllocationId> {
        let event = match allocation.activation() {
            Some(id) => {
                let event = (self.event)(id);
                self.complete &= event.is_some();
                self.heap_complete &= event.is_some();
                Some(event?)
            }
            None => None,
        };
        let module = self.module(allocation.source);
        self.heap_complete &= module.is_some();
        Some(ProgramAllocationId {
            module: module?,
            origin: allocation.local,
            event,
        })
    }

    fn cell(
        &mut self,
        cell: &super::RuntimeEffectCell,
    ) -> Option<EffectCell<ProgramAllocationId, ProgramBindingId>> {
        let normalized = (|| {
            Some(match cell {
                EffectCell::Binding(binding) => EffectCell::Binding(self.binding(binding)?),
                EffectCell::GlobalProp(property) => EffectCell::GlobalProp(property.clone()),
                EffectCell::ArrayContents(origin) => {
                    EffectCell::ArrayContents(self.allocation(*origin)?)
                }
                EffectCell::SetContents(origin) => {
                    EffectCell::SetContents(self.allocation(*origin)?)
                }
                EffectCell::ObjectShape(origin) => {
                    EffectCell::ObjectShape(self.allocation(*origin)?)
                }
                EffectCell::OwnProperty { origin, property } => EffectCell::OwnProperty {
                    origin: self.allocation(*origin)?,
                    property: property.clone(),
                },
            })
        })();
        self.heap_complete &= normalized.is_some();
        normalized
    }

    fn lane(
        &mut self,
        lane: &BTreeMap<RuntimeBindingId, BTreeSet<RuntimeSourceUnitId>>,
    ) -> BTreeMap<ProgramBindingId, BTreeSet<ProgramSourceUnitId>> {
        lane.iter()
            .filter_map(|(binding, owners)| {
                let binding = self.binding(binding)?;
                let owners = owners
                    .iter()
                    .filter_map(|owner| self.binding_owner(*owner))
                    .collect();
                Some((binding, owners))
            })
            .collect()
    }

    fn tags(
        &mut self,
        tags: &BTreeMap<RuntimeBindingId, AssumedEffectReason>,
    ) -> BTreeMap<ProgramBindingId, AssumedEffectReason> {
        tags.iter()
            .filter_map(|(binding, reason)| Some((self.binding(binding)?, reason.clone())))
            .collect()
    }
}

impl ProgramExecutionEffects {
    pub(crate) fn normalize(
        bindings: &RuntimeBindingEffects,
        heap: &super::RuntimeHeapContentCells,
        frontiers: &BTreeMap<RuntimeSourceScope, (bool, bool)>,
        observed: &super::RuntimeHeapContentAccesses,
        owners: &BTreeSet<RuntimeSourceUnitId>,
        module: impl Fn(RuntimeSourceScope) -> Option<String>,
        event: impl Fn(super::RuntimeActivationId) -> Option<ProgramAllocationEvent>,
    ) -> Self {
        let mut map = Normalizer {
            module,
            event,
            complete: true,
            binding_complete: true,
            binding_owner_complete: true,
            heap_complete: true,
            availability_complete: true,
        };
        let mut result = Self {
            default_exports: DefaultExportReadEffects {
                reads: bindings
                    .default_exports
                    .reads
                    .iter()
                    .filter_map(|(cell, owners)| {
                        let cell = map.binding_owner(*cell)?;
                        let owners = owners
                            .iter()
                            .filter_map(|owner| map.binding_owner(*owner))
                            .collect();
                        Some((cell, owners))
                    })
                    .collect(),
                uninitialized_reads: bindings
                    .default_exports
                    .uninitialized_reads
                    .iter()
                    .filter_map(|cell| map.binding_owner(*cell))
                    .collect(),
                fallback_only: FallbackOnlyEffects {
                    reads: bindings
                        .default_exports
                        .fallback_only
                        .reads
                        .iter()
                        .filter_map(|(cell, reason)| {
                            Some((map.binding_owner(*cell)?, reason.clone()))
                        })
                        .collect(),
                    rebinds: BTreeMap::new(),
                    uninitialized_reads: bindings
                        .default_exports
                        .fallback_only
                        .uninitialized_reads
                        .iter()
                        .filter_map(|(cell, reason)| {
                            Some((map.binding_owner(*cell)?, reason.clone()))
                        })
                        .collect(),
                },
            },
            availability_reads: bindings
                .availability_reads
                .iter()
                .filter_map(|(cell, observers)| {
                    let cell = map.availability_cell(cell)?;
                    let observers = observers
                        .iter()
                        .filter_map(|(owner, states)| {
                            Some((map.availability_owner(*owner)?, states.clone()))
                        })
                        .collect();
                    Some((cell, observers))
                })
                .collect(),
            availability_effects_available: false,
            reads: map.lane(&bindings.reads),
            rebinds: map.lane(&bindings.rebinds),
            uninitialized_reads: bindings
                .uninitialized_reads
                .iter()
                .filter_map(|binding| map.binding(binding))
                .collect(),
            fallback_only: FallbackOnlyEffects {
                reads: map.tags(&bindings.fallback_only.reads),
                rebinds: map.tags(&bindings.fallback_only.rebinds),
                uninitialized_reads: map.tags(&bindings.fallback_only.uninitialized_reads),
            },
            heap_contents: HeapContentCells {
                allocations: heap
                    .allocations
                    .iter()
                    .filter_map(|cell| map.cell(cell))
                    .collect(),
                reads: heap
                    .reads
                    .iter()
                    .filter_map(|cell| map.cell(cell))
                    .collect(),
                writes: heap
                    .writes
                    .iter()
                    .filter_map(|cell| map.cell(cell))
                    .collect(),
                opaque: heap.opaque,
                observes_external_heap: heap.observes_external_heap,
            },
            heap_frontiers: frontiers
                .iter()
                .filter_map(|(source, flags)| {
                    let module = map.module(*source);
                    map.heap_complete &= module.is_some();
                    Some((module?, *flags))
                })
                .collect(),
            observed_heap_accesses: HeapContentAccesses {
                reads: observed
                    .reads
                    .iter()
                    .filter_map(|cell| map.cell(cell))
                    .collect(),
                writes: observed
                    .writes
                    .iter()
                    .filter_map(|cell| map.cell(cell))
                    .collect(),
            },
            executed_owners: owners
                .iter()
                .filter_map(|owner| map.owner(*owner))
                .collect(),
            available: false,
            binding_effects_available: false,
            heap_effects_available: false,
        };
        result.available = map.complete;
        result.binding_effects_available = map.binding_complete && map.binding_owner_complete;
        result.heap_effects_available = map.heap_complete;
        result.availability_effects_available =
            bindings.availability_complete && map.availability_complete;
        if !result.heap_effects_available {
            result.heap_contents.opaque = true;
            result.heap_contents.observes_external_heap = true;
        }
        result
    }
}

impl<B: Ord> ProgramExecutionEffects<B> {
    /// Older artifacts only carried the aggregate bit. Treat their complete
    /// summaries as complete for each lane while keeping new partial summaries
    /// independently conservative.
    pub(crate) fn binding_lane_available(&self) -> bool {
        self.binding_effects_available || self.available
    }

    pub(crate) fn heap_lane_available(&self) -> bool {
        self.heap_effects_available || self.available
    }

    /// Translate only source binding contexts. Canonical modules, source
    /// coordinates and all completeness/provenance flags are unchanged.
    pub(crate) fn try_map_bindings<C: Ord, E>(
        &self,
        mut map: impl FnMut(&B) -> Result<C, E>,
    ) -> Result<ProgramExecutionEffects<C>, E> {
        fn binding<B, C, E>(
            value: &ProgramBindingId<B>,
            map: &mut impl FnMut(&B) -> Result<C, E>,
        ) -> Result<ProgramBindingId<C>, E> {
            Ok(ProgramBindingId {
                module: value.module.clone(),
                binding: map(&value.binding)?,
            })
        }
        fn lane<B, C: Ord, E>(
            values: &BTreeMap<ProgramBindingId<B>, BTreeSet<ProgramSourceUnitId>>,
            map: &mut impl FnMut(&B) -> Result<C, E>,
        ) -> Result<BTreeMap<ProgramBindingId<C>, BTreeSet<ProgramSourceUnitId>>, E> {
            values
                .iter()
                .map(|(value, owners)| Ok((binding(value, map)?, owners.clone())))
                .collect()
        }
        fn tags<B, C: Ord, E>(
            values: &BTreeMap<ProgramBindingId<B>, AssumedEffectReason>,
            map: &mut impl FnMut(&B) -> Result<C, E>,
        ) -> Result<BTreeMap<ProgramBindingId<C>, AssumedEffectReason>, E> {
            values
                .iter()
                .map(|(value, reason)| Ok((binding(value, map)?, reason.clone())))
                .collect()
        }
        fn cells<B, C: Ord, E>(
            values: &BTreeSet<EffectCell<ProgramAllocationId, ProgramBindingId<B>>>,
            map: &mut impl FnMut(&B) -> Result<C, E>,
        ) -> Result<BTreeSet<EffectCell<ProgramAllocationId, ProgramBindingId<C>>>, E> {
            values
                .iter()
                .map(|cell| {
                    Ok(match cell {
                        EffectCell::Binding(value) => EffectCell::Binding(binding(value, map)?),
                        EffectCell::GlobalProp(property) => {
                            EffectCell::GlobalProp(property.clone())
                        }
                        EffectCell::ArrayContents(origin) => {
                            EffectCell::ArrayContents(origin.clone())
                        }
                        EffectCell::SetContents(origin) => EffectCell::SetContents(origin.clone()),
                        EffectCell::ObjectShape(origin) => EffectCell::ObjectShape(origin.clone()),
                        EffectCell::OwnProperty { origin, property } => EffectCell::OwnProperty {
                            origin: origin.clone(),
                            property: property.clone(),
                        },
                    })
                })
                .collect()
        }
        Ok(ProgramExecutionEffects {
            default_exports: self.default_exports.clone(),
            availability_reads: self
                .availability_reads
                .iter()
                .map(|(cell, observers)| {
                    let cell = match cell {
                        ProgramAvailabilityCell::Binding(value) => {
                            ProgramAvailabilityCell::Binding(binding(value, &mut map)?)
                        }
                        ProgramAvailabilityCell::Default(value) => {
                            ProgramAvailabilityCell::Default(value.clone())
                        }
                    };
                    Ok((cell, observers.clone()))
                })
                .collect::<Result<_, E>>()?,
            availability_effects_available: self.availability_effects_available,
            available: self.available,
            binding_effects_available: self.binding_effects_available,
            heap_effects_available: self.heap_effects_available,
            reads: lane(&self.reads, &mut map)?,
            rebinds: lane(&self.rebinds, &mut map)?,
            uninitialized_reads: self
                .uninitialized_reads
                .iter()
                .map(|value| binding(value, &mut map))
                .collect::<Result<_, _>>()?,
            fallback_only: FallbackOnlyEffects {
                reads: tags(&self.fallback_only.reads, &mut map)?,
                rebinds: tags(&self.fallback_only.rebinds, &mut map)?,
                uninitialized_reads: tags(&self.fallback_only.uninitialized_reads, &mut map)?,
            },
            heap_contents: HeapContentCells {
                allocations: cells(&self.heap_contents.allocations, &mut map)?,
                reads: cells(&self.heap_contents.reads, &mut map)?,
                writes: cells(&self.heap_contents.writes, &mut map)?,
                opaque: self.heap_contents.opaque,
                observes_external_heap: self.heap_contents.observes_external_heap,
            },
            heap_frontiers: self.heap_frontiers.clone(),
            observed_heap_accesses: HeapContentAccesses {
                reads: cells(&self.observed_heap_accesses.reads, &mut map)?,
                writes: cells(&self.observed_heap_accesses.writes, &mut map)?,
            },
            executed_owners: self.executed_owners.clone(),
        })
    }
}

#[cfg(test)]
mod availability_tests {
    use super::{
        AvailabilityState, ProgramAvailabilityCell, ProgramExecutionEffects, ProgramSourceUnitId,
    };
    use source_identity::SourceUnitOrdinal;
    use std::collections::{BTreeMap, BTreeSet};

    #[test]
    fn availability_trace_round_trips_and_old_artifacts_remain_incomplete() {
        let cell = ProgramAvailabilityCell::Default(ProgramSourceUnitId {
            module: "provider".to_owned(),
            ordinal: SourceUnitOrdinal(0),
        });
        let observer = ProgramSourceUnitId {
            module: "importer".to_owned(),
            ordinal: SourceUnitOrdinal(1),
        };
        let effects = ProgramExecutionEffects::<swc_ecma_ast::Id> {
            availability_effects_available: true,
            availability_reads: BTreeMap::from([(
                cell.clone(),
                BTreeMap::from([(
                    observer.clone(),
                    BTreeSet::from([AvailabilityState::InitializedUndefined]),
                )]),
            )]),
            ..Default::default()
        };

        let encoded = analysis_codec::encode(&effects).unwrap();
        let restored: ProgramExecutionEffects = analysis_codec::decode(&encoded).unwrap();
        assert!(restored.availability_effects_available);
        assert_eq!(restored.availability_reads, effects.availability_reads);

        let mut legacy =
            serde_json::to_value(ProgramExecutionEffects::<swc_ecma_ast::Id>::default()).unwrap();
        let object = legacy.as_object_mut().unwrap();
        object.remove("availability_reads");
        object.remove("availability_effects_available");
        let restored: ProgramExecutionEffects = serde_json::from_value(legacy).unwrap();
        assert!(!restored.availability_effects_available);
        assert!(restored.availability_reads.is_empty());
    }
}
