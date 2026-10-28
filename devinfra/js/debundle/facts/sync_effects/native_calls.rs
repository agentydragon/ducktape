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
    /// An opaque successful return retaining an input graph and its callbacks.
    DeferredArgument(usize),
    /// A capability for descriptor entries captured from one exact ordinary
    /// source object. It remains opaque except for the paired application.
    OwnDescriptorMap,
    /// `Object.defineProperties` returns its target when the call completes;
    /// application effects have a separate input guard.
    DefinedPropertiesTarget,
}

#[derive(Debug, Clone, Copy)]
pub(super) enum CallInputs {
    Primitives { min: usize, max: usize },
    FreshDataRecord,
    OwnDescriptorRecord,
    FreshDescriptorMapTarget,
    OrdinaryCallable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum NativeHeapEffect {
    Opaque,
    /// Initializes fresh state without user-observable existing heap effects
    /// under the guarded native identity/integrity premise.
    FreshAllocation,
}

#[derive(Debug, Clone, Copy)]
pub(super) struct NativeCallModel {
    pub(super) intrinsic: IntrinsicRoot,
    pub(super) inputs: CallInputs,
    pub(super) returns: CallReturn,
    pub(super) heap_effect: NativeHeapEffect,
}

pub(super) fn native_call_model(
    receiver: &str,
    member: Option<&str>,
    kind: CallKind,
) -> Option<NativeCallModel> {
    match (receiver, member, kind) {
        ("Map" | "Set" | "WeakMap" | "WeakSet", None, CallKind::New) => Some(NativeCallModel {
            intrinsic: match receiver {
                "Map" => IntrinsicRoot::Map,
                "Set" => IntrinsicRoot::Set,
                "WeakMap" => IntrinsicRoot::WeakMap,
                "WeakSet" => IntrinsicRoot::WeakSet,
                _ => unreachable!(),
            },
            inputs: CallInputs::Primitives { min: 0, max: 0 },
            returns: CallReturn::Unknown,
            heap_effect: NativeHeapEffect::FreshAllocation,
        }),
        ("FinalizationRegistry", None, CallKind::New) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::FinalizationRegistry,
            inputs: CallInputs::OrdinaryCallable,
            returns: CallReturn::DeferredArgument(0),
            heap_effect: NativeHeapEffect::Opaque,
        }),
        ("Math", Some("round" | "floor" | "ceil" | "trunc" | "abs" | "sign"), CallKind::Call) => {
            Some(NativeCallModel {
                intrinsic: IntrinsicRoot::Math,
                inputs: CallInputs::Primitives { min: 1, max: 1 },
                returns: CallReturn::Primitive,
                heap_effect: NativeHeapEffect::Opaque,
            })
        }
        ("Object", Some("getOwnPropertyDescriptors"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Object,
            inputs: CallInputs::OwnDescriptorRecord,
            returns: CallReturn::OwnDescriptorMap,
            heap_effect: NativeHeapEffect::Opaque,
        }),
        ("Object", Some("defineProperties"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Object,
            inputs: CallInputs::FreshDescriptorMapTarget,
            returns: CallReturn::DefinedPropertiesTarget,
            heap_effect: NativeHeapEffect::Opaque,
        }),
        ("Object", Some("freeze"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Object,
            inputs: CallInputs::FreshDataRecord,
            returns: CallReturn::Argument(0),
            heap_effect: NativeHeapEffect::Opaque,
        }),
        ("RegExp", None, CallKind::Call | CallKind::New) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::RegExp,
            inputs: CallInputs::Primitives { min: 0, max: 2 },
            returns: CallReturn::Unknown,
            heap_effect: NativeHeapEffect::Opaque,
        }),
        ("Symbol", None | Some("for"), CallKind::Call) => Some(NativeCallModel {
            intrinsic: IntrinsicRoot::Symbol,
            inputs: CallInputs::Primitives { min: 0, max: 1 },
            returns: CallReturn::Primitive,
            heap_effect: NativeHeapEffect::Opaque,
        }),
        _ => None,
    }
}
