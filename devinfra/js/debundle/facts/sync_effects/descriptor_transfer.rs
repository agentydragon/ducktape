//! Shared descriptor snapshots and applications over one prefix heap.

use super::allocation_transfer::AllocationJudgment;
use super::effect_coverage::EffectCoverageFragment;
use super::native_operation::{CheckedNativePlan, NativeOperationCommit, NativeOperationResult};
use super::object_surface::{
    CurrentOwnPropertyDescriptor, CurrentOwnPropertyProjection, ObjectSurfaceStore,
    OwnPropertyLayout, OwnPropertyLayoutDescriptor, OwnPropertyLayoutKind,
};
use super::own_data_transfer::OwnDataContext;
use super::property_inference::{
    CapturedPropertyDescriptor, DescriptorAttributes, DescriptorSource,
    OwnPropertyDescriptorProjection, OwnPropertyProjection, OwnPropertyProof,
};
use super::value_model::{
    AbstractValue, CallSite, ProviderInput, Values, ordered_own_property_names,
};
use crate::analysis_inputs::{ParameterHeapRead, ProofDependencies};
use crate::facts::abstract_domain::builtins::{IntrinsicOp, Invocation, Row};
use crate::facts::abstract_domain::value::{HeapAddress, Kind, SourceHeapResolver, Value};
use crate::facts::{EffectCell, HeapContentCells};
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnDescriptorMapState {
    /// AST source site for diagnostics/protocol correspondence only. The
    /// qualified heap address is the map's semantic identity.
    pub(super) source_site: CallSite,
    pub(super) source_object: DescriptorSource,
    pub(super) valid: bool,
    pub(super) property_order: Vec<String>,
    pub(super) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(super) attributes: BTreeMap<String, DescriptorAttributes>,
    pub(super) retained_values: Values,
}

pub(super) use super::object_surface::AppliedAccessor;

pub(super) trait DescriptorContext: OwnDataContext {
    fn descriptor_map(&self, address: HeapAddress) -> Option<&OwnDescriptorMapState>;
    fn has_applied_accessors(&self, object: HeapAddress) -> bool;
    fn target_is_extensible(&self, object: HeapAddress) -> bool;
    fn heap_epoch(&self) -> u64;
}

pub(super) struct DescriptorApplication {
    pub(super) target: HeapAddress,
    pub(super) map_address: HeapAddress,
    /// Every descriptor is copied into the plan so commit cannot fail after
    /// invalidating any prefix facts.
    pub(super) definitions: Vec<(String, CapturedPropertyDescriptor)>,
    pub(super) attributes: BTreeMap<String, DescriptorAttributes>,
    pub(super) result: NativeObjectPostcondition,
}

#[derive(Debug, Clone)]
struct NativeProperty {
    descriptor: CapturedPropertyDescriptor,
    attributes: DescriptorAttributes,
}

/// A postcondition of one normal native return, not an exact shape for every
/// runtime object represented by its pooled allocation origin.
#[derive(Debug, Clone)]
pub(super) struct NativeObjectPostcondition {
    target: HeapAddress,
    epoch: u64,
    committed: bool,
    order: Vec<String>,
    properties: BTreeMap<String, NativeProperty>,
    dependencies: Box<ProofDependencies>,
}

impl NativeObjectPostcondition {
    /// A selected normal result can describe this generation even when a
    /// pooled surface lost all-instance authority during the native write.
    /// Reading this product never installs or revives a pooled certificate.
    pub(super) fn current_projection(
        &self,
        context: &impl DescriptorContext,
        selected: &Values,
    ) -> Option<CurrentOwnPropertyProjection> {
        if !self.committed
            || self.epoch != context.heap_epoch()
            || !self.matches(context.heap_resolver(), selected)
        {
            return None;
        }
        let layout = OwnPropertyLayout::from_creation_order(
            &self.order,
            self.properties
                .iter()
                .map(|(name, property)| {
                    (
                        name.clone(),
                        OwnPropertyLayoutDescriptor {
                            kind: if matches!(
                                property.descriptor,
                                CapturedPropertyDescriptor::Data(_)
                            ) {
                                OwnPropertyLayoutKind::Data
                            } else {
                                OwnPropertyLayoutKind::Accessor
                            },
                            attributes: property.attributes,
                        },
                    )
                })
                .collect(),
        )?;
        let surface = context.object_surface();
        let data = surface.prefix_object_properties.get(&self.target)?;
        if data.get(&None).is_some_and(|values| !values.is_empty()) {
            return None;
        }
        let accessors = surface.applied_accessors.get(&self.target);
        let mut descriptors = BTreeMap::new();
        for (name, property) in &self.properties {
            let descriptor = match &property.descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    let retained = data.get(&Some(name.clone()))?;
                    if values.is_empty() || !values.is_subset(retained) {
                        return None;
                    }
                    CurrentOwnPropertyDescriptor::Data {
                        values: values.clone(),
                        attributes: property.attributes,
                    }
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    let retained = accessors?.get(name)?;
                    if !getter.is_subset(&retained.getter) || !setter.is_subset(&retained.setter) {
                        return None;
                    }
                    CurrentOwnPropertyDescriptor::Accessor {
                        getter: getter.clone(),
                        setter: setter.clone(),
                        attributes: property.attributes,
                    }
                }
            };
            descriptors.insert(name.clone(), descriptor);
        }
        let mut dependencies = self.dependencies.as_ref().clone();
        dependencies.extend(&surface.own_property_dependencies(self.target));
        Some(CurrentOwnPropertyProjection {
            layout,
            descriptors,
            dependencies,
        })
    }

    pub(super) fn retained_values(&self) -> Values {
        self.properties
            .values()
            .flat_map(|property| match &property.descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    values.iter().cloned().collect::<Values>()
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    getter.iter().chain(setter).cloned().collect()
                }
            })
            .collect()
    }

    pub(super) fn matches(&self, resolver: SourceHeapResolver, values: &Values) -> bool {
        object(resolver, values) == Some(self.target)
    }

    pub(super) fn after_mutation(mut self: Box<Self>, epoch: u64) -> Box<Self> {
        self.epoch = epoch;
        self.committed = true;
        self
    }

    pub(super) fn extend_dependencies(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
    }
}

fn target_shape(
    context: &impl DescriptorContext,
    values: &Values,
    postcondition: Option<&NativeObjectPostcondition>,
) -> Option<NativeObjectPostcondition> {
    let resolver = context.heap_resolver();
    let target = object(resolver, values)?;
    if !context.object_properties_are_settled(target) {
        return None;
    }
    if let Some(proof) = postcondition {
        return (proof.committed
            && proof.matches(resolver, values)
            && proof.epoch == context.heap_epoch())
        .then(|| proof.clone());
    }
    if !context.target_is_extensible(target) {
        return None;
    }
    let projection = context
        .object_surface()
        .current_own_property_projection(target)?;
    let properties = projection
        .descriptors
        .into_iter()
        .map(|(name, descriptor)| {
            let CurrentOwnPropertyDescriptor::Data { values, attributes } = descriptor else {
                return None;
            };
            Some((
                name,
                NativeProperty {
                    descriptor: CapturedPropertyDescriptor::Data(values),
                    attributes,
                },
            ))
        })
        .collect::<Option<BTreeMap<_, _>>>()?;
    Some(NativeObjectPostcondition {
        target,
        epoch: context.heap_epoch(),
        committed: false,
        order: projection.layout.ordered_keys,
        properties,
        dependencies: Box::new(projection.dependencies),
    })
}

trait ResolveObjectIdentity: Copy {
    fn in_scope_object(self, resolver: SourceHeapResolver) -> Option<HeapAddress>;
}

impl ResolveObjectIdentity for u32 {
    fn in_scope_object(self, resolver: SourceHeapResolver) -> Option<HeapAddress> {
        Some(resolver.allocation(Kind::Object, self))
    }
}

impl ResolveObjectIdentity for HeapAddress {
    fn in_scope_object(self, resolver: SourceHeapResolver) -> Option<HeapAddress> {
        (self.kind() == Some(Kind::Object) && self.module() == Some(resolver.scope()))
            .then_some(self)
    }
}

fn object(resolver: SourceHeapResolver, values: &Values) -> Option<HeapAddress> {
    if values.len() != 1 {
        return None;
    }
    match values.first()? {
        AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
            object.in_scope_object(resolver)
        }
        _ => None,
    }
}

/// Configurable existing properties and extensibility discharge the normal
/// completion obligations independently of the descriptor values being copied.
pub(super) fn application(
    context: &impl DescriptorContext,
    target: &Values,
    descriptors: &Values,
    postcondition: Option<&NativeObjectPostcondition>,
) -> Option<DescriptorApplication> {
    let mut result = target_shape(context, target, postcondition)?;
    result.committed = false;
    if descriptors.len() != 1 {
        return None;
    }
    let AbstractValue::OwnDescriptorMap(map_address) = descriptors.first()? else {
        return None;
    };
    let map = context
        .descriptor_map(*map_address)
        .filter(|map| map.valid)?;
    let mut definitions = Vec::with_capacity(map.property_order.len());
    for name in &map.property_order {
        if let Some(existing) = result.properties.get(name) {
            if existing.attributes.configurable != Some(true) {
                return None;
            }
        } else {
            result.order.push(name.clone());
        }
        let descriptor = map.properties.get(name)?.clone();
        result.properties.insert(
            name.clone(),
            NativeProperty {
                descriptor: descriptor.clone(),
                attributes: *map.attributes.get(name)?,
            },
        );
        definitions.push((name.clone(), descriptor));
    }
    Some(DescriptorApplication {
        target: result.target,
        map_address: *map_address,
        definitions,
        attributes: map.attributes.clone(),
        result,
    })
}

pub(super) struct PropertyAssignment {
    pub(super) target: HeapAddress,
    pub(super) source: HeapAddress,
    pub(super) values: BTreeMap<String, Values>,
    pub(super) result: NativeObjectPostcondition,
}

pub(super) fn assignment(
    context: &impl DescriptorContext,
    target: &Values,
    source: &Values,
    postcondition: Option<&NativeObjectPostcondition>,
) -> Option<PropertyAssignment> {
    let mut result = target_shape(context, target, postcondition)?;
    result.committed = false;
    let source_shape = target_shape(context, source, None)?;
    result.dependencies.extend(&source_shape.dependencies);
    let source_properties = context.own_properties(source_shape.target)?;
    let mut values = BTreeMap::new();
    for name in &ordered_own_property_names(&source_shape.order) {
        let descriptor = source_shape.properties.get(name)?;
        if !matches!(descriptor.descriptor, CapturedPropertyDescriptor::Data(_))
            || descriptor.attributes.enumerable != Some(true)
        {
            return None;
        }
        if let Some(existing) = result.properties.get(name) {
            if !matches!(existing.descriptor, CapturedPropertyDescriptor::Data(_))
                || existing.attributes.writable != Some(true)
            {
                return None;
            }
        } else {
            // The legacy prototype setter changes [[Prototype]] rather than
            // creating an own data property.
            if name == "__proto__" {
                return None;
            }
            result.order.push(name.clone());
            result.properties.insert(
                name.clone(),
                NativeProperty {
                    descriptor: CapturedPropertyDescriptor::Data(Values::new()),
                    attributes: DescriptorAttributes::literal(true),
                },
            );
        }
        let contents = source_properties.get(&Some(name.clone()))?;
        if contents.is_empty() {
            return None;
        }
        values.insert(name.clone(), contents.clone());
        result.properties.get_mut(name)?.descriptor =
            CapturedPropertyDescriptor::Data(contents.clone());
    }
    Some(PropertyAssignment {
        target: result.target,
        source: source_shape.target,
        values,
        result,
    })
}

pub(super) fn supports_native_call(row: &Row) -> bool {
    row.invocation == [Invocation::Call]
        && row.invokes.is_empty()
        && matches!(
            row.op,
            IntrinsicOp::ObjectGetOwnPropertyDescriptors
                | IntrinsicOp::ObjectGetOwnPropertyNames
                | IntrinsicOp::ObjectDefineProperties
                | IntrinsicOp::ObjectAssign
        )
}

fn source(resolver: SourceHeapResolver, values: &Values) -> Option<DescriptorSource> {
    if values.len() != 1 {
        return None;
    }
    match values.first()? {
        AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
            Some(DescriptorSource::Object(object.in_scope_object(resolver)?))
        }
        AbstractValue::SymbolicInput(input)
            if matches!(input.input, ProviderInput::Argument(_)) =>
        {
            Some(DescriptorSource::Input(*input))
        }
        _ => None,
    }
}

fn record_inspection_read(
    origin: &DescriptorSource,
    property: Option<&str>,
    heap: &mut HeapContentCells,
    parameters: &mut BTreeSet<ParameterHeapRead>,
) -> Option<()> {
    match (origin, property) {
        (DescriptorSource::Object(origin), None) => {
            heap.reads.insert(EffectCell::ObjectShape(*origin));
        }
        (DescriptorSource::Object(origin), Some(property)) => {
            heap.reads.insert(EffectCell::OwnProperty {
                origin: *origin,
                property: property.to_owned(),
            });
        }
        (DescriptorSource::Input(input), property) => {
            let ProviderInput::Argument(argument) = input.input else {
                return None;
            };
            parameters.insert(match property {
                None => ParameterHeapRead::ObjectShape { argument },
                Some(property) => ParameterHeapRead::OwnProperty {
                    argument,
                    property: property.to_owned(),
                },
            });
        }
    }
    Some(())
}

pub(super) struct DescriptorCall<'a> {
    pub(super) row: &'a Row,
    pub(super) site: CallSite,
    pub(super) arguments: &'a [Values],
    pub(super) projection: Option<&'a OwnPropertyProof>,
    pub(super) target_postcondition: Option<&'a NativeObjectPostcondition>,
}

/// Body effects after independent callee/protocol-integrity admission. Exact
/// prefix projections discharge inspection's proxy/internal-method obligations;
/// application additionally discharges descriptor conversion and target throws.
/// No operand expression is evaluated here.
pub(super) fn native_call_plan(
    context: &impl DescriptorContext,
    call: DescriptorCall<'_>,
    dependencies: &ProofDependencies,
) -> Option<CheckedNativePlan> {
    let DescriptorCall {
        row,
        site,
        arguments,
        projection,
        target_postcondition,
    } = call;
    if !supports_native_call(row) || !row.arity.contains(&arguments.len()) {
        return None;
    }
    let mut heap = HeapContentCells::default();
    let mut parameter_reads = BTreeSet::new();
    let mut coverage = EffectCoverageFragment::no_effects();
    coverage.depend_on(dependencies);
    let mut operation_dependencies = dependencies.clone();
    let result;
    let commit;
    if let Some(proof) = target_postcondition {
        coverage.depend_on(&proof.dependencies);
        operation_dependencies.extend(&proof.dependencies);
    }
    match row.op {
        IntrinsicOp::ObjectGetOwnPropertyDescriptors => {
            let proof = projection?;
            let OwnPropertyProjection::Descriptors(descriptors) = &proof.projection else {
                return None;
            };
            if source(context.heap_resolver(), arguments.first()?)? != descriptors.source_object {
                return None;
            }
            coverage.depend_on(&proof.dependencies);
            operation_dependencies.extend(&proof.dependencies);
            record_inspection_read(
                &descriptors.source_object,
                None,
                &mut heap,
                &mut parameter_reads,
            )?;
            for (property, descriptor) in &descriptors.properties {
                if matches!(descriptor, CapturedPropertyDescriptor::Data(_)) {
                    record_inspection_read(
                        &descriptors.source_object,
                        Some(property),
                        &mut heap,
                        &mut parameter_reads,
                    )?;
                }
            }
            let map_address = context
                .heap_resolver()
                .allocation(Kind::Object, site.allocation_id());
            heap.allocations
                .insert(EffectCell::DescriptorMapContents(map_address));
            commit = NativeOperationCommit::CaptureDescriptors {
                site,
                projection: descriptors.clone(),
            };
            result = Some(NativeOperationResult {
                values: Values::from([AbstractValue::OwnDescriptorMap(map_address)]),
                postcondition: None,
            });
        }
        IntrinsicOp::ObjectGetOwnPropertyNames => {
            let proof = projection?;
            let OwnPropertyProjection::Names(names) = &proof.projection else {
                return None;
            };
            coverage.depend_on(&proof.dependencies);
            operation_dependencies.extend(&proof.dependencies);
            record_inspection_read(
                &source(context.heap_resolver(), arguments.first()?)?,
                None,
                &mut heap,
                &mut parameter_reads,
            )?;
            let allocation = context
                .heap_resolver()
                .allocation(Kind::Object, site.allocation_id());
            heap.allocations
                .insert(EffectCell::ArrayContents(allocation));
            commit = NativeOperationCommit::CreateNamesArray {
                allocation,
                names: names.names.clone(),
            };
            result = Some(NativeOperationResult {
                values: Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Object,
                    allocation,
                ))]),
                postcondition: None,
            });
        }
        IntrinsicOp::ObjectDefineProperties => {
            let [target, descriptors] = arguments else {
                return None;
            };
            let application = application(context, target, descriptors, target_postcondition)?;
            operation_dependencies.extend(&application.result.dependencies);
            let map = context.descriptor_map(application.map_address)?;
            heap.reads
                .insert(EffectCell::DescriptorMapContents(application.map_address));
            heap.reads
                .insert(EffectCell::ObjectShape(application.target));
            heap.writes
                .insert(EffectCell::ObjectShape(application.target));
            for property in &map.property_order {
                heap.writes.insert(EffectCell::OwnProperty {
                    origin: application.target,
                    property: property.clone(),
                });
            }
            result = Some(NativeOperationResult {
                values: target.clone(),
                postcondition: Some(Box::new(application.result.clone())),
            });
            commit = NativeOperationCommit::DefineProperties(application);
        }
        IntrinsicOp::ObjectAssign => {
            let [target, source] = arguments else {
                return None;
            };
            let assignment = assignment(context, target, source, target_postcondition)?;
            operation_dependencies.extend(&assignment.result.dependencies);
            heap.reads
                .insert(EffectCell::ObjectShape(assignment.source));
            heap.reads
                .insert(EffectCell::ObjectShape(assignment.target));
            heap.writes
                .insert(EffectCell::ObjectShape(assignment.target));
            for property in assignment.values.keys() {
                heap.reads.insert(EffectCell::OwnProperty {
                    origin: assignment.source,
                    property: property.clone(),
                });
                heap.writes.insert(EffectCell::OwnProperty {
                    origin: assignment.target,
                    property: property.clone(),
                });
            }
            result = Some(NativeOperationResult {
                values: target.clone(),
                postcondition: Some(Box::new(assignment.result.clone())),
            });
            commit = NativeOperationCommit::AssignProperties(assignment);
        }
        _ => return None,
    }
    let allocation = if parameter_reads.is_empty() {
        AllocationJudgment::CoveredAllocationOnly(Box::new(operation_dependencies.clone()))
    } else {
        AllocationJudgment::Opaque
    };
    Some(CheckedNativePlan {
        expected_revision: context.heap_epoch(),
        commit,
        result: result?,
        effects: coverage,
        dependencies: Box::new(operation_dependencies),
        heap,
        parameter_reads,
        allocation,
    })
}

/// A caller supplies its prefix store. Admission owns native identity,
/// ordinary target/carrier, scope and completion proofs;
/// these updates grant neither ordinary-property shape nor effect coverage.
pub(super) struct DescriptorHeap<'a> {
    pub(super) resolver: SourceHeapResolver,
    pub(super) maps: &'a mut OrdMap<HeapAddress, OwnDescriptorMapState>,
    pub(super) surface: &'a mut ObjectSurfaceStore,
    pub(super) revision: &'a mut u64,
}

impl DescriptorHeap<'_> {
    pub(super) fn capture(
        &mut self,
        site: CallSite,
        projection: OwnPropertyDescriptorProjection,
    ) -> AbstractValue {
        let address = self.resolver.allocation(Kind::Object, site.allocation_id());
        let OwnPropertyDescriptorProjection {
            source_object,
            ordered_names: property_order,
            properties,
            attributes,
            retained_values,
        } = projection;
        let changed = match self.maps.get_mut(&address) {
            Some(existing) => {
                if existing.source_site != site
                    || existing.source_object != source_object
                    || existing.property_order != property_order
                    || existing.properties != properties
                    || existing.attributes != attributes
                {
                    let was_valid = existing.valid;
                    let retained_count = existing.retained_values.len();
                    existing.valid = false;
                    existing.retained_values.extend(retained_values);
                    was_valid || retained_count != existing.retained_values.len()
                } else {
                    false
                }
            }
            None => {
                self.maps.insert(
                    address,
                    OwnDescriptorMapState {
                        source_site: site,
                        source_object,
                        valid: true,
                        property_order,
                        properties,
                        attributes,
                        retained_values,
                    },
                );
                true
            }
        };
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
        AbstractValue::OwnDescriptorMap(address)
    }

    pub(super) fn invalidate(&mut self, address: HeapAddress) {
        let candidates = self
            .maps
            .keys()
            .copied()
            .filter(|candidate| address.may_overlap_with_provider_family(*candidate))
            .collect::<Vec<_>>();
        let mut changed = false;
        for candidate in candidates {
            if let Some(map) = self.maps.get_mut(&candidate)
                && map.valid
            {
                map.valid = false;
                changed = true;
            }
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// A write through one member of a provider/source alias pair may mutate
    /// a descriptor carrier returned through the other. Ordinary source
    /// writes still leave independent captured snapshots untouched.
    pub(super) fn invalidate_provider_aliases(&mut self, address: HeapAddress) {
        let candidates = self.maps.keys().copied().collect::<Vec<_>>();
        let mut changed = false;
        for candidate in candidates {
            let provider_alias = matches!(
                (address, candidate),
                (HeapAddress::ProviderAllocationFamily { .. }, _)
                    | (_, HeapAddress::ProviderAllocationFamily { .. })
            );
            if provider_alias
                && address.may_overlap_with_provider_family(candidate)
                && let Some(map) = self.maps.get_mut(&candidate)
                && map.valid
            {
                map.valid = false;
                changed = true;
            }
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Apply only a still-valid captured map. The caller separately proves
    /// that defining these properties on its target completes normally.
    pub(super) fn apply(&mut self, target: HeapAddress, address: HeapAddress) -> bool {
        let Some(map) = self.maps.get(&address).filter(|map| map.valid).cloned() else {
            return false;
        };
        let Some(definitions) = Self::definitions(&map) else {
            return false;
        };
        self.apply_definitions(target, &definitions, &map.attributes);
        true
    }

    /// Commit a prevalidated map snapshot. No lookup or refusal remains after
    /// the caller starts mutating prefix facts.
    pub(super) fn apply_application(&mut self, application: &DescriptorApplication) {
        self.apply_definitions(
            application.target,
            &application.definitions,
            &application.attributes,
        );
    }

    fn definitions(
        map: &OwnDescriptorMapState,
    ) -> Option<Vec<(String, CapturedPropertyDescriptor)>> {
        map.property_order
            .iter()
            .map(|name| Some((name.clone(), map.properties.get(name)?.clone())))
            .collect()
    }

    fn apply_definitions(
        &mut self,
        target: HeapAddress,
        definitions: &[(String, CapturedPropertyDescriptor)],
        attributes: &BTreeMap<String, DescriptorAttributes>,
    ) {
        for (name, descriptor) in definitions {
            let attributes = attributes.get(name).copied().unwrap_or_default();
            match descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    self.surface
                        .define_data_property(target, name.clone(), values, attributes);
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    self.surface.define_accessor_property(
                        target,
                        name.clone(),
                        getter,
                        setter,
                        attributes,
                    );
                }
            }
        }
        // Attribute-only definitions invalidate short-lived postconditions too.
        *self.revision = self.revision.wrapping_add(1);
    }

    pub(super) fn assign(&mut self, assignment: &PropertyAssignment) {
        for (name, values) in &assignment.values {
            let attributes = assignment
                .result
                .properties
                .get(name)
                .map(|property| property.attributes)
                .unwrap_or_default();
            self.surface
                .define_data_property(assignment.target, name.clone(), values, attributes);
        }
        *self.revision = self.revision.wrapping_add(1);
    }
}

#[cfg(test)]
mod provider_alias_tests {
    use super::*;
    use crate::facts::abstract_domain::value::{HeapModuleScope, ProviderCallableId};

    fn descriptor_state(source_object: HeapAddress, valid: bool) -> OwnDescriptorMapState {
        OwnDescriptorMapState {
            source_site: CallSite { start: 1, end: 2 },
            source_object: DescriptorSource::Object(source_object),
            valid,
            property_order: Vec::new(),
            properties: BTreeMap::new(),
            attributes: BTreeMap::new(),
            retained_values: Values::new(),
        }
    }

    #[test]
    fn source_writes_invalidate_provider_descriptor_aliases_but_keep_snapshots() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let source = HeapAddress::module_family(source_identity::ChunkId(4), Kind::Object, 27);
        let recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(source_identity::ChunkId(9)),
            call_origin: 31,
            producer: ProviderCallableId {
                module: producer,
                origin: 42,
            },
            producer_birth: None,
            slot: 0,
            kind: Kind::Object,
        };
        let unrelated = HeapAddress::module_family(source_identity::ChunkId(5), Kind::Object, 27);
        let mut maps = OrdMap::new();
        maps.insert(source, descriptor_state(source, true));
        maps.insert(recipe, descriptor_state(source, true));
        maps.insert(unrelated, descriptor_state(unrelated, true));
        let mut surface = ObjectSurfaceStore::default();
        let mut revision = 0;
        let mut heap = DescriptorHeap {
            resolver: SourceHeapResolver::new(producer),
            maps: &mut maps,
            surface: &mut surface,
            revision: &mut revision,
        };

        heap.invalidate_provider_aliases(source);

        assert!(
            heap.maps[&source].valid,
            "source snapshots remain unchanged"
        );
        assert!(!heap.maps[&recipe].valid);
        assert!(heap.maps[&unrelated].valid);
        assert_eq!(*heap.revision, 1);
        let previous_revision = *heap.revision;
        heap.invalidate_provider_aliases(recipe);
        assert!(
            !heap.maps[&source].valid,
            "provider writes also invalidate source aliases"
        );
        assert_eq!(*heap.revision, previous_revision + 1);
    }

    #[test]
    fn unverified_provider_write_invalidates_all_verified_descriptor_aliases() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let source = HeapAddress::module_family(source_identity::ChunkId(4), Kind::Object, 27);
        let recipe = |consumer, call_origin, callable_origin, slot, birth| {
            HeapAddress::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(consumer),
                call_origin,
                producer: ProviderCallableId {
                    module: producer,
                    origin: callable_origin,
                },
                producer_birth: birth,
                slot,
                kind: Kind::Object,
            }
        };
        let unverified = recipe(source_identity::ChunkId(9), 31, 42, 0, None);
        let first_birth = recipe(source_identity::ChunkId(12), 81, 20, 3, Some(27));
        let second_birth = recipe(source_identity::ChunkId(13), 82, 21, 4, Some(28));
        let unrelated = HeapAddress::module_family(source_identity::ChunkId(5), Kind::Object, 27);
        let mut maps = OrdMap::new();
        for address in [source, first_birth, second_birth, unrelated] {
            maps.insert(address, descriptor_state(address, true));
        }
        let mut surface = ObjectSurfaceStore::default();
        let mut revision = 0;
        let mut heap = DescriptorHeap {
            resolver: SourceHeapResolver::new(producer),
            maps: &mut maps,
            surface: &mut surface,
            revision: &mut revision,
        };

        heap.invalidate_provider_aliases(unverified);

        assert!(!heap.maps[&source].valid);
        assert!(!heap.maps[&first_birth].valid);
        assert!(!heap.maps[&second_birth].valid);
        assert!(heap.maps[&unrelated].valid);
    }
}
