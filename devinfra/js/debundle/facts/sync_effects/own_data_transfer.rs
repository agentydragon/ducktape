//! Present ordinary own-data slots at one settled execution prefix.

use super::object_surface::{
    ExtensibilityKnowledge, ObjectSurfaceStore, OwnPropertyKnowledge, PrototypeKnowledge,
};
use super::value_model::{
    AbstractValue, InstanceOrigin, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    canonical_array_index, ordered_own_property_names,
};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::{ObjectSpreadRefusal, OrderedFieldReadRefusal};
use crate::facts::EffectCell;
use crate::facts::abstract_domain::value::{
    ClassDefinitionId, HeapAddress, SourceHeapResolver, Value,
};
use analysis_model::PrimKinds;
use std::collections::{BTreeMap, BTreeSet};

pub(super) trait OwnDataContext {
    fn heap_resolver(&self) -> SourceHeapResolver;
    /// Source allocation coordinates and admissible runtime heap addresses
    /// are separate when a checked provider executes in the caller world.
    fn permits_heap_address(&self, address: HeapAddress) -> bool {
        address.module() == Some(self.heap_resolver().scope())
    }
    fn object_surface(&self) -> &ObjectSurfaceStore;
    fn ordinary_record_shape(&self, allocation: HeapAddress) -> Option<&OwnPropertyShape>;
    fn own_properties(&self, allocation: HeapAddress) -> Option<&BTreeMap<Option<String>, Values>>;
    fn object_properties_are_settled(&self, allocation: HeapAddress) -> bool;
    /// The execution heap does not generally retain enumerable attributes.
    /// CopyDataProperties requires them, so contexts must opt in only when
    /// that descriptor premise is known for this object.
    fn copy_data_properties_are_exact(&self, _allocation: HeapAddress) -> bool {
        false
    }
    fn instance_properties_are_settled(&self, class: ClassDefinitionId) -> bool;
    fn property_values(&self, allocation: HeapAddress, property: &str) -> Option<&Values>;
    fn property_is_accessor(&self, allocation: HeapAddress, property: &str) -> bool;
    fn property_is_writable(&self, allocation: HeapAddress, property: &str) -> bool;

    fn own_property_knowledge(
        &self,
        allocation: HeapAddress,
        property: &str,
    ) -> OwnPropertyKnowledge {
        let source_accessor = self
            .ordinary_record_shape(allocation)
            .and_then(|shape| shape.properties.get(property))
            .is_some_and(|descriptor| {
                matches!(descriptor, OwnPropertyDescriptorSyntax::Accessor { .. })
            });
        let known_absent = self.object_properties_are_settled(allocation)
            && self
                .ordinary_record_shape(allocation)
                .is_some_and(|shape| !shape.properties.contains_key(property));
        let knowledge = self
            .object_surface()
            .own_property(allocation, property, known_absent);
        if source_accessor && matches!(&knowledge, OwnPropertyKnowledge::Data { .. }) {
            let mut retained_values = self
                .property_values(allocation, property)
                .cloned()
                .unwrap_or_default();
            retained_values.insert(AbstractValue::Value(
                crate::facts::abstract_domain::value::Value::Top,
            ));
            OwnPropertyKnowledge::Unknown { retained_values }
        } else {
            knowledge
        }
    }

    fn prototype_knowledge(&self, allocation: HeapAddress) -> PrototypeKnowledge {
        self.object_surface().prototype(allocation)
    }

    fn extensibility_knowledge(&self, allocation: HeapAddress) -> ExtensibilityKnowledge {
        self.object_surface().extensibility(allocation)
    }

    /// Only application or isolated provider contexts with a current
    /// first-generation array-literal witness provide this length. Retained
    /// queries stay conservative until they carry the same prefix state.
    fn first_generation_dense_array_length(&self, _allocation: HeapAddress) -> Option<u32> {
        None
    }
}

trait ResolveAllocationIdentity: Copy {
    fn in_scope(
        self,
        kind: crate::facts::abstract_domain::value::Kind,
        context: &impl OwnDataContext,
    ) -> Option<HeapAddress>;
}

impl ResolveAllocationIdentity for u32 {
    fn in_scope(
        self,
        kind: crate::facts::abstract_domain::value::Kind,
        context: &impl OwnDataContext,
    ) -> Option<HeapAddress> {
        Some(context.heap_resolver().allocation(kind, self))
    }
}

impl ResolveAllocationIdentity for HeapAddress {
    fn in_scope(
        self,
        kind: crate::facts::abstract_domain::value::Kind,
        context: &impl OwnDataContext,
    ) -> Option<HeapAddress> {
        (context.permits_heap_address(self) && self.kind() == Some(kind)).then_some(self)
    }
}

trait ResolveClassIdentity: Copy {
    fn in_scope(self, context: &impl OwnDataContext) -> Option<ClassDefinitionId>;
}

impl ResolveClassIdentity for u32 {
    fn in_scope(self, context: &impl OwnDataContext) -> Option<ClassDefinitionId> {
        Some(context.heap_resolver().class_definition(self))
    }
}

impl ResolveClassIdentity for ClassDefinitionId {
    fn in_scope(self, context: &impl OwnDataContext) -> Option<ClassDefinitionId> {
        (self.module == context.heap_resolver().scope()).then_some(self)
    }
}

/// Settled own-data payload for one CopyDataProperties operation. Enumeration
/// is independent: an uncertain enumerable subset can retain possible values
/// without an exact copied shape or an operation receipt.
pub(super) struct OwnDataCopySource {
    pub(super) origin: HeapAddress,
    pub(super) properties: Vec<(String, Values)>,
    pub(super) enumerability_settled: bool,
}

/// Resolve an ordinary data payload matching its prepared/runtime shape.
/// Return the first failed check in the existing guard order; enumeration is
/// reported on the payload because values can still be retained when only its
/// enumerable subset is unsettled.
pub(super) fn copy_source(
    context: &impl OwnDataContext,
    receivers: &Values,
) -> Result<OwnDataCopySource, ObjectSpreadRefusal> {
    let [AbstractValue::Value(Value::Alloc(analysis_kind, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return Err(ObjectSpreadRefusal::ReceiverNotSingleton);
    };
    if *analysis_kind != crate::facts::abstract_domain::value::Kind::Object {
        return Err(ObjectSpreadRefusal::ReceiverNotOrdinaryObject);
    }
    let origin = object
        .in_scope(*analysis_kind, context)
        .ok_or(ObjectSpreadRefusal::ReceiverNotOrdinaryObject)?;
    if !context.object_properties_are_settled(origin) {
        return Err(ObjectSpreadRefusal::ReceiverUnsettled);
    }
    let shape = context
        .ordinary_record_shape(origin)
        .ok_or(ObjectSpreadRefusal::MissingShape)?;
    let own = context
        .own_properties(origin)
        .ok_or(ObjectSpreadRefusal::MissingOwnProperties)?;
    let names = ordered_own_property_names(&shape.creation_order);
    if names.len() != shape.properties.len() {
        return Err(ObjectSpreadRefusal::ShapePropertyCountMismatch);
    }
    if own.len() != names.len() {
        return Err(ObjectSpreadRefusal::OwnPropertyCountMismatch);
    }
    for (name, descriptor) in &shape.properties {
        if !matches!(descriptor, OwnPropertyDescriptorSyntax::Data) {
            return Err(ObjectSpreadRefusal::NonDataDescriptor);
        }
        if context.property_is_accessor(origin, name) {
            return Err(ObjectSpreadRefusal::AccessorProperty);
        }
    }
    for name in &names {
        if !shape.properties.contains_key(name) {
            return Err(ObjectSpreadRefusal::MissingShapeProperty);
        }
        let Some(values) = own.get(&Some(name.clone())) else {
            return Err(ObjectSpreadRefusal::MissingOwnProperty);
        };
        if values.is_empty() {
            return Err(ObjectSpreadRefusal::EmptyOwnPropertyValue);
        }
    }
    let mut properties = Vec::with_capacity(names.len());
    for name in names {
        let values = context
            .property_values(origin, &name)
            .ok_or(ObjectSpreadRefusal::MissingHeapValue)?
            .clone();
        if values.is_empty() {
            return Err(ObjectSpreadRefusal::EmptyOwnPropertyValue);
        }
        if values.contains(&AbstractValue::Value(Value::Unavailable)) {
            return Err(ObjectSpreadRefusal::UnavailableOwnPropertyValue);
        }
        properties.push((name, values));
    }
    Ok(OwnDataCopySource {
        origin,
        properties,
        enumerability_settled: context.copy_data_properties_are_exact(origin),
    })
}

/// Ordered local mutation needs stronger ownership than a read from an
/// already checked source prefix. Neither mode proves allocation multiplicity.
pub(super) enum OwnDataOwnership<'a> {
    CurrentPrefix,
    FreshLocal {
        borrowed: &'a BTreeSet<HeapAddress>,
        fresh: &'a BTreeSet<HeapAddress>,
    },
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct OwnDataSlot {
    pub(super) object: HeapAddress,
    property: String,
}

/// An own-data slot whose current prefix also proves it is writable. This
/// proof is tied to the operation that consumes the slot; an allocation
/// origin or a writer observed elsewhere is not enough.
#[derive(Clone, PartialEq, Eq)]
pub(super) struct WritableOwnDataSlot(OwnDataSlot);

impl WritableOwnDataSlot {
    pub(super) fn slot(&self) -> &OwnDataSlot {
        &self.0
    }

    fn cell(&self) -> EffectCell {
        self.0.cell()
    }
}

/// The postcondition of one ordinary member store. A proved replacement
/// changes only the value cell. Every other modeled store may alter own-key
/// or descriptor shape as well as the named value cell.
#[derive(Clone, PartialEq, Eq)]
pub(super) enum OwnPropertyStorePostcondition {
    ReplaceExistingWritableData(WritableOwnDataSlot),
    MayCreateOrRedefine {
        origin: HeapAddress,
        property: Option<String>,
    },
}

impl OwnPropertyStorePostcondition {
    pub(super) fn write_cells(&self) -> BTreeSet<EffectCell> {
        match self {
            Self::ReplaceExistingWritableData(slot) => BTreeSet::from([slot.cell()]),
            Self::MayCreateOrRedefine { origin, property } => {
                // Every may-create/redefine store can alter a receiver's
                // visible surface. `__proto__` changes prototype state instead
                // of one own slot, so retain the whole-shape footprint without
                // inventing an OwnProperty cell for it.
                let mut cells = BTreeSet::from([EffectCell::ObjectShape(*origin)]);
                if let Some(property) = property
                    .as_deref()
                    .filter(|property| *property != "__proto__")
                {
                    cells.insert(EffectCell::OwnProperty {
                        origin: *origin,
                        property: property.to_owned(),
                    });
                }
                cells
            }
        }
    }

    pub(super) fn proves_replacement_for(&self, receiver: &AbstractValue, property: &str) -> bool {
        let Self::ReplaceExistingWritableData(slot) = self else {
            return false;
        };
        receiver
            == &AbstractValue::Value(Value::Alloc(
                crate::facts::abstract_domain::value::Kind::Object,
                slot.0.object,
            ))
            && slot.0.property == property
    }
}

pub(super) struct OwnDataRead {
    pub(super) values: Values,
    pub(super) cell: EffectCell,
    pub(super) dependencies: ProofDependencies,
}

/// A checked present own-data read. The variant retains which shape proof
/// admitted the read so callers with additional ordinary-record restrictions
/// can keep those restrictions without rejecting proved array slots.
pub(super) enum PresentOwnDataSlot {
    OrdinaryRecord(OwnDataSlot),
    InstanceField(OwnDataSlot),
    ArrayIndex(OwnDataSlot),
    /// Read-only view of the native own `length` data property. This variant
    /// is available only while the array-literal prefix proves its value;
    /// it is deliberately not accepted by the writable-slot resolver.
    ArrayLength(OwnDataSlot),
}

impl PresentOwnDataSlot {
    pub(super) fn object(&self) -> HeapAddress {
        match self {
            Self::OrdinaryRecord(slot)
            | Self::InstanceField(slot)
            | Self::ArrayIndex(slot)
            | Self::ArrayLength(slot) => slot.object,
        }
    }

    pub(super) fn property(&self) -> &str {
        match self {
            Self::OrdinaryRecord(slot)
            | Self::InstanceField(slot)
            | Self::ArrayIndex(slot)
            | Self::ArrayLength(slot) => &slot.property,
        }
    }

    pub(super) fn is_array_specific(&self) -> bool {
        matches!(self, Self::ArrayIndex(_) | Self::ArrayLength(_))
    }

    pub(super) fn is_instance_field(&self) -> bool {
        matches!(self, Self::InstanceField(_))
    }

    pub(super) fn read(self, context: &impl OwnDataContext) -> OwnDataRead {
        match self {
            Self::OrdinaryRecord(slot) | Self::InstanceField(slot) | Self::ArrayIndex(slot) => {
                slot.read(context)
            }
            Self::ArrayLength(slot) => OwnDataRead {
                values: Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))]),
                cell: slot.cell(),
                dependencies: context
                    .object_surface()
                    .own_property_dependencies(slot.object),
            },
        }
    }
}

impl OwnDataSlot {
    pub(super) fn cell(&self) -> EffectCell {
        EffectCell::OwnProperty {
            origin: self.object,
            property: self.property.clone(),
        }
    }

    /// Consume the checked slot immediately, without an intervening transfer.
    pub(super) fn read(self, context: &impl OwnDataContext) -> OwnDataRead {
        OwnDataRead {
            values: context
                .property_values(self.object, &self.property)
                .expect("the checked own-data slot is still present at this prefix")
                .clone(),
            cell: self.cell(),
            dependencies: context
                .object_surface()
                .own_property_dependencies(self.object),
        }
    }
}

/// Resolve a present read at the current settled prefix. An array index needs
/// the same first-generation dense literal witness as native `length`, plus
/// an in-range own data value. Neither proof grants a write.
pub(super) fn resolve_present_read(
    context: &impl OwnDataContext,
    receivers: &Values,
    property: &str,
    ownership: OwnDataOwnership<'_>,
) -> Result<PresentOwnDataSlot, OrderedFieldReadRefusal> {
    if receivers.len() != 1 {
        return Err(OrderedFieldReadRefusal::ReceiverNotSingleton);
    }
    if let Some(AbstractValue::Instance(class, _, InstanceOrigin::Constructed { own_data })) =
        receivers.first()
    {
        // A class definition identifies behavior, not an instance heap.
        // Constructor executions share a distinct pooled heap family; this
        // read never grants fresh-local ownership or a write receipt.
        if !matches!(ownership, OwnDataOwnership::CurrentPrefix) {
            return Err(OrderedFieldReadRefusal::AllocationNotFresh);
        }
        if !own_data.contains(property) {
            return Err(OrderedFieldReadRefusal::MissingField);
        }
        let class = class
            .in_scope(context)
            .ok_or(OrderedFieldReadRefusal::UnsettledReceiver)?;
        if !context.instance_properties_are_settled(class) {
            return Err(OrderedFieldReadRefusal::UnsettledReceiver);
        }
        let instance = context.heap_resolver().constructor_instances(class);
        if context.property_is_accessor(instance, property) {
            return Err(OrderedFieldReadRefusal::NonDataField);
        }
        if context.property_values(instance, property).is_none() {
            return Err(OrderedFieldReadRefusal::MissingHeapValue);
        }
        return Ok(PresentOwnDataSlot::InstanceField(OwnDataSlot {
            object: instance,
            property: property.to_owned(),
        }));
    }
    let Some(AbstractValue::Value(Value::Alloc(kind, object))) = receivers.first() else {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    };
    if !kind.supports_ordinary_own_data_read_proof() {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    }
    let origin = object
        .in_scope(*kind, context)
        .ok_or(OrderedFieldReadRefusal::ReceiverNotObject)?;
    if let OwnDataOwnership::FreshLocal { borrowed, .. } = &ownership
        && borrowed.contains(&origin)
    {
        return Err(OrderedFieldReadRefusal::BorrowedReceiver);
    }
    if !context.object_properties_are_settled(origin) {
        return Err(OrderedFieldReadRefusal::UnsettledReceiver);
    }
    if let Some(length) = context.first_generation_dense_array_length(origin)
        && (property == "length" || canonical_array_index(property).is_some())
    {
        if property != "length"
            && !canonical_array_index(property).is_some_and(|index| index < length)
        {
            return Err(OrderedFieldReadRefusal::MissingField);
        }
        if let OwnDataOwnership::FreshLocal { fresh, .. } = &ownership
            && !fresh.contains(&origin)
        {
            return Err(OrderedFieldReadRefusal::AllocationNotFresh);
        }
        if context.property_is_accessor(origin, property) {
            return Err(OrderedFieldReadRefusal::NonDataField);
        }
        if context.property_values(origin, property).is_none() {
            return Err(OrderedFieldReadRefusal::MissingHeapValue);
        }
        let slot = OwnDataSlot {
            object: origin,
            property: property.to_owned(),
        };
        return Ok(if property == "length" {
            PresentOwnDataSlot::ArrayLength(slot)
        } else {
            PresentOwnDataSlot::ArrayIndex(slot)
        });
    }
    resolve_slot(context, receivers, property, ownership).map(PresentOwnDataSlot::OrdinaryRecord)
}

pub(super) fn resolve_slot(
    context: &impl OwnDataContext,
    receivers: &Values,
    property: &str,
    ownership: OwnDataOwnership<'_>,
) -> Result<OwnDataSlot, OrderedFieldReadRefusal> {
    if receivers.len() != 1 {
        return Err(OrderedFieldReadRefusal::ReceiverNotSingleton);
    }
    let Some(AbstractValue::Value(Value::Alloc(kind, object))) = receivers.first() else {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    };
    if !kind.supports_ordinary_own_data_read_proof() {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    }
    let origin = object
        .in_scope(*kind, context)
        .ok_or(OrderedFieldReadRefusal::ReceiverNotObject)?;
    if let OwnDataOwnership::FreshLocal { borrowed, .. } = &ownership
        && borrowed.contains(&origin)
    {
        return Err(OrderedFieldReadRefusal::BorrowedReceiver);
    }
    if !context.object_properties_are_settled(origin) {
        return Err(OrderedFieldReadRefusal::UnsettledReceiver);
    }
    if let OwnDataOwnership::FreshLocal { fresh, .. } = &ownership
        && !fresh.contains(&origin)
    {
        return Err(OrderedFieldReadRefusal::AllocationNotFresh);
    }
    match context.own_property_knowledge(origin, property) {
        OwnPropertyKnowledge::KnownAbsent => {
            return Err(OrderedFieldReadRefusal::MissingField);
        }
        OwnPropertyKnowledge::Data { .. } => {}
        OwnPropertyKnowledge::Accessor { .. } => {
            return Err(OrderedFieldReadRefusal::NonDataField);
        }
        OwnPropertyKnowledge::Unknown { .. } => {
            return Err(OrderedFieldReadRefusal::UnsupportedRecordShape);
        }
    }
    Ok(OwnDataSlot {
        object: origin,
        property: property.to_owned(),
    })
}

/// Resolve the same canonical slot as a read, then consume the independent
/// current-prefix writable witness. A present Data slot alone never proves a
/// normally completing write.
pub(super) fn resolve_writable_slot(
    context: &impl OwnDataContext,
    receivers: &Values,
    property: &str,
) -> Option<WritableOwnDataSlot> {
    let slot = resolve_slot(
        context,
        receivers,
        property,
        OwnDataOwnership::CurrentPrefix,
    )
    .ok()?;
    writable_slot(context, slot).ok()
}

/// Classify a store against the actual slot selected for this operation.
/// Failure to prove a writable existing data property keeps the conservative
/// shape-changing postcondition even when a source-level assignment was
/// recognized.
pub(super) fn store_postcondition(
    context: &impl OwnDataContext,
    slot: OwnDataSlot,
) -> OwnPropertyStorePostcondition {
    match writable_slot(context, slot) {
        Ok(slot) => OwnPropertyStorePostcondition::ReplaceExistingWritableData(slot),
        Err(slot) => OwnPropertyStorePostcondition::MayCreateOrRedefine {
            origin: slot.object,
            property: Some(slot.property),
        },
    }
}

fn writable_slot(
    context: &impl OwnDataContext,
    slot: OwnDataSlot,
) -> Result<WritableOwnDataSlot, OwnDataSlot> {
    if context.property_is_writable(slot.object, &slot.property) {
        Ok(WritableOwnDataSlot(slot))
    } else {
        Err(slot)
    }
}
