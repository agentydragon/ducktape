use super::context::{Disposition, KernelContext, Unproved};
use super::continuation::value_of;
use super::frame::{LocalFrame, RemainderDisposition};
use swc_common::Span;
use swc_ecma_ast::Id;
use value_model::{CallSite, LiteralPropertyKind, ValueExpr, Values};
use world::allocation_transfer::NativeIterableReceipt;
use world::allocation_transfer::NativeIterableUse;
use world::allocation_transfer::{AllocationContext, AllocationValue, LiteralHeap, LiteralLayout};
use world::effect_coverage::EffectCoverageFragment;

/// The kernel's view of `allocation_transfer` for a frame: binding reads go to
/// the adapter and effects into the ledger. An adapter refusal met while
/// transferring is kept so the caller reports the right remainder.
// CLEANUP(added 2026-11-09): dissolve with `AllocationContext` when L4e
//   deletes `legacy/evaluator.rs` and its `ApplicationAllocationContext`.
pub(super) struct LocalValueContext<'a, C> {
    pub(super) context: &'a mut C,
    pub(super) frame: &'a mut LocalFrame,
    pub(super) refusal: Option<RemainderDisposition>,
}

impl<C: KernelContext> AllocationContext for LocalValueContext<'_, C> {
    fn source_covered(&self) -> bool {
        true
    }
    fn array_created(&mut self, allocation: u32, tainted: bool) {
        if tainted {
            // A hole has no own property, so later index reads see the
            // prototype; an unproved spread has unknown slots. Either way the
            // object is no longer a settled literal.
            self.context
                .world_mut()
                .surface
                .tainted_objects
                .insert(allocation);
        }
    }
    /// The one `IterateSpread` rule: the adapter's receipt covers the
    /// protocol and its source reads go into this frame's ledger; without a
    /// receipt the body's remainder is the adapter's to summarize.
    fn array_spread(&mut self, span: Span, operand: &Values) -> Option<NativeIterableReceipt> {
        match self
            .context
            .native_iterable(NativeIterableUse::ArraySpread(span), operand)
        {
            Some(receipt) => {
                self.frame
                    .ledger
                    .heap_accesses
                    .reads
                    .extend(receipt.external_heap_contents().reads);
                self.frame.ledger.effects.compose(&receipt.coverage);
                Some(receipt)
            }
            None => {
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                self.refusal = Some(
                    match self
                        .context
                        .unproved(self.frame, Unproved::IterateSpread { operand, span })
                    {
                        Disposition::Summarized => RemainderDisposition::AlreadySummarized,
                        Disposition::Resolved(_) | Disposition::Refused => {
                            RemainderDisposition::NeedsSummary
                        }
                    },
                );
                None
            }
        }
    }
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        match self.context.read_binding(binding, self.frame) {
            Ok(result) => {
                self.frame.ledger.effects.compose(&result.effects);
                Some(result.allocation)
            }
            Err(remainder) => {
                self.refusal = Some(remainder);
                None
            }
        }
    }
    fn unmodeled_effect_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
    }
    fn noninvoking_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::no_effects());
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        match value_of(self.context, self.frame, expression) {
            Ok(value) => Some(value),
            Err(remainder) => {
                self.refusal = Some(remainder);
                None
            }
        }
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.context.index().allocation_layout(allocation)
    }
    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind {
        self.context
            .index()
            .literal_property_kind(allocation, position)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        self.context.world_mut().literal_heap()
    }
    fn keyed_collection_new(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        self.context
            .keyed_collection_new(self.frame, callee, arguments, site)
    }
}
