//! Heap and native-operation transfers over one settled execution snapshot.

use super::effect_coverage::EffectCoverageFragment;
use super::execution_index::{CallKind, MemberValueWrite};
use super::interpreter::{Executor, NativeCallResolution};
use super::value_model::{CallSite, Context, ValueExpr, Values};
use std::collections::BTreeSet;
use swc_common::Span;

#[derive(Debug, Clone, Copy)]
pub(super) enum NativeIterableInput {
    Borrowed,
    /// Canonical immediate argument-array syntax, not origin uniqueness.
    ImmediateArrayLiteral(u32),
}

#[derive(Debug, Clone, Copy)]
pub(super) enum NativeIterableUse {
    ArraySpread(Span),
    SetConstruction {
        site: CallSite,
        input: NativeIterableInput,
    },
}

#[derive(Debug, Clone)]
pub(super) enum NativeIterableYields {
    ExactSlots(Vec<Values>),
    /// Only primitive yields are known. Never infer slots, cardinality,
    /// insertion order or Set deduplication from this proposition.
    PrimitiveOnly,
}

#[derive(Debug, Clone)]
pub(super) struct NativeIterableReceipt {
    /// `None` for the scoped imported-array transfer, which intentionally
    /// carries yields but no provider heap identity.
    pub(super) source: Option<u32>,
    pub(super) yields: NativeIterableYields,
    /// Body reads are retained even for a private input whose read cannot
    /// observe a caller's earlier heap. The external projection is below.
    pub(super) heap_contents: crate::facts::HeapContentCells,
    pub(super) input: NativeIterableInput,
    pub(super) coverage: EffectCoverageFragment,
}

impl NativeIterableReceipt {
    pub(super) fn external_heap_contents(&self) -> crate::facts::HeapContentCells {
        let mut external = self.heap_contents.clone();
        if let NativeIterableInput::ImmediateArrayLiteral(origin) = self.input
            && self.source == Some(origin)
        {
            // The operation validated the actual source against this canonical
            // immediate literal. This invocation consumes its newly built
            // array without retention; no earlier pooled generation is read.
            // Keep the Set allocation and every other source/body read.
            external
                .reads
                .remove(&crate::facts::EffectCell::ArrayContents(origin));
        }
        external
    }
}

pub(super) enum HeapOperation {
    IterateNative {
        operation: NativeIterableUse,
        operand: Values,
    },
    WriteMembers {
        writes: Vec<MemberValueWrite>,
        environment: Context,
        captures: Context,
        certified_data_receivers: BTreeSet<u32>,
        deferred_global_write: bool,
    },
    ResolveNative {
        kind: CallKind,
        callee: ValueExpr,
        site: CallSite,
        expressions: Vec<ValueExpr>,
        arguments: Vec<Values>,
    },
}

pub(super) enum HeapResult {
    Updated,
    Iteration(Box<Option<NativeIterableReceipt>>),
    Native(Box<Option<NativeCallResolution>>),
}

impl HeapOperation {
    pub(super) fn execute(self, executor: &mut Executor<'_, '_>) -> HeapResult {
        match self {
            Self::IterateNative { operation, operand } => HeapResult::Iteration(Box::new(
                executor.transfer_native_iterable(operation, &operand),
            )),
            Self::WriteMembers {
                writes,
                environment,
                captures,
                certified_data_receivers,
                deferred_global_write,
            } => {
                executor.apply_member_value_writes_transfer(
                    &writes,
                    &environment,
                    &captures,
                    &certified_data_receivers,
                    deferred_global_write,
                );
                HeapResult::Updated
            }
            Self::ResolveNative {
                kind,
                callee,
                site,
                expressions,
                arguments,
            } => HeapResult::Native(Box::new(executor.resolve_native_call_transfer(
                kind,
                &callee,
                site,
                &expressions,
                &arguments,
            ))),
        }
    }
}
