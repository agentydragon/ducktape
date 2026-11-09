//! The five narrow adapter traits. An adapter supplies policy; the kernel owns
//! operation order and semantics.

use super::super::allocation_transfer::AllocationValue;
use super::super::array_transfer::ArrayPushSelection;
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::execution_index::ExecutionIndex;
use super::super::local_operations::DeferredCallableCreation;
use super::super::own_data_transfer::OwnDataContext;
use super::super::runtime_heap::{NativeIterableReceipt, NativeIterableUse};
use super::super::value_model::{CallSite, CapturedContext, LexicalCaptures, ValueExpr, Values};
use super::super::world::World;
use super::closure::{CapturePolicy, ClosureAdmission};
use super::frame::{LocalCallRequest, LocalFrame, LocalValueResult, RemainderDisposition};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedFieldReadRefusal;
use rustc_hash::FxHashSet;
use swc_common::Span;
use swc_ecma_ast::Id;

pub(in crate::facts::sync_effects) trait BindingContext {
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition>;
    /// Whether this exact evaluated receiver came from an initialized,
    /// non-imported module binding at the current prefix. Strict provider
    /// queries keep the default false and require fresh-local ownership.
    fn module_prefix_receiver(&self, _binding: &Id, _receivers: &Values) -> bool {
        false
    }
    /// Which of this body's bindings the kernel gives `World` cells.
    fn cell_policy(&self) -> CellPolicy<'_>;
    /// How a closure created here retains free bindings without a cell.
    fn capture_policy(&self) -> CapturePolicy<'_>;
    /// The effects of creating, initializing or storing to a declared binding
    /// that has no cell: no closure can observe it.
    fn cell_effect(&self) -> EffectCoverageFragment;
}

/// Which declared bindings get `World` cells. A binding without one lives only
/// in the frame, so no closure can observe its stores.
#[derive(Clone, Copy)]
pub(in crate::facts::sync_effects) struct CellPolicy<'a> {
    /// The bindings some closure captures.
    pub(in crate::facts::sync_effects) captured: &'a FxHashSet<Id>,
}

impl CellPolicy<'_> {
    pub(in crate::facts::sync_effects) fn owns(self, binding: &Id) -> bool {
        self.captured.contains(binding)
    }
}

pub(in crate::facts::sync_effects) trait CallContext:
    BindingContext
{
    /// The one selected direct-import call may prove its module callee read
    /// after source closure validation. Other reads use ordinary coverage.
    fn read_call_callee(
        &mut self,
        binding: &Id,
        _call_span: Span,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        self.read_binding(binding, frame)
    }
    /// Operands have already been evaluated in source order. Return the value
    /// from that invocation's effect transfer; do not evaluate the call again.
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        request: &LocalCallRequest,
    ) -> Result<LocalValueResult, RemainderDisposition>;
}

pub(in crate::facts::sync_effects) trait HeapContext:
    OwnDataContext
{
    fn index(&self) -> &ExecutionIndex;
    /// The stores this body mutates in place. Observations never go here;
    /// the kernel returns them in its ledger.
    fn world(&self) -> &World;
    fn world_mut(&mut self) -> &mut World;
    /// Conditions under which the current settled prefix is valid at this
    /// moment: `World.prerequisites` plus whatever the adapter's queries have
    /// consumed since it was last folded in.
    fn current_prerequisites(&self) -> Box<ProofDependencies>;
    /// Whether this creation may proceed, and what creating it contributes.
    /// The kernel has already computed the captures; the body does not run.
    fn admit_closure(
        &mut self,
        frame: &LocalFrame,
        creation: DeferredCallableCreation,
        captures: &LexicalCaptures,
    ) -> Result<ClosureAdmission, RemainderDisposition>;
    /// The adapter's run-scoped identity for these captures.
    fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext;
}

pub(in crate::facts::sync_effects) trait NativeContext {
    /// The native iterator protocol over an already evaluated operand, as a
    /// receipt the adapter proves from its prefix (intact Array intrinsics,
    /// settled dense slots or a primitive-array premise). The kernel's
    /// `IterateSpread` rule consumes it; `None` is a typed refusal through
    /// `ControlContext::unproved`. The strict provider has no prefix to prove
    /// it from.
    fn native_iterable(
        &mut self,
        _operation: NativeIterableUse,
        _operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        None
    }
    /// Select `Array.prototype.push` on this receiver before arguments run.
    fn array_method_callee(
        &mut self,
        _receivers: &Values,
        _property: &str,
    ) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
        None
    }
    /// An exact native binding and own-data slot proof can cover this whole
    /// lookup, including its global receiver. Other contexts evaluate the
    /// receiver and retain the ordinary own-data or accessor path.
    fn native_field_read(
        &mut self,
        _frame: &mut LocalFrame,
        _receiver: &Id,
        _property: &str,
        _span: Span,
    ) -> Option<LocalValueResult> {
        None
    }
    /// The adapter proves original constructor identity for this canonical New
    /// expression. The operation's row and result semantics are shared.
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _site: CallSite,
    ) -> Option<AllocationValue> {
        None
    }
}

/// An operation the kernel could not prove from its own state. Its operands
/// have already run; the adapter must not evaluate them again.
pub(in crate::facts::sync_effects) enum Unproved<'a> {
    /// The receiver cell was read at this operation and no fresh-local or
    /// module-prefix own-data slot was found.
    FieldRead {
        receiver: &'a Id,
        receivers: &'a Values,
        property: &'a str,
        span: Span,
        refusal: OrderedFieldReadRefusal,
    },
    FieldWrite {
        receivers: &'a Values,
        values: &'a Values,
        span: Span,
    },
    /// Both operands completed, but at least one may be an object or unknown
    /// value whose coercion can execute user code.
    Operator { operands: &'a Values, span: Span },
    /// The spread operand completed, but no native iterator receipt covers
    /// it: the protocol may run user code.
    IterateSpread { operand: &'a Values, span: Span },
    /// A store to a binding this body does not declare and no active cell
    /// holds: it reaches state outside the kernel's world.
    CaptureStore { values: &'a Values, span: Span },
}

pub(in crate::facts::sync_effects) enum Disposition {
    /// Only for `Unproved::FieldRead`: an established read proof outside the
    /// kernel's own-data transfer supplied the value and the body continues.
    Resolved(AllocationValue),
    /// The adapter summarized the body's remainder; the kernel stops.
    Summarized,
    /// The adapter cannot certify the body and summarized nothing.
    Refused,
}

/// Everything the kernel asks of one adapter.
pub(in crate::facts::sync_effects) trait KernelContext:
    BindingContext + CallContext + HeapContext + NativeContext + ControlContext
{
}

impl<C: BindingContext + CallContext + HeapContext + NativeContext + ControlContext> KernelContext
    for C
{
}

pub(in crate::facts::sync_effects) trait ControlContext {
    fn unproved(&mut self, frame: &mut LocalFrame, what: Unproved<'_>) -> Disposition;
}
