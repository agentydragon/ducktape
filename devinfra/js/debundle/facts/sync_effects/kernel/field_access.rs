use super::context::{Disposition, KernelContext, Unproved};
use super::continuation::{absorb, completing, value_of};
use super::frame::{LocalFrame, LocalValueResult, RemainderDisposition, SelectedValue};
use analysis::facts::statement_facts::EffectCell;
use analysis_vocabulary::execution_evidence::OrderedFieldReadRefusal;
use swc_common::Span;
use swc_ecma_ast::Id;
use value_model::own_data_transfer::{self, OwnDataOwnership, OwnDataSlot};
use value_model::{CallableSelection, ValueExpr, Values};
use world::allocation_transfer::{AllocationJudgment, AllocationValue};
use world::effect_coverage::EffectCoverageFragment;

pub(super) fn own_data_field(
    context: &impl KernelContext,
    receivers: &Values,
    property: &str,
    frame: &LocalFrame,
) -> Result<OwnDataSlot, OrderedFieldReadRefusal> {
    own_data_transfer::resolve_slot(
        context,
        receivers,
        property,
        OwnDataOwnership::FreshLocal {
            borrowed: &frame.borrowed_records,
            fresh: &frame.fresh_records,
        },
    )
}

pub(super) fn read_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receiver: &Id,
    receivers: &Values,
    property: &str,
    span: Span,
) -> Result<LocalValueResult, RemainderDisposition> {
    let prefix_tracked = context.prefix_tracked_receiver(receiver, receivers);
    let slot = if prefix_tracked {
        own_data_transfer::resolve_slot(
            context,
            receivers,
            property,
            OwnDataOwnership::CurrentPrefix,
        )
    } else {
        own_data_field(context, receivers, property, frame)
    };
    match slot {
        Ok(slot) => {
            let selection_dependencies = context.current_prerequisites();
            let read = slot.read(context);
            let selection =
                CallableSelection::observed(&read.values, selection_dependencies.clone());
            frame.ledger.heap_accesses.reads.insert(read.cell);
            // [[Get]] of a present own data property runs no getter,
            // prototype lookup, global or host operation; its one heap
            // effect is the slot cell recorded above.
            let mut effects = EffectCoverageFragment::no_effects();
            if prefix_tracked {
                effects.depend_on(selection_dependencies.as_ref());
            }
            Ok(LocalValueResult {
                allocation: AllocationValue {
                    values: read.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
                },
                effects,
                selection,
            })
        }
        Err(refusal) => match context.unproved(
            frame,
            Unproved::FieldRead {
                receiver,
                receivers,
                property,
                span,
                refusal,
            },
        ) {
            Disposition::Resolved(allocation) => Ok(LocalValueResult {
                allocation,
                effects: EffectCoverageFragment::default(),
                selection: None,
            }),
            Disposition::Summarized => Err(RemainderDisposition::AlreadySummarized),
            Disposition::Refused => Err(RemainderDisposition::NeedsSummary),
        },
    }
}

pub(super) fn read_field_expression(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receiver: &Id,
    property: &str,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    if let Some(result) = context.native_field_read(frame, receiver, property, span) {
        return Ok(absorb(frame, result));
    }
    let receivers = value_of(context, frame, &ValueExpr::Binding(receiver.clone()))?;
    frame.ledger.allocation.compose(&receivers.judgment);
    let receivers = completing(receivers.values, frame);
    if receivers.is_empty() {
        return Ok(SelectedValue::raw(Values::new()));
    }
    let result = read_field(context, frame, receiver, &receivers, property, span)?;
    Ok(absorb(frame, result))
}

pub(super) fn store_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receivers: &Values,
    prefix_tracked: bool,
    property: &str,
    values: &Values,
    span: Span,
) -> Result<(), RemainderDisposition> {
    let slot = if prefix_tracked {
        own_data_transfer::resolve_writable_slot(context, receivers, property)
    } else {
        own_data_field(context, receivers, property, frame).ok()
    };
    let Some(slot) = slot else {
        return Err(
            match context.unproved(
                frame,
                Unproved::FieldWrite {
                    receivers,
                    values,
                    span,
                },
            ) {
                Disposition::Summarized => RemainderDisposition::AlreadySummarized,
                Disposition::Resolved(_) | Disposition::Refused => {
                    RemainderDisposition::NeedsSummary
                }
            },
        );
    };
    let write_dependencies = context
        .property_is_writable(slot.object, property)
        .then(|| context.current_prerequisites());
    frame.ledger.heap_accesses.writes.insert(slot.cell());
    // Writability is an attribute of the object's shape: a later
    // redefinition or integrity change must stay ordered after this store.
    frame
        .ledger
        .heap_accesses
        .reads
        .insert(EffectCell::ObjectShape(slot.object));
    context.world_mut().literal_heap().write_property(
        slot.object,
        Some(property.to_owned()),
        values,
    );
    // [[Set]] on an existing writable own data property of an ordinary
    // object with settled accessors replaces its value: no setter, prototype
    // lookup, global or host operation runs. The one heap effect is the
    // slot cell recorded above.
    let effects = match write_dependencies {
        Some(dependencies) => {
            let mut effects = EffectCoverageFragment::no_effects();
            effects.depend_on(dependencies.as_ref());
            effects
        }
        None => EffectCoverageFragment::default(),
    };
    frame.ledger.effects.compose(&effects);
    Ok(())
}
