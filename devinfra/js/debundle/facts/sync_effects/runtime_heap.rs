//! Heap and native-operation transfers over one settled execution snapshot.

use super::execution_index::{CallKind, MemberValueWrite};
use super::interpreter::{Executor, NativeCallResolution};
use std::collections::BTreeSet;
use value_model::{CallSite, Context, ValueExpr, Values};
use world::allocation_transfer::{NativeIterableReceipt, NativeIterableUse};

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
