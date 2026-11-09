//! The five narrow adapter traits. An adapter supplies policy; the kernel owns
//! operation order and semantics.
//!
//! Methods marked `CLEANUP` are kernel semantics in the target design that
//! still need shared state devel does not have yet: lexical cells live in the
//! application `ExecutionState`, not in a `World` both adapters share.

use super::super::allocation_transfer::{AllocationValue, LiteralHeap};
use super::super::array_transfer::ArrayPushSelection;
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::execution_index::ExecutionIndex;
use super::super::local_operations::DeferredCallableCreation;
use super::super::own_data_transfer::OwnDataContext;
use super::super::value_model::{CallSite, ValueExpr, Values};
use super::frame::{LocalCallRequest, LocalFrame, LocalValueResult, RemainderDisposition};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedFieldReadRefusal;
use crate::facts::EffectCell;
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
    // CLEANUP(added 2026-11-09): the five methods below become kernel cell
    // semantics (`cell_policy`/`cell_effect` adapter policy) once L2b's
    // `World.cells` exists; today the application adapter owns the cells in
    // `ExecutionState` and the strict provider has none.
    /// A store to a binding this body does not declare.
    fn assign_capture(
        &mut self,
        frame: &mut LocalFrame,
        binding: &Id,
        values: &Values,
        span: Span,
    ) -> Result<AllocationValue, RemainderDisposition>;
    fn instantiate_parameter_bindings(&mut self, _frame: &mut LocalFrame) {}
    fn instantiate_bindings(&mut self, _frame: &mut LocalFrame) {}
    fn local_binding_stored(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    /// Effects of declarative-cell creation, initialization, or a checked
    /// store to an initialized local. Captured cells remain separate.
    fn declarative_binding_operation(
        &self,
        _binding: &Id,
        _frame: &LocalFrame,
    ) -> EffectCoverageFragment {
        EffectCoverageFragment::default()
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
    fn heap(&mut self) -> LiteralHeap<'_>;
    // CLEANUP(added 2026-11-09): derive from `World.settled_dependencies`
    // once L2b lands; the application snapshots its prefix prerequisites here
    // and the strict provider has none.
    /// Conditions under which the current settled prefix is valid.
    fn settled_dependencies(&self) -> Box<ProofDependencies> {
        Box::default()
    }
    // CLEANUP(added 2026-11-09): closure creation becomes one kernel rule
    // (`AbstractValue::Closure(HeapAddress, LexicalCaptures)`) once L2b gives
    // both adapters a shared `World` with lexical cells and qualified
    // addresses; today the two adapters build different closure values.
    fn closure(
        &mut self,
        frame: &mut LocalFrame,
        creation: DeferredCallableCreation,
    ) -> Result<LocalValueResult, RemainderDisposition>;
}

pub(in crate::facts::sync_effects) trait NativeContext {
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
    fn publish_throw(&mut self, values: &Values);
    // CLEANUP(added 2026-11-09): the effect sink moves to `World.scope` when
    // L2b lands; an isolated provider scope then discards instead of
    // overriding these.
    fn record_property_read(&mut self, cell: EffectCell);
    fn record_property_write(&mut self, cell: EffectCell);
}
