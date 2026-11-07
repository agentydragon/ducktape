//! Shared descriptor snapshots and applications over one prefix heap.

use super::effect_coverage::EffectCoverageFragment;
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
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::{EffectCell, HeapContentCells};
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone)]
pub(super) struct OwnDescriptorMapState {
    pub(super) source_object: DescriptorSource,
    pub(super) valid: bool,
    pub(super) property_order: Vec<String>,
    pub(super) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(super) attributes: BTreeMap<String, DescriptorAttributes>,
    pub(super) retained_values: Values,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct AppliedAccessor {
    pub(super) getter: Values,
    pub(super) setter: Values,
}

pub(super) trait DescriptorContext: OwnDataContext {
    fn descriptor_map(&self, site: CallSite) -> Option<&OwnDescriptorMapState>;
    fn has_applied_accessors(&self, object: u32) -> bool;
    fn target_is_extensible(&self, object: u32) -> bool;
    fn heap_epoch(&self) -> u64;
}

pub(super) struct DescriptorApplication {
    pub(super) target: u32,
    pub(super) map_site: CallSite,
    pub(super) result: NativeObjectPostcondition,
}

#[derive(Debug, Clone)]
struct NativeProperty {
    data: bool,
    attributes: DescriptorAttributes,
}

/// A postcondition of one normal native return, not an exact shape for every
/// runtime object represented by its pooled allocation origin.
#[derive(Debug, Clone)]
pub(super) struct NativeObjectPostcondition {
    target: u32,
    epoch: u64,
    order: Vec<String>,
    properties: BTreeMap<String, NativeProperty>,
    dependencies: Box<ProofDependencies>,
}

impl NativeObjectPostcondition {
    pub(super) fn matches(&self, values: &Values) -> bool {
        object(values) == Some(self.target)
    }

    pub(super) fn after_mutation(mut self: Box<Self>, epoch: u64) -> Box<Self> {
        self.epoch = epoch;
        self
    }
}

fn target_shape(
    context: &impl DescriptorContext,
    values: &Values,
    postcondition: Option<&NativeObjectPostcondition>,
) -> Option<NativeObjectPostcondition> {
    let target = object(values)?;
    if !context.object_properties_are_settled(target) {
        return None;
    }
    if let Some(proof) = postcondition {
        return (proof.matches(values) && proof.epoch == context.heap_epoch())
            .then(|| proof.clone());
    }
    let shape = context.ordinary_record_shape(target)?;
    let properties = context.own_properties(target)?;
    if !context.target_is_extensible(target)
        || context.has_applied_accessors(target)
        || !properties
            .keys()
            .map(Option::as_deref)
            .eq(shape.properties.keys().map(|name| Some(name.as_str())))
        || shape.properties.values().any(|descriptor| {
            !matches!(
                descriptor,
                super::value_model::OwnPropertyDescriptorSyntax::Data
            )
        })
    {
        return None;
    }
    Some(NativeObjectPostcondition {
        target,
        epoch: context.heap_epoch(),
        order: shape.creation_order.clone(),
        properties: shape
            .properties
            .keys()
            .map(|name| {
                (
                    name.clone(),
                    NativeProperty {
                        data: true,
                        attributes: DescriptorAttributes::literal(true),
                    },
                )
            })
            .collect(),
        dependencies: Box::default(),
    })
}

fn object(values: &Values) -> Option<u32> {
    if values.len() != 1 {
        return None;
    }
    match values.first()? {
        AbstractValue::Value(Value::Alloc(Kind::Object, object)) => Some(*object),
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
    if descriptors.len() != 1 {
        return None;
    }
    let AbstractValue::OwnDescriptorMap(map_site) = descriptors.first()? else {
        return None;
    };
    let map = context.descriptor_map(*map_site).filter(|map| map.valid)?;
    for name in &map.property_order {
        if let Some(existing) = result.properties.get(name) {
            if existing.attributes.configurable != Some(true) {
                return None;
            }
        } else {
            result.order.push(name.clone());
        }
        result.properties.insert(
            name.clone(),
            NativeProperty {
                data: matches!(
                    map.properties.get(name)?,
                    CapturedPropertyDescriptor::Data(_)
                ),
                attributes: *map.attributes.get(name)?,
            },
        );
    }
    Some(DescriptorApplication {
        target: result.target,
        map_site: *map_site,
        result,
    })
}

pub(super) struct PropertyAssignment {
    pub(super) target: u32,
    pub(super) source: u32,
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
    let source_shape = target_shape(context, source, None)?;
    let source_properties = context.own_properties(source_shape.target)?;
    let mut values = BTreeMap::new();
    for name in &ordered_own_property_names(&source_shape.order) {
        let descriptor = source_shape.properties.get(name)?;
        if !descriptor.data || descriptor.attributes.enumerable != Some(true) {
            return None;
        }
        if let Some(existing) = result.properties.get(name) {
            if !existing.data || existing.attributes.writable != Some(true) {
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
                    data: true,
                    attributes: DescriptorAttributes::literal(true),
                },
            );
        }
        let contents = source_properties.get(&Some(name.clone()))?;
        if contents.is_empty() {
            return None;
        }
        values.insert(name.clone(), contents.clone());
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

pub(super) struct DescriptorOperationEffects {
    pub(super) coverage: EffectCoverageFragment,
    pub(super) heap: HeapContentCells,
    pub(super) result: Option<Box<NativeObjectPostcondition>>,
    pub(super) parameter_reads: BTreeSet<ParameterHeapRead>,
}

fn source(values: &Values) -> Option<DescriptorSource> {
    if values.len() != 1 {
        return None;
    }
    match values.first()? {
        AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
            Some(DescriptorSource::Object(*object))
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
pub(super) fn native_call_effects(
    context: &impl DescriptorContext,
    call: DescriptorCall<'_>,
    dependencies: &ProofDependencies,
) -> Option<DescriptorOperationEffects> {
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
    let mut result = None;
    if let Some(proof) = target_postcondition {
        coverage.depend_on(&proof.dependencies);
    }
    match row.op {
        IntrinsicOp::ObjectGetOwnPropertyDescriptors => {
            let proof = projection?;
            let OwnPropertyProjection::Descriptors(descriptors) = &proof.projection else {
                return None;
            };
            if source(arguments.first()?)? != descriptors.source_object {
                return None;
            }
            coverage.depend_on(&proof.dependencies);
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
            heap.allocations
                .insert(EffectCell::DescriptorMapContents(site.allocation_id()));
        }
        IntrinsicOp::ObjectGetOwnPropertyNames => {
            let proof = projection?;
            if !matches!(&proof.projection, OwnPropertyProjection::Names(_)) {
                return None;
            }
            coverage.depend_on(&proof.dependencies);
            record_inspection_read(
                &source(arguments.first()?)?,
                None,
                &mut heap,
                &mut parameter_reads,
            )?;
            heap.allocations
                .insert(EffectCell::ArrayContents(site.allocation_id()));
        }
        IntrinsicOp::ObjectDefineProperties => {
            let [target, descriptors] = arguments else {
                return None;
            };
            let application = application(context, target, descriptors, target_postcondition)?;
            let map = context.descriptor_map(application.map_site)?;
            heap.reads.insert(EffectCell::DescriptorMapContents(
                application.map_site.allocation_id(),
            ));
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
            result = Some(Box::new(application.result));
        }
        IntrinsicOp::ObjectAssign => {
            let [target, source] = arguments else {
                return None;
            };
            let assignment = assignment(context, target, source, target_postcondition)?;
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
            result = Some(Box::new(assignment.result));
        }
        _ => return None,
    }
    if let Some(proof) = &mut result {
        proof.dependencies.extend(dependencies);
    }
    Some(DescriptorOperationEffects {
        coverage,
        heap,
        result,
        parameter_reads,
    })
}

/// A caller supplies its prefix store. Admission owns native identity,
/// ordinary target/carrier, scope and completion proofs;
/// these updates grant neither ordinary-property shape nor effect coverage.
pub(super) struct DescriptorHeap<'a> {
    pub(super) maps: &'a mut OrdMap<CallSite, OwnDescriptorMapState>,
    pub(super) properties: &'a mut OrdMap<u32, BTreeMap<Option<String>, Values>>,
    pub(super) accessors: &'a mut OrdMap<u32, BTreeMap<String, AppliedAccessor>>,
    pub(super) revision: &'a mut u64,
}

impl DescriptorHeap<'_> {
    pub(super) fn capture(
        &mut self,
        site: CallSite,
        projection: OwnPropertyDescriptorProjection,
    ) -> AbstractValue {
        let OwnPropertyDescriptorProjection {
            source_object,
            ordered_names: property_order,
            properties,
            attributes,
            retained_values,
        } = projection;
        let changed = match self.maps.get_mut(&site) {
            Some(existing) => {
                if existing.source_object != source_object
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
                    site,
                    OwnDescriptorMapState {
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
        AbstractValue::OwnDescriptorMap(site)
    }

    pub(super) fn invalidate(&mut self, site: CallSite) {
        if let Some(map) = self.maps.get_mut(&site)
            && map.valid
        {
            map.valid = false;
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Apply only a still-valid captured map. The caller separately proves
    /// that defining these properties on its target completes normally.
    pub(super) fn apply(&mut self, target: u32, site: CallSite) -> bool {
        let Some(map) = self.maps.get(&site).filter(|map| map.valid).cloned() else {
            return false;
        };
        let mut applied_accessors = BTreeMap::new();
        let properties = self.properties.entry(target).or_default();
        for name in map.property_order {
            let descriptor = map
                .properties
                .get(&name)
                .expect("a captured descriptor map retains every ordered property");
            match descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    properties
                        .entry(Some(name))
                        .or_default()
                        .extend(values.iter().cloned());
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    properties.entry(Some(name.clone())).or_default().extend(
                        if getter.is_empty() {
                            Values::from([AbstractValue::UNDEFINED])
                        } else {
                            Values::from([AbstractValue::Value(Value::Top)])
                        },
                    );
                    applied_accessors.insert(
                        name,
                        AppliedAccessor {
                            getter: getter.clone(),
                            setter: setter.clone(),
                        },
                    );
                }
            }
        }
        // Attribute-only definitions invalidate short-lived postconditions too.
        *self.revision = self.revision.wrapping_add(1);
        for (name, accessor) in applied_accessors {
            let existing = self
                .accessors
                .entry(target)
                .or_default()
                .entry(name)
                .or_default();
            existing.getter.extend(accessor.getter);
            existing.setter.extend(accessor.setter);
        }
        true
    }

    pub(super) fn assign(&mut self, assignment: &PropertyAssignment) {
        let properties = self.properties.entry(assignment.target).or_default();
        for (name, values) in &assignment.values {
            properties
                .entry(Some(name.clone()))
                .or_default()
                .extend(values.iter().cloned());
        }
        *self.revision = self.revision.wrapping_add(1);
    }
}
