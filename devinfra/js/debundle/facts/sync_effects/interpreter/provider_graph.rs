//! Instantiate one source-verified provider result into the current leaf world.

use super::super::strict_provider::{VerifiedProviderGraph, VerifiedProviderSlot};
use super::*;
use crate::analysis_inputs::{
    ProviderGraphInput, ProviderGraphNodeKind, ProviderGraphRole, ProviderGraphValue,
};
use crate::facts::abstract_domain::value::ProviderCallableId;
use crate::facts::sync_effects::object_surface::{AppliedAccessor, OwnPropertyLayoutKind};

pub(super) struct PreparedProviderGraph {
    pub(super) provider: crate::analysis_inputs::ConditionalProviderIdentity,
    pub(super) graph: crate::analysis_inputs::ProviderRetainedGraph,
    pub(super) inputs: BTreeMap<ProviderGraphInput, Values>,
    pub(super) verified: Box<VerifiedProviderGraph>,
}

fn substitute(
    value: &ProviderGraphValue,
    inputs: &BTreeMap<ProviderGraphInput, Values>,
    slots: &[Values],
) -> Values {
    match value {
        ProviderGraphValue::Input { input } => inputs[input].clone(),
        ProviderGraphValue::Primitive { kinds } => {
            Values::from([AbstractValue::Value(Value::Prim(*kinds))])
        }
        ProviderGraphValue::Slot { slot } => slots[*slot].clone(),
    }
}

impl Executor<'_, '_> {
    /// Preflight the complete graph before allocating a cell or writing a
    /// property. Unsupported graph domains retain the opaque carrier.
    pub(super) fn instantiate_verified_provider_graph(
        &mut self,
        call: &Call,
        verified: &VerifiedProviderGraph,
        inputs: &BTreeMap<ProviderGraphInput, Values>,
        outcome: &mut Outcome,
    ) -> Option<Values> {
        let graph = &verified.graph;
        if verified.slots.len() != graph.nodes.len() {
            return None;
        }
        let producer = ProviderCallableId {
            module: verified.source.scope,
            origin: verified.callable_origin,
        };
        let mut addresses = vec![None; graph.nodes.len()];
        for (slot, (node, recipe)) in graph.nodes.iter().zip(&verified.slots).enumerate() {
            let kind = match (recipe, &node.kind) {
                (
                    VerifiedProviderSlot::Heap {
                        kind: Kind::Object,
                        properties: Some(_),
                        ..
                    },
                    ProviderGraphNodeKind::Record,
                ) => Kind::Object,
                (
                    VerifiedProviderSlot::Closure {
                        source_origin,
                        bindings,
                    },
                    ProviderGraphNodeKind::Closure { .. },
                ) => {
                    self.inputs.provider_programs.callable_program(
                        verified.source.scope,
                        &verified.source.sha256,
                        *source_origin,
                    )?;
                    if bindings.values().any(|slot| {
                        !matches!(
                            verified.slots.get(*slot),
                            Some(VerifiedProviderSlot::LocalCell { .. })
                        )
                    }) {
                        return None;
                    }
                    Kind::Function
                }
                (VerifiedProviderSlot::LocalCell { .. }, ProviderGraphNodeKind::LexicalCell) => {
                    continue;
                }
                _ => return None,
            };
            let source_origin = match recipe {
                VerifiedProviderSlot::Heap { source_origin, .. }
                | VerifiedProviderSlot::Closure { source_origin, .. } => *source_origin,
                VerifiedProviderSlot::LocalCell { .. } | VerifiedProviderSlot::ModuleCell => {
                    unreachable!("cell recipes have no heap address")
                }
            };
            let birth = verified.source.current_position(source_origin)?;
            addresses[slot] = Some(
                self.heap_resolver()
                    .provider_allocation(call.span.lo.0, producer, u32::try_from(slot).ok()?, kind)
                    .with_verified_producer_birth(birth),
            );
        }
        // Different formal slots are not independently proved disjoint by
        // their labels. Require the central address law to agree before any
        // per-object descriptor authority can be installed.
        for (index, address) in addresses.iter().flatten().enumerate() {
            if addresses
                .iter()
                .flatten()
                .skip(index + 1)
                .any(|other| address.may_overlap_with_provider_family(*other))
            {
                return None;
            }
        }
        for (node, recipe) in graph.nodes.iter().zip(&verified.slots) {
            if matches!(recipe, VerifiedProviderSlot::Heap { .. })
                && node.edges.iter().any(|edge| {
                    !matches!(
                        edge.role,
                        ProviderGraphRole::OwnData { .. }
                            | ProviderGraphRole::Getter { .. }
                            | ProviderGraphRole::Setter { .. }
                    )
                })
            {
                return None;
            }
            if let VerifiedProviderSlot::Closure { bindings, .. } = recipe {
                for edge in &node.edges {
                    if let (ProviderGraphRole::Retained, ProviderGraphValue::Slot { slot }) =
                        (&edge.role, &edge.value)
                        && matches!(verified.slots[*slot], VerifiedProviderSlot::Closure { .. })
                        && !bindings.values().any(|cell| {
                            graph.nodes[*cell]
                                .edges
                                .iter()
                                .any(|stored| stored.value == edge.value)
                        })
                    {
                        return None;
                    }
                }
            }
            if let VerifiedProviderSlot::Heap {
                properties: Some(properties),
                ..
            } = recipe
            {
                let actual: BTreeSet<_> = node
                    .edges
                    .iter()
                    .filter_map(|edge| match &edge.role {
                        ProviderGraphRole::OwnData { property }
                        | ProviderGraphRole::Getter { property }
                        | ProviderGraphRole::Setter { property } => Some(property.clone()),
                        _ => None,
                    })
                    .collect();
                if !properties.layout.is_complete()
                    || actual.iter().ne(properties.layout.descriptors.keys())
                    || properties
                        .layout
                        .ordered_keys
                        .iter()
                        .collect::<BTreeSet<_>>()
                        != actual.iter().collect()
                    || node.edges.iter().any(|edge| match &edge.role {
                        ProviderGraphRole::OwnData { property } => {
                            properties.layout.descriptors[property].kind
                                != OwnPropertyLayoutKind::Data
                        }
                        ProviderGraphRole::Getter { property }
                        | ProviderGraphRole::Setter { property } => {
                            properties.layout.descriptors[property].kind
                                != OwnPropertyLayoutKind::Accessor
                        }
                        _ => true,
                    })
                {
                    return None;
                }
            }
        }

        // A new recipe label cannot revive a certificate retired through a
        // previously observed alias. Keep payloads, and retire all associated
        // object/native/descriptor authority through the central invalidator.
        for address in addresses.iter().flatten() {
            if self
                .heap_addresses_aliasing(*address)
                .iter()
                .any(|other| other != address)
            {
                self.invalidate_object_heap_proofs(*address);
            }
        }
        let mut heap = HeapContentCells::default();
        heap.allocations.extend(
            addresses
                .iter()
                .flatten()
                .copied()
                .map(EffectCell::ObjectShape),
        );
        let mut cells = BTreeMap::new();
        for (slot, recipe) in verified.slots.iter().enumerate() {
            if let VerifiedProviderSlot::LocalCell {
                kind,
                source_origin,
            } = recipe
            {
                let cell = self
                    .state
                    .world
                    .make_mut()
                    .lexical_cells
                    .insert(LexicalCell {
                        values: Values::new(),
                        selection: None,
                        kind: *kind,
                        initialization: CellInitialization::Initialized,
                    });
                let addressed = EffectCell::LexicalCell {
                    origin: *source_origin,
                    instance: cell,
                };
                heap.allocations.insert(addressed.clone());
                heap.writes.insert(addressed);
                cells.insert(slot, cell);
            }
        }
        let mut slots = vec![Values::new(); graph.nodes.len()];
        for (slot, recipe) in verified.slots.iter().enumerate() {
            if let VerifiedProviderSlot::Heap { kind, .. } = recipe {
                slots[slot].insert(AbstractValue::Value(Value::Alloc(
                    *kind,
                    addresses[slot].expect("heap slot was preflighted"),
                )));
            }
        }
        // Closures close over allocated cells, allowing cycles and aliases to
        // share one substitution without embedding recursively copied values.
        for (slot, recipe) in verified.slots.iter().enumerate() {
            if let VerifiedProviderSlot::Closure {
                source_origin,
                bindings,
            } = recipe
            {
                let captures = LexicalCaptures {
                    cells: bindings
                        .iter()
                        .map(|(binding, slot)| (binding.clone(), cells[slot]))
                        .collect(),
                    ..LexicalCaptures::default()
                };
                let value = self.construct_provider_closure(
                    addresses[slot].expect("heap slot was preflighted"),
                    InstantiatedProviderClosure {
                        module: verified.source.scope,
                        source_origin: *source_origin,
                        source_sha256: verified.source.sha256.clone(),
                        captures,
                        retained: graph.nodes[slot]
                            .edges
                            .iter()
                            .filter(|edge| edge.role == ProviderGraphRole::Retained)
                            .flat_map(|edge| substitute(&edge.value, inputs, &slots))
                            .collect(),
                    },
                );
                slots[slot].insert(value);
            }
        }
        for (slot, cell) in &cells {
            let values = graph.nodes[*slot]
                .edges
                .iter()
                .flat_map(|edge| substitute(&edge.value, inputs, &slots))
                .collect();
            let selection =
                CallableSelection::observed(&values, Box::new(verified.dependencies.clone()));
            let cell = &mut self.state.world.make_mut().lexical_cells[cell.0];
            cell.values = values;
            cell.selection = selection;
        }
        for (slot, recipe) in verified.slots.iter().enumerate() {
            let VerifiedProviderSlot::Heap {
                properties: Some(properties),
                ..
            } = recipe
            else {
                continue;
            };
            let address = addresses[slot].expect("heap slot was preflighted");
            heap.allocations.insert(EffectCell::ObjectShape(address));
            let surface = &mut self.state.world.make_mut().object_surface;
            surface.observe_own_property_layout(
                address,
                Some(properties.layout.clone()),
                verified.dependencies.clone(),
            );
            let mut data = BTreeMap::<String, Values>::new();
            let mut getters = BTreeMap::<String, Values>::new();
            let mut setters = BTreeMap::<String, Values>::new();
            for edge in &graph.nodes[slot].edges {
                let target = match &edge.role {
                    ProviderGraphRole::OwnData { property } => {
                        data.entry(property.clone()).or_default()
                    }
                    ProviderGraphRole::Getter { property } => {
                        getters.entry(property.clone()).or_default()
                    }
                    ProviderGraphRole::Setter { property } => {
                        setters.entry(property.clone()).or_default()
                    }
                    _ => unreachable!("verified record edges were preflighted"),
                };
                target.extend(substitute(&edge.value, inputs, &slots));
            }
            for property in &properties.layout.ordered_keys {
                heap.writes.insert(EffectCell::OwnProperty {
                    origin: address,
                    property: property.clone(),
                });
            }
            surface.record_data_properties(
                address,
                data.into_iter()
                    .map(|(name, values)| (Some(name), values))
                    .collect(),
                Some(&properties.layout),
            );
            let accessors = properties
                .layout
                .descriptors
                .iter()
                .filter(|(_, descriptor)| descriptor.kind == OwnPropertyLayoutKind::Accessor)
                .map(|(name, _)| {
                    (
                        name.clone(),
                        AppliedAccessor {
                            getter: getters.remove(name).unwrap_or_default(),
                            setter: setters.remove(name).unwrap_or_default(),
                        },
                    )
                })
                .collect();
            surface.record_accessor_properties(address, accessors, Some(&properties.layout));
        }
        outcome.ledger.effects.depend_on(&verified.dependencies);
        outcome
            .effects
            .record_heap_contents(&heap, self.state.heap_access_origin);
        self.state.world.bump_prefix_revision();
        Some(
            graph
                .roots
                .iter()
                .flat_map(|value| substitute(value, inputs, &slots))
                .collect(),
        )
    }
}
