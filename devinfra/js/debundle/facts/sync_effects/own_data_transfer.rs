//! Present ordinary own-data slots at one settled execution prefix.

use super::value_model::{AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values};
use crate::facts::EffectCell;
use crate::facts::abstract_domain::value::Value;
use crate::reports::schema::OrderedFieldReadRefusal;
use std::collections::BTreeSet;

pub(super) trait OwnDataContext {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape>;
    fn object_properties_are_settled(&self, allocation: u32) -> bool;
    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values>;
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool;
    fn property_is_writable(&self, allocation: u32, property: &str) -> bool;
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
