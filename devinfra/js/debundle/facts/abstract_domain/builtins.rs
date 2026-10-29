//! Builtin summary table: what each native operation coerces, invokes,
//! allocates, mutates and returns, with its ECMA-262 citation.
//!
//! The synchronous interpreter consults a row after the callee's native
//! identity is proved and admits the call only when every listed coercion is
//! silent for the call's abstract operands. A row describes the operation
//! after callee and argument evaluation; it asserts no purity and no
//! exception freedom. Nothing else in the crate names a builtin's semantics.

use super::effect::CoerceOp;
use super::heap::Mutation;
use super::value::Kind;
use crate::analysis_inputs::NativeIntrinsic;
use std::ops::RangeInclusive;

/// An operand of the operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgSel {
    /// The zero-based positional argument; an absent argument is `undefined`.
    Arg(usize),
}

/// How the callee is invoked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Invocation {
    Call,
    Construct,
}

/// One intrinsic function object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntrinsicOp {
    Symbol,
    SymbolFor,
    Map,
    Set,
    WeakMap,
    WeakSet,
    FinalizationRegistry,
    MathRound,
    MathFloor,
    MathCeil,
    MathTrunc,
    MathAbs,
    MathSign,
    ObjectGetOwnPropertyDescriptors,
    ObjectGetOwnPropertyNames,
    ObjectDefineProperties,
    ObjectFreeze,
    RegExp,
}

impl IntrinsicOp {
    /// Source spelling: the global binding and its static member, if any.
    fn callee(self) -> (&'static str, Option<&'static str>) {
        match self {
            Self::Symbol => ("Symbol", None),
            Self::SymbolFor => ("Symbol", Some("for")),
            Self::Map => ("Map", None),
            Self::Set => ("Set", None),
            Self::WeakMap => ("WeakMap", None),
            Self::WeakSet => ("WeakSet", None),
            Self::FinalizationRegistry => ("FinalizationRegistry", None),
            Self::MathRound => ("Math", Some("round")),
            Self::MathFloor => ("Math", Some("floor")),
            Self::MathCeil => ("Math", Some("ceil")),
            Self::MathTrunc => ("Math", Some("trunc")),
            Self::MathAbs => ("Math", Some("abs")),
            Self::MathSign => ("Math", Some("sign")),
            Self::ObjectGetOwnPropertyDescriptors => ("Object", Some("getOwnPropertyDescriptors")),
            Self::ObjectGetOwnPropertyNames => ("Object", Some("getOwnPropertyNames")),
            Self::ObjectDefineProperties => ("Object", Some("defineProperties")),
            Self::ObjectFreeze => ("Object", Some("freeze")),
            Self::RegExp => ("RegExp", None),
        }
    }
}

/// The successful completion value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Return {
    /// No shape is tracked.
    Unknown,
    /// A primitive.
    Primitive,
    /// The operand itself, identity preserved.
    Arg(ArgSel),
    /// The allocation named by `Row::allocates`.
    Fresh,
}

/// One builtin's summary.
#[derive(Debug)]
pub struct Row {
    pub op: IntrinsicOp,
    /// Invocation kinds the row covers.
    pub invocation: &'static [Invocation],
    /// ECMA-262 section anchor of the summarized algorithm
    /// (`https://tc39.es/ecma262/#<cite>`).
    pub cite: &'static str,
    /// Admitted argument counts; a spread argument is never admitted.
    pub arity: RangeInclusive<usize>,
    /// Steps that run user code unless the interpreter proves the operand
    /// primitive or an exact ordinary object.
    pub coerces: &'static [(ArgSel, CoerceOp)],
    /// Operands invoked synchronously.
    pub invokes: &'static [ArgSel],
    /// The fresh allocation the operation creates.
    pub allocates: Option<Kind>,
    pub mutates: &'static [Mutation],
    pub returns: Return,
    /// Intrinsic family whose integrity the native call identity judgment
    /// requires together with the global object.
    pub requires: NativeIntrinsic,
}

const PRIMITIVE_UNARY_MATH: Row = Row {
    op: IntrinsicOp::MathRound,
    invocation: &[Invocation::Call],
    cite: "sec-math.round",
    arity: 1..=1,
    coerces: &[(ArgSel::Arg(0), CoerceOp::ToNumber)],
    invokes: &[],
    allocates: None,
    mutates: &[],
    returns: Return::Primitive,
    requires: NativeIntrinsic::Math,
};

const EMPTY_KEYED_COLLECTION: Row = Row {
    op: IntrinsicOp::Map,
    invocation: &[Invocation::Construct],
    cite: "sec-map-iterable",
    arity: 0..=0,
    coerces: &[],
    invokes: &[],
    allocates: Some(Kind::Map),
    mutates: &[],
    returns: Return::Fresh,
    requires: NativeIntrinsic::Map,
};

pub const ROWS: &[Row] = &[
    Row {
        op: IntrinsicOp::Symbol,
        invocation: &[Invocation::Call],
        cite: "sec-symbol-description",
        arity: 0..=1,
        coerces: &[(ArgSel::Arg(0), CoerceOp::ToString)],
        invokes: &[],
        allocates: None,
        mutates: &[],
        returns: Return::Primitive,
        requires: NativeIntrinsic::Symbol,
    },
    // The GlobalSymbolRegistry is agent-local and unobservable from source.
    Row {
        op: IntrinsicOp::SymbolFor,
        invocation: &[Invocation::Call],
        cite: "sec-symbol.for",
        arity: 0..=1,
        coerces: &[(ArgSel::Arg(0), CoerceOp::ToString)],
        invokes: &[],
        allocates: None,
        mutates: &[],
        returns: Return::Primitive,
        requires: NativeIntrinsic::Symbol,
    },
    // Without an iterable, each constructor allocates its empty collection
    // and returns before looking up an adder or touching the iterator protocol.
    EMPTY_KEYED_COLLECTION,
    Row {
        op: IntrinsicOp::Set,
        cite: "sec-set-iterable",
        allocates: Some(Kind::Set),
        requires: NativeIntrinsic::Set,
        ..EMPTY_KEYED_COLLECTION
    },
    Row {
        op: IntrinsicOp::WeakMap,
        cite: "sec-weakmap-iterable",
        allocates: Some(Kind::WeakMap),
        requires: NativeIntrinsic::WeakMap,
        ..EMPTY_KEYED_COLLECTION
    },
    Row {
        op: IntrinsicOp::WeakSet,
        cite: "sec-weakset-iterable",
        allocates: Some(Kind::WeakSet),
        requires: NativeIntrinsic::WeakSet,
        ..EMPTY_KEYED_COLLECTION
    },
    // Checks callability, then stores the callback through
    // HostMakeJobCallback without invoking it.
    Row {
        op: IntrinsicOp::FinalizationRegistry,
        invocation: &[Invocation::Construct],
        cite: "sec-finalization-registry-cleanup-callback",
        arity: 1..=1,
        coerces: &[],
        invokes: &[],
        allocates: Some(Kind::Registry),
        mutates: &[Mutation::FreshRetains(ArgSel::Arg(0))],
        returns: Return::Fresh,
        requires: NativeIntrinsic::FinalizationRegistry,
    },
    PRIMITIVE_UNARY_MATH,
    Row {
        op: IntrinsicOp::MathFloor,
        cite: "sec-math.floor",
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathCeil,
        cite: "sec-math.ceil",
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathTrunc,
        cite: "sec-math.trunc",
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathAbs,
        cite: "sec-math.abs",
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathSign,
        cite: "sec-math.sign",
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::ObjectGetOwnPropertyDescriptors,
        invocation: &[Invocation::Call],
        cite: "sec-object.getownpropertydescriptors",
        arity: 1..=1,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
        ],
        invokes: &[],
        allocates: Some(Kind::Object),
        mutates: &[Mutation::FreshOwnDescriptorsOf(ArgSel::Arg(0))],
        returns: Return::Fresh,
        requires: NativeIntrinsic::Object,
    },
    Row {
        op: IntrinsicOp::ObjectGetOwnPropertyNames,
        invocation: &[Invocation::Call],
        cite: "sec-object.getownpropertynames",
        arity: 1..=1,
        coerces: &[(ArgSel::Arg(0), CoerceOp::OwnPropertyKeys)],
        invokes: &[],
        allocates: Some(Kind::Array),
        mutates: &[Mutation::FreshOwnKeysOf(ArgSel::Arg(0))],
        returns: Return::Fresh,
        requires: NativeIntrinsic::Object,
    },
    // ObjectDefineProperties reads every enumerable own descriptor of the
    // record, converts it with ToPropertyDescriptor, then defines it on the
    // target.
    Row {
        op: IntrinsicOp::ObjectDefineProperties,
        invocation: &[Invocation::Call],
        cite: "sec-object.defineproperties",
        arity: 2..=2,
        coerces: &[
            (ArgSel::Arg(1), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(1), CoerceOp::GetOwnProperty),
            (ArgSel::Arg(1), CoerceOp::Get),
            (ArgSel::Arg(0), CoerceOp::DefineOwnProperty),
        ],
        invokes: &[],
        allocates: None,
        mutates: &[Mutation::DefineProperties {
            target: ArgSel::Arg(0),
            descriptors: ArgSel::Arg(1),
        }],
        returns: Return::Arg(ArgSel::Arg(0)),
        requires: NativeIntrinsic::Object,
    },
    // SetIntegrityLevel(frozen): the stored data values are never invoked.
    Row {
        op: IntrinsicOp::ObjectFreeze,
        invocation: &[Invocation::Call],
        cite: "sec-object.freeze",
        arity: 1..=1,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::PreventExtensions),
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
            (ArgSel::Arg(0), CoerceOp::DefineOwnProperty),
        ],
        invokes: &[],
        allocates: None,
        mutates: &[Mutation::Integrity(ArgSel::Arg(0))],
        returns: Return::Arg(ArgSel::Arg(0)),
        requires: NativeIntrinsic::Object,
    },
    // IsRegExp reads `@@match` only from an object pattern; a called `RegExp`
    // returns an object pattern itself when its `constructor` is `%RegExp%`,
    // so the result shape is tracked only as an allocation. Pattern syntax
    // errors stay observable.
    Row {
        op: IntrinsicOp::RegExp,
        invocation: &[Invocation::Call, Invocation::Construct],
        cite: "sec-regexp-pattern-flags",
        arity: 0..=2,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::Get),
            (ArgSel::Arg(0), CoerceOp::ToString),
            (ArgSel::Arg(1), CoerceOp::ToString),
        ],
        invokes: &[],
        allocates: Some(Kind::RegExp),
        mutates: &[],
        returns: Return::Unknown,
        requires: NativeIntrinsic::RegExp,
    },
];

/// The row for a direct call or construction of `global` or `global.member`.
pub fn lookup(global: &str, member: Option<&str>, invocation: Invocation) -> Option<&'static Row> {
    ROWS.iter()
        .find(|row| row.op.callee() == (global, member) && row.invocation.contains(&invocation))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    /// `lookup` returns the first match, so one op has one row; every write
    /// to the fresh allocation needs that allocation.
    #[test]
    fn rows_are_unambiguous_and_fresh_writes_have_an_allocation() {
        let mut ops = BTreeSet::new();
        for row in ROWS {
            assert!(
                ops.insert(format!("{:?}", row.op)),
                "duplicate row {:?}",
                row.op
            );
            let writes_fresh = row.mutates.iter().any(|mutation| {
                matches!(
                    mutation,
                    Mutation::FreshOwnDescriptorsOf(_)
                        | Mutation::FreshOwnKeysOf(_)
                        | Mutation::FreshRetains(_)
                )
            });
            assert!(
                !(writes_fresh || row.returns == Return::Fresh) || row.allocates.is_some(),
                "{:?} uses a fresh allocation it does not declare",
                row.op
            );
        }
    }
}
