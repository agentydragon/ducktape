//! Builtin summary table: what each native operation coerces, invokes,
//! allocates, mutates and returns, with its ECMA-262 citation.
//!
//! The synchronous interpreter consults a row after the callee's native
//! identity is proved and admits the call only when every listed coercion is
//! silent for the call's abstract operands. Purity consults the same row for
//! the call's syntactic operands (`purity/native_rows.rs`). A row describes the
//! operation after callee and argument evaluation. Nothing else in the crate
//! names a builtin's semantics.

use super::effect::CoerceOp;
use super::heap::Mutation;
use super::integrity::IntrinsicSlot;
use super::value::{Kind, PrimKinds};
use crate::analysis_inputs::NativeIntrinsic;
use std::collections::BTreeSet;
use std::ops::RangeInclusive;
use std::sync::LazyLock;

/// An operand of the operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgSel {
    /// The zero-based positional argument; an absent argument is `undefined`.
    Arg(usize),
    /// Every positional argument from this index on.
    Rest(usize),
}

/// How the callee is invoked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Invocation {
    Call,
    Construct,
    /// A tagged template: `Arg(0)` is the engine's frozen template object and
    /// the substitutions follow.
    Tag,
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
    ArrayIsArray,
    NumberIsFinite,
    NumberIsInteger,
    NumberIsNaN,
    NumberIsSafeInteger,
    ObjectIs,
    ObjectKeys,
    ObjectValues,
    ObjectEntries,
    ObjectFromEntries,
    Boolean,
    Array,
    TextDecoder,
    TextEncoder,
    UrlSearchParams,
    Proxy,
    StringRaw,
}

impl IntrinsicOp {
    /// Source spelling: the global binding and its static member, if any.
    pub fn callee(self) -> (&'static str, Option<&'static str>) {
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
            Self::ArrayIsArray => ("Array", Some("isArray")),
            Self::NumberIsFinite => ("Number", Some("isFinite")),
            Self::NumberIsInteger => ("Number", Some("isInteger")),
            Self::NumberIsNaN => ("Number", Some("isNaN")),
            Self::NumberIsSafeInteger => ("Number", Some("isSafeInteger")),
            Self::ObjectIs => ("Object", Some("is")),
            Self::ObjectKeys => ("Object", Some("keys")),
            Self::ObjectValues => ("Object", Some("values")),
            Self::ObjectEntries => ("Object", Some("entries")),
            Self::ObjectFromEntries => ("Object", Some("fromEntries")),
            Self::Boolean => ("Boolean", None),
            Self::Array => ("Array", None),
            Self::TextDecoder => ("TextDecoder", None),
            Self::TextEncoder => ("TextEncoder", None),
            Self::UrlSearchParams => ("URLSearchParams", None),
            Self::Proxy => ("Proxy", None),
            Self::StringRaw => ("String", Some("raw")),
        }
    }
}

/// The successful completion value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Return {
    /// No shape is tracked.
    Unknown,
    /// A primitive of one of these types.
    Prim(PrimKinds),
    /// The operand itself, identity preserved.
    Arg(ArgSel),
    /// The allocation named by `Row::allocates`.
    Fresh,
}

/// Abrupt completions the operation produces beyond its listed coercions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Throws {
    Never,
    /// A TypeError unless every listed operand is an object.
    UnlessObjects(&'static [ArgSel]),
    /// A TypeError unless the operand is callable.
    UnlessCallable(ArgSel),
    /// On some operand values even after silent coercions: a pattern's syntax.
    OnValue,
}

/// One builtin's summary.
#[derive(Debug)]
pub struct Row {
    pub op: IntrinsicOp,
    /// Invocation kinds the row covers.
    pub invocation: &'static [Invocation],
    /// ECMA-262 section anchor of the summarized algorithm
    /// (`https://tc39.es/ecma262/#<cite>`), or the URL of a host
    /// specification's algorithm.
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
    pub throws: Throws,
    /// Whether syntactic purity may take the callee's identity from A11 and
    /// an unshadowed spelling alone. Otherwise purity reaches the operation
    /// only through a certificate proving `requires`: the census or a
    /// `native_operation_inputs` site.
    pub assumed_intact: bool,
    /// Slots the callee's identity reads: its global binding and, for a
    /// static member, that data property. The row applies only when every
    /// slot's integrity flag is intact or author-asserted at the call site.
    /// Empty for a callee outside the native census, whose identity rests on
    /// A11 alone; the synchronous interpreter never admits such a row.
    pub requires: &'static [IntrinsicSlot],
}

impl Row {
    /// The census family of the callee; every required slot belongs to it.
    pub fn family(&self) -> Option<NativeIntrinsic> {
        self.requires.first().map(|slot| slot.family())
    }
}

/// Every argument count.
const ANY_ARITY: RangeInclusive<usize> = 0..=usize::MAX;

const PRIMITIVE_UNARY_MATH: Row = Row {
    op: IntrinsicOp::MathRound,
    invocation: &[Invocation::Call],
    cite: "sec-math.round",
    arity: 1..=1,
    coerces: &[(ArgSel::Arg(0), CoerceOp::ToNumber)],
    invokes: &[],
    allocates: None,
    mutates: &[],
    returns: Return::Prim(PrimKinds::NUMBER),
    throws: Throws::Never,
    assumed_intact: false,
    requires: &[
        IntrinsicSlot::Global(NativeIntrinsic::Math),
        IntrinsicSlot::Static(NativeIntrinsic::Math, "round"),
    ],
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
    throws: Throws::Never,
    assumed_intact: true,
    requires: &[IntrinsicSlot::Global(NativeIntrinsic::Map)],
};

/// A call that inspects no operand: it neither coerces nor reads one.
const INSPECTS_NO_OPERAND: Row = Row {
    op: IntrinsicOp::ArrayIsArray,
    invocation: &[Invocation::Call],
    cite: "sec-array.isarray",
    arity: ANY_ARITY,
    coerces: &[],
    invokes: &[],
    allocates: None,
    mutates: &[],
    returns: Return::Prim(PrimKinds::BOOLEAN),
    throws: Throws::Never,
    assumed_intact: true,
    requires: &[
        IntrinsicSlot::Global(NativeIntrinsic::Array),
        IntrinsicSlot::Static(NativeIntrinsic::Array, "isArray"),
    ],
};

/// `EnumerableOwnProperties` of `ToObject(O)`, collected into a fresh array.
const OWN_ENUMERABLE: Row = Row {
    op: IntrinsicOp::ObjectKeys,
    invocation: &[Invocation::Call],
    cite: "sec-object.keys",
    arity: 1..=1,
    coerces: &[
        (ArgSel::Arg(0), CoerceOp::ToObject),
        (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
        (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
    ],
    invokes: &[],
    allocates: Some(Kind::Array),
    mutates: &[],
    returns: Return::Fresh,
    throws: Throws::Never,
    assumed_intact: true,
    requires: &[
        IntrinsicSlot::Global(NativeIntrinsic::Object),
        IntrinsicSlot::Static(NativeIntrinsic::Object, "keys"),
    ],
};

/// A host constructor's no-argument form.
const EMPTY_HOST_OBJECT: Row = Row {
    op: IntrinsicOp::TextDecoder,
    invocation: &[Invocation::Construct],
    cite: "https://encoding.spec.whatwg.org/#dom-textdecoder",
    arity: 0..=0,
    coerces: &[],
    invokes: &[],
    allocates: None,
    mutates: &[],
    returns: Return::Unknown,
    throws: Throws::Never,
    assumed_intact: true,
    requires: &[],
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
        returns: Return::Prim(PrimKinds::SYMBOL),
        throws: Throws::Never,
        assumed_intact: true,
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::Symbol)],
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
        returns: Return::Prim(PrimKinds::SYMBOL),
        throws: Throws::Never,
        assumed_intact: false,
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Symbol),
            IntrinsicSlot::Static(NativeIntrinsic::Symbol, "for"),
        ],
    },
    // Without an iterable, each constructor allocates its empty collection
    // and returns before looking up an adder or touching the iterator protocol.
    // With one, `Map` reads each entry's key and value and `Set` stores each
    // value through the intrinsic adder, compared by SameValueZero.
    Row {
        arity: 0..=1,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::Iterate),
            (ArgSel::Arg(0), CoerceOp::EntryGet),
        ],
        ..EMPTY_KEYED_COLLECTION
    },
    Row {
        op: IntrinsicOp::Set,
        cite: "sec-set-iterable",
        arity: 0..=1,
        coerces: &[(ArgSel::Arg(0), CoerceOp::Iterate)],
        allocates: Some(Kind::Set),
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::Set)],
        ..EMPTY_KEYED_COLLECTION
    },
    Row {
        op: IntrinsicOp::WeakMap,
        cite: "sec-weakmap-iterable",
        allocates: Some(Kind::WeakMap),
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::WeakMap)],
        ..EMPTY_KEYED_COLLECTION
    },
    Row {
        op: IntrinsicOp::WeakSet,
        cite: "sec-weakset-iterable",
        allocates: Some(Kind::WeakSet),
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::WeakSet)],
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
        throws: Throws::UnlessCallable(ArgSel::Arg(0)),
        assumed_intact: false,
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::FinalizationRegistry)],
    },
    PRIMITIVE_UNARY_MATH,
    Row {
        op: IntrinsicOp::MathFloor,
        cite: "sec-math.floor",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Math),
            IntrinsicSlot::Static(NativeIntrinsic::Math, "floor"),
        ],
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathCeil,
        cite: "sec-math.ceil",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Math),
            IntrinsicSlot::Static(NativeIntrinsic::Math, "ceil"),
        ],
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathTrunc,
        cite: "sec-math.trunc",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Math),
            IntrinsicSlot::Static(NativeIntrinsic::Math, "trunc"),
        ],
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathAbs,
        cite: "sec-math.abs",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Math),
            IntrinsicSlot::Static(NativeIntrinsic::Math, "abs"),
        ],
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::MathSign,
        cite: "sec-math.sign",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Math),
            IntrinsicSlot::Static(NativeIntrinsic::Math, "sign"),
        ],
        ..PRIMITIVE_UNARY_MATH
    },
    Row {
        op: IntrinsicOp::ObjectGetOwnPropertyDescriptors,
        invocation: &[Invocation::Call],
        cite: "sec-object.getownpropertydescriptors",
        arity: 1..=1,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::ToObject),
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
        ],
        invokes: &[],
        allocates: Some(Kind::Object),
        mutates: &[Mutation::FreshOwnDescriptorsOf(ArgSel::Arg(0))],
        returns: Return::Fresh,
        throws: Throws::Never,
        assumed_intact: false,
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "getOwnPropertyDescriptors"),
        ],
    },
    Row {
        op: IntrinsicOp::ObjectGetOwnPropertyNames,
        invocation: &[Invocation::Call],
        cite: "sec-object.getownpropertynames",
        arity: 1..=1,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::ToObject),
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
        ],
        invokes: &[],
        allocates: Some(Kind::Array),
        mutates: &[Mutation::FreshOwnKeysOf(ArgSel::Arg(0))],
        returns: Return::Fresh,
        throws: Throws::Never,
        assumed_intact: false,
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "getOwnPropertyNames"),
        ],
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
            (ArgSel::Arg(1), CoerceOp::ToObject),
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
        throws: Throws::UnlessObjects(&[ArgSel::Arg(0)]),
        assumed_intact: false,
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "defineProperties"),
        ],
    },
    // SetIntegrityLevel(frozen): the stored data values are never invoked. A
    // primitive operand is returned unchanged.
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
        throws: Throws::Never,
        assumed_intact: true,
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "freeze"),
        ],
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
        throws: Throws::OnValue,
        assumed_intact: false,
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::RegExp)],
    },
    // IsArray also throws a TypeError for a revoked Proxy, which `throws`
    // does not model; no user code runs.
    INSPECTS_NO_OPERAND,
    // `Number.is*` return false for a non-Number without converting it.
    // `Number`, `Boolean`, `String`, `Proxy` and the host constructors are
    // outside the native census.
    Row {
        op: IntrinsicOp::NumberIsFinite,
        cite: "sec-number.isfinite",
        requires: &[],
        ..INSPECTS_NO_OPERAND
    },
    Row {
        op: IntrinsicOp::NumberIsInteger,
        cite: "sec-number.isinteger",
        requires: &[],
        ..INSPECTS_NO_OPERAND
    },
    Row {
        op: IntrinsicOp::NumberIsNaN,
        cite: "sec-number.isnan",
        requires: &[],
        ..INSPECTS_NO_OPERAND
    },
    Row {
        op: IntrinsicOp::NumberIsSafeInteger,
        cite: "sec-number.issafeinteger",
        requires: &[],
        ..INSPECTS_NO_OPERAND
    },
    // SameValue dispatches on the operand types.
    Row {
        op: IntrinsicOp::ObjectIs,
        cite: "sec-object.is",
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "is"),
        ],
        ..INSPECTS_NO_OPERAND
    },
    // ToBoolean is decided by the operand type.
    Row {
        op: IntrinsicOp::Boolean,
        cite: "sec-boolean-constructor-boolean-value",
        requires: &[],
        ..INSPECTS_NO_OPERAND
    },
    OWN_ENUMERABLE,
    Row {
        op: IntrinsicOp::ObjectValues,
        cite: "sec-object.values",
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::ToObject),
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
            (ArgSel::Arg(0), CoerceOp::Get),
        ],
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "values"),
        ],
        ..OWN_ENUMERABLE
    },
    Row {
        op: IntrinsicOp::ObjectEntries,
        cite: "sec-object.entries",
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::ToObject),
            (ArgSel::Arg(0), CoerceOp::OwnPropertyKeys),
            (ArgSel::Arg(0), CoerceOp::GetOwnProperty),
            (ArgSel::Arg(0), CoerceOp::Get),
        ],
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "entries"),
        ],
        ..OWN_ENUMERABLE
    },
    // CreateDataPropertyOrThrow on a fresh ordinary object runs no user code.
    Row {
        op: IntrinsicOp::ObjectFromEntries,
        cite: "sec-object.fromentries",
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::Iterate),
            (ArgSel::Arg(0), CoerceOp::EntryGet),
            (ArgSel::Arg(0), CoerceOp::EntryKeyToPropertyKey),
        ],
        allocates: Some(Kind::Object),
        requires: &[
            IntrinsicSlot::Global(NativeIntrinsic::Object),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "fromEntries"),
        ],
        ..OWN_ENUMERABLE
    },
    // With no arguments the length is 0 and no RangeError is possible.
    Row {
        op: IntrinsicOp::Array,
        invocation: &[Invocation::Construct],
        cite: "sec-array",
        arity: 0..=0,
        allocates: Some(Kind::Array),
        returns: Return::Fresh,
        requires: &[IntrinsicSlot::Global(NativeIntrinsic::Array)],
        ..INSPECTS_NO_OPERAND
    },
    // The default label `"utf-8"` is valid; a supplied label can throw a
    // RangeError.
    EMPTY_HOST_OBJECT,
    Row {
        op: IntrinsicOp::TextEncoder,
        cite: "https://encoding.spec.whatwg.org/#dom-textencoder",
        ..EMPTY_HOST_OBJECT
    },
    // An object `init` is iterated or read as a record. A primitive is
    // converted to a string and parsed as application/x-www-form-urlencoded,
    // which accepts every string.
    Row {
        op: IntrinsicOp::UrlSearchParams,
        cite: "https://url.spec.whatwg.org/#dom-urlsearchparams-urlsearchparams",
        arity: 0..=1,
        coerces: &[(ArgSel::Arg(0), CoerceOp::ToString)],
        ..EMPTY_HOST_OBJECT
    },
    // ProxyCreate stores the target and handler without looking up a trap;
    // traps run on later operations on the proxy.
    Row {
        op: IntrinsicOp::Proxy,
        invocation: &[Invocation::Construct],
        cite: "sec-proxy-target-handler",
        arity: 2..=2,
        coerces: &[],
        invokes: &[],
        allocates: Some(Kind::Proxy),
        mutates: &[
            Mutation::FreshRetains(ArgSel::Arg(0)),
            Mutation::FreshRetains(ArgSel::Arg(1)),
        ],
        returns: Return::Fresh,
        throws: Throws::UnlessObjects(&[ArgSel::Arg(0), ArgSel::Arg(1)]),
        assumed_intact: true,
        requires: &[],
    },
    // The template object's `raw` strings are the engine's own frozen data;
    // each substitution is converted with ToString.
    Row {
        op: IntrinsicOp::StringRaw,
        invocation: &[Invocation::Tag],
        cite: "sec-string.raw",
        arity: 1..=usize::MAX,
        coerces: &[
            (ArgSel::Arg(0), CoerceOp::Get),
            (ArgSel::Rest(1), CoerceOp::ToString),
        ],
        invokes: &[],
        allocates: None,
        mutates: &[],
        returns: Return::Prim(PrimKinds::STRING),
        throws: Throws::Never,
        assumed_intact: true,
        requires: &[],
    },
];

/// The row for a direct call or construction of `global` or `global.member`.
pub fn lookup(global: &str, member: Option<&str>, invocation: Invocation) -> Option<&'static Row> {
    ROWS.iter()
        .find(|row| row.op.callee() == (global, member) && row.invocation.contains(&invocation))
}

/// What reading an intrinsic's own data property yields.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PropertyValue {
    Primitive,
    /// A builtin function; calling it is summarized only by its row.
    Function,
}

/// A non-accessor own property of a global constructor or namespace: reading
/// it runs no user code.
#[derive(Debug)]
pub struct DataProperty {
    pub global: &'static str,
    pub name: &'static str,
    pub value: PropertyValue,
    /// ECMA-262 section anchor listing the property.
    pub cite: &'static str,
}

const fn primitive(global: &'static str, name: &'static str, cite: &'static str) -> DataProperty {
    DataProperty {
        global,
        name,
        value: PropertyValue::Primitive,
        cite,
    }
}

const fn object_function(name: &'static str) -> DataProperty {
    DataProperty {
        global: "Object",
        name,
        value: PropertyValue::Function,
        cite: "sec-properties-of-the-object-constructor",
    }
}

const MATH: &str = "sec-value-properties-of-the-math-object";
const NUMBER: &str = "sec-properties-of-the-number-constructor";
const SYMBOL: &str = "sec-properties-of-the-symbol-constructor";

pub const DATA_PROPERTIES: &[DataProperty] = &[
    primitive("Math", "PI", MATH),
    primitive("Math", "E", MATH),
    primitive("Math", "LN2", MATH),
    primitive("Math", "LN10", MATH),
    primitive("Math", "LOG2E", MATH),
    primitive("Math", "LOG10E", MATH),
    primitive("Math", "SQRT2", MATH),
    primitive("Math", "SQRT1_2", MATH),
    primitive("Number", "EPSILON", NUMBER),
    primitive("Number", "MAX_SAFE_INTEGER", NUMBER),
    primitive("Number", "MIN_SAFE_INTEGER", NUMBER),
    primitive("Number", "MAX_VALUE", NUMBER),
    primitive("Number", "MIN_VALUE", NUMBER),
    primitive("Number", "POSITIVE_INFINITY", NUMBER),
    primitive("Number", "NEGATIVE_INFINITY", NUMBER),
    primitive("Number", "NaN", NUMBER),
    primitive("Symbol", "iterator", SYMBOL),
    primitive("Symbol", "asyncIterator", SYMBOL),
    primitive("Symbol", "dispose", SYMBOL),
    primitive("Symbol", "asyncDispose", SYMBOL),
    primitive("Symbol", "toStringTag", SYMBOL),
    primitive("Symbol", "toPrimitive", SYMBOL),
    primitive("Symbol", "hasInstance", SYMBOL),
    primitive("Symbol", "species", SYMBOL),
    primitive("Symbol", "isConcatSpreadable", SYMBOL),
    primitive("Symbol", "match", SYMBOL),
    primitive("Symbol", "replace", SYMBOL),
    primitive("Symbol", "search", SYMBOL),
    primitive("Symbol", "split", SYMBOL),
    object_function("defineProperty"),
    object_function("defineProperties"),
    object_function("freeze"),
    object_function("values"),
    object_function("keys"),
    object_function("entries"),
    object_function("fromEntries"),
    object_function("getOwnPropertyDescriptor"),
    object_function("getOwnPropertyDescriptors"),
    object_function("getOwnPropertyNames"),
    object_function("getOwnPropertySymbols"),
    object_function("getPrototypeOf"),
    object_function("setPrototypeOf"),
    object_function("create"),
    object_function("assign"),
    object_function("is"),
    object_function("isFrozen"),
    object_function("isSealed"),
    object_function("isExtensible"),
    object_function("preventExtensions"),
    object_function("seal"),
    object_function("hasOwn"),
];

/// The data property `global.name`.
pub fn data_property(global: &str, name: &str) -> Option<&'static DataProperty> {
    DATA_PROPERTIES
        .iter()
        .find(|property| property.global == global && property.name == name)
}

/// Every global binding a row or data property names. A source binding of
/// one of these names shadows the intrinsic.
pub static GLOBALS: LazyLock<BTreeSet<&'static str>> = LazyLock::new(|| {
    ROWS.iter()
        .map(|row| row.op.callee().0)
        .chain(DATA_PROPERTIES.iter().map(|property| property.global))
        .collect()
});

#[cfg(test)]
mod tests {
    use super::*;

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
            // The identity premise reads exactly the callee's spelling; a
            // callee outside the census has none.
            let (global, member) = row.op.callee();
            let expected =
                NativeIntrinsic::for_global_binding(global).map_or_else(Vec::new, |family| {
                    let mut slots = vec![IntrinsicSlot::Global(family)];
                    slots.extend(member.map(|member| IntrinsicSlot::Static(family, member)));
                    slots
                });
            assert_eq!(row.requires, expected.as_slice(), "{:?}", row.op);
        }
    }
}
