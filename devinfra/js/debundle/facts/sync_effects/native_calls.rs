//! Native call models: successful return shape and a guarded no-reentry proof.
//!
//! These describe the operation after callee/argument evaluation. They do not
//! assert purity, suppress argument effects, or certify exception-free calls.
//! The interpreter shares identity, mutation and input premises between
//! return-value evaluation and synchronous callback effects.

use super::{CallKind, IntrinsicRoot};

#[derive(Debug, Clone, Copy)]
pub(super) enum CallReturn {
    Unknown,
    Primitive,
    Argument(usize),
}

#[derive(Debug, Clone, Copy)]
pub(super) enum CallInputs {
    Primitives { min: usize, max: usize },
    FreshDataRecord,
}

#[derive(Debug, Clone, Copy)]
pub(super) struct NativeCallModel {
    pub(super) intrinsic: IntrinsicRoot,
    pub(super) inputs: CallInputs,
    pub(super) returns: CallReturn,
}

pub(super) fn native_call_model(
    receiver: &str,
    member: Option<&str>,
    kind: CallKind,
) -> Option<NativeCallModel> {
    match (receiver, member, kind) {
        ("Math", Some("round" | "floor" | "ceil" | "trunc" | "abs" | "sign"), CallKind::Call) => {
            Some(NativeCallModel {
                intrinsic: IntrinsicRoot::Math,
                inputs: CallInputs::Primitives { min: 1, max: 1 },
                returns: CallReturn::Primitive,
            })
        }
        ("Object", Some("freeze"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Object,
            inputs: CallInputs::FreshDataRecord,
            returns: CallReturn::Argument(0),
        }),
        ("RegExp", None, CallKind::Call | CallKind::New) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::RegExp,
            inputs: CallInputs::Primitives { min: 0, max: 2 },
            returns: CallReturn::Unknown,
        }),
        ("Symbol", None | Some("for"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Symbol,
            inputs: CallInputs::Primitives { min: 0, max: 1 },
            returns: CallReturn::Primitive,
        }),
        _ => None,
    }
}
