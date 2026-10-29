//! Heap and native-operation transfers over one settled execution snapshot.

use super::super::value_model::{CallSite, Context, ValueExpr, Values};
use super::CallKind;
use super::interpreter::{Executor, NativeCallResolution};
use std::collections::BTreeSet;
use swc_ecma_ast::Id;

pub(super) enum HeapOperation {
    WriteMembers {
        writes: Vec<(Id, Option<String>, ValueExpr)>,
        environment: Context,
        captures: Context,
        certified_data_receivers: BTreeSet<u32>,
        deferred_global_write: bool,
    },
    ApplyDescriptors {
        arguments: Vec<Values>,
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
    Native(Box<Option<NativeCallResolution>>),
}

impl HeapOperation {
    pub(super) fn execute(self, executor: &mut Executor<'_, '_>) -> HeapResult {
        match self {
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
            Self::ApplyDescriptors { arguments } => {
                executor.apply_own_descriptor_map_transfer(&arguments);
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
