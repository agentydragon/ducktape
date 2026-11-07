//! Present ordinary own-data slots at one settled execution prefix.

use super::value_model::{
    AbstractValue, InstanceOrigin, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    canonical_array_index, ordered_own_property_names,
};
use crate::execution_evidence::OrderedFieldReadRefusal;
use crate::facts::EffectCell;
use crate::facts::abstract_domain::value::Value;
use analysis_model::PrimKinds;
use std::collections::{BTreeMap, BTreeSet};

pub(super) trait OwnDataContext {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape>;
    fn own_properties(&self, allocation: u32) -> Option<&BTreeMap<Option<String>, Values>>;
    fn object_properties_are_settled(&self, allocation: u32) -> bool;
    /// The execution heap does not generally retain enumerable attributes.
    /// CopyDataProperties requires them, so contexts must opt in only when
    /// that descriptor premise is known for this object.
    fn copy_data_properties_are_exact(&self, _allocation: u32) -> bool {
        false
    }
    fn instance_properties_are_settled(&self, class: u32) -> bool;
    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values>;
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool;
    fn property_is_writable(&self, allocation: u32, property: &str) -> bool;

    /// Only application or isolated provider contexts with a current
    /// first-generation array-literal witness provide this length. Retained
    /// queries stay conservative until they carry the same prefix state.
    fn first_generation_dense_array_length(&self, _allocation: u32) -> Option<u32> {
        None
    }
}

/// The source snapshot for one CopyDataProperties operation. Names and values
/// are paired in ECMAScript own-key order, while property storage remains in
/// the ordinary heap.
pub(super) struct OwnDataCopySource {
    pub(super) origin: u32,
    pub(super) properties: Vec<(String, Values)>,
}

/// Resolve only a settled ordinary object whose complete own-key set and
/// descriptors match its prepared static shape. This models source Get and
/// own-key/descriptor support; the caller still performs destination
/// CreateDataProperty through the shared allocation heap.
pub(super) fn copy_source(
    context: &impl OwnDataContext,
    receivers: &Values,
) -> Option<OwnDataCopySource> {
    let [AbstractValue::Value(Value::Alloc(analysis_kind, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return None;
    };
    if *analysis_kind != crate::facts::abstract_domain::value::Kind::Object
        || !context.object_properties_are_settled(*object)
        || !context.copy_data_properties_are_exact(*object)
    {
        return None;
    }
    let shape = context.ordinary_record_shape(*object)?;
    let own = context.own_properties(*object)?;
    let names = ordered_own_property_names(&shape.creation_order);
    if names.len() != shape.properties.len()
        || own.len() != names.len()
        || shape.properties.iter().any(|(name, descriptor)| {
            !matches!(descriptor, OwnPropertyDescriptorSyntax::Data)
                || context.property_is_accessor(*object, name)
        })
        || names.iter().any(|name| {
            !shape.properties.contains_key(name)
                || !own.contains_key(&Some(name.clone()))
                || own.get(&Some(name.clone())).is_none_or(Values::is_empty)
        })
    {
        return None;
    }
    let properties = names
        .into_iter()
        .map(|name| {
            let values = context.property_values(*object, &name)?.clone();
            (!values.is_empty()).then_some((name, values))
        })
        .collect::<Option<Vec<_>>>()?;
    Some(OwnDataCopySource {
        origin: *object,
        properties,
    })
}

/// Ordered local mutation needs stronger ownership than a read from an
/// already checked source prefix. Neither mode proves allocation multiplicity.
pub(super) enum OwnDataOwnership<'a> {
    CurrentPrefix,
    FreshLocal {
        borrowed: &'a BTreeSet<u32>,
        fresh: &'a BTreeSet<u32>,
    },
}

pub(super) struct OwnDataSlot {
    pub(super) object: u32,
    property: String,
}

pub(super) struct OwnDataRead {
    pub(super) values: Values,
    pub(super) cell: EffectCell,
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
    pub(super) fn object(&self) -> u32 {
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
        // A class key is a pooled heap origin, not an allocation identity.
        // Never grant fresh-local ownership or a write from this read receipt.
        if !matches!(ownership, OwnDataOwnership::CurrentPrefix) {
            return Err(OrderedFieldReadRefusal::AllocationNotFresh);
        }
        if !own_data.contains(property) {
            return Err(OrderedFieldReadRefusal::MissingField);
        }
        if !context.instance_properties_are_settled(*class) {
            return Err(OrderedFieldReadRefusal::UnsettledReceiver);
        }
        if context.property_is_accessor(*class, property) {
            return Err(OrderedFieldReadRefusal::NonDataField);
        }
        if context.property_values(*class, property).is_none() {
            return Err(OrderedFieldReadRefusal::MissingHeapValue);
        }
        return Ok(PresentOwnDataSlot::InstanceField(OwnDataSlot {
            object: *class,
            property: property.to_owned(),
        }));
    }
    let Some(AbstractValue::Value(Value::Alloc(kind, object))) = receivers.first() else {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    };
    if !kind.supports_ordinary_own_data_read_proof() {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    }
    if let OwnDataOwnership::FreshLocal { borrowed, .. } = &ownership
        && borrowed.contains(object)
    {
        return Err(OrderedFieldReadRefusal::BorrowedReceiver);
    }
    if !context.object_properties_are_settled(*object) {
        return Err(OrderedFieldReadRefusal::UnsettledReceiver);
    }
    if let Some(length) = context.first_generation_dense_array_length(*object)
        && (property == "length" || canonical_array_index(property).is_some())
    {
        if property != "length"
            && !canonical_array_index(property).is_some_and(|index| index < length)
        {
            return Err(OrderedFieldReadRefusal::MissingField);
        }
        if let OwnDataOwnership::FreshLocal { fresh, .. } = &ownership
            && !fresh.contains(object)
        {
            return Err(OrderedFieldReadRefusal::AllocationNotFresh);
        }
        if context.property_is_accessor(*object, property) {
            return Err(OrderedFieldReadRefusal::NonDataField);
        }
        if context.property_values(*object, property).is_none() {
            return Err(OrderedFieldReadRefusal::MissingHeapValue);
        }
        let slot = OwnDataSlot {
            object: *object,
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
    if let OwnDataOwnership::FreshLocal { borrowed, .. } = &ownership
        && borrowed.contains(object)
    {
        return Err(OrderedFieldReadRefusal::BorrowedReceiver);
    }
    if !context.object_properties_are_settled(*object) {
        return Err(OrderedFieldReadRefusal::UnsettledReceiver);
    }
    let Some(shape) = context.ordinary_record_shape(*object) else {
        return Err(OrderedFieldReadRefusal::UnsupportedRecordShape);
    };
    if let OwnDataOwnership::FreshLocal { fresh, .. } = &ownership
        && !fresh.contains(object)
    {
        return Err(OrderedFieldReadRefusal::AllocationNotFresh);
    }
    let Some(descriptor) = shape.properties.get(property) else {
        return Err(OrderedFieldReadRefusal::MissingField);
    };
    if !matches!(descriptor, OwnPropertyDescriptorSyntax::Data) {
        return Err(OrderedFieldReadRefusal::NonDataField);
    }
    if context.property_is_accessor(*object, property) {
        return Err(OrderedFieldReadRefusal::NonDataField);
    }
    if context.property_values(*object, property).is_none() {
        return Err(OrderedFieldReadRefusal::MissingHeapValue);
    }
    Ok(OwnDataSlot {
        object: *object,
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
) -> Option<OwnDataSlot> {
    let slot = resolve_slot(
        context,
        receivers,
        property,
        OwnDataOwnership::CurrentPrefix,
    )
    .ok()?;
    context
        .property_is_writable(slot.object, property)
        .then_some(slot)
}
