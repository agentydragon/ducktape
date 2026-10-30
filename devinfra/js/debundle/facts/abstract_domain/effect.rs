//! Steps a builtin performs on its operands that can run user code.

/// An abstract operation or internal method a builtin applies to one operand.
/// Each can run user code: a conversion through a `ToPrimitive` hook, an
/// internal method through a Proxy trap or an accessor. A primitive operand
/// makes every step silent; an exact ordinary object makes the internal
/// methods silent under an intact prototype chain.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoerceOp {
    /// `ToString`.
    ToString,
    /// `ToNumber`.
    ToNumber,
    /// `[[Get]]` of a named property, including descriptor fields.
    Get,
    /// `[[OwnPropertyKeys]]`.
    OwnPropertyKeys,
    /// `[[GetOwnProperty]]` per own key.
    GetOwnProperty,
    /// `[[DefineOwnProperty]]` per key.
    DefineOwnProperty,
    /// `[[PreventExtensions]]`.
    PreventExtensions,
    /// `ToObject`: a TypeError for `undefined` and `null`.
    ToObject,
    /// `GetIterator` and `IteratorStep` over the operand, then the row's
    /// operation on each produced value.
    Iterate,
    /// `[[Get]]` of `"0"` and `"1"` on each iterated value: a TypeError for a
    /// primitive value.
    EntryGet,
    /// `ToPropertyKey` of each iterated entry's `"0"` value.
    EntryKeyToPropertyKey,
}

impl CoerceOp {
    /// Whether a primitive operand makes the step run no user code and
    /// complete normally, apart from `ToString` of a Symbol and `ToNumber` of
    /// a BigInt. `ToObject` rejects `undefined` and `null`; iteration reads
    /// the primitive's prototype.
    pub fn silent_on_primitive(self) -> bool {
        !matches!(
            self,
            Self::ToObject | Self::Iterate | Self::EntryGet | Self::EntryKeyToPropertyKey
        )
    }
}

/// What evaluating source can do that the analysis did not discharge. A
/// statement or function is pure when evaluation has no effect; each effect
/// projects to the `PurityRule` that reports it (`purity/purity_type.rs`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Effect {
    /// Writes a binding or a property: assignment, update, or an initializer
    /// of a `var` another statement also declares.
    Write,
    /// Deletes a property.
    Delete,
    /// A conversion or protocol step on an operand not proven to run no user
    /// code.
    Coerce(CoerceSite),
    /// Calls or constructs code no summary covers, or reads a property that
    /// may be an accessor.
    Invoke(InvokeSite),
    /// May throw on some operand the analysis admits: a primitive operation
    /// across numeric kinds, `ToNumber` of a BigInt, an iterated hole.
    Throws,
    /// Leaves straight-line synchronous evaluation, or is syntax the transfer
    /// does not model.
    Havoc(HavocCause),
}

/// Where a coercion or protocol step happens.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CoerceSite {
    /// `ToPrimitive`/`ToNumeric`/`ToString` of an operator or template
    /// interpolation, `in`'s `[[HasProperty]]`, `instanceof`'s
    /// `@@hasInstance`, or a builtin row's operand conversion.
    Operator,
    /// `ToPropertyKey` of a computed key.
    PropertyKey,
    /// `for-of`/`for-in` iteration.
    Iteration,
    /// A destructuring pattern's `[[Get]]` or iteration.
    Destructuring,
    /// Array or argument spread.
    ArraySpread,
    /// Object spread.
    ObjectSpread,
}

/// What kind of unsummarized code an `Invoke` may run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum InvokeSite {
    Call,
    Construct,
    TaggedTemplate,
    /// A member read whose receiver may hold an accessor or be a Proxy.
    MemberRead,
    /// A `super` property read.
    SuperProperty,
    /// A class definition's observable eager parts: decorators, static
    /// blocks, or an impure heritage, key or static initializer.
    ClassDefinition,
}

/// Why evaluation leaves the modeled straight-line path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum HavocCause {
    /// `await` or `yield`.
    Suspension,
    ThrowStatement,
    Debugger,
    /// A statement-level block, loop or conditional.
    ControlFlow,
    /// A `{ name = value }` cover-initialized property outside a pattern.
    CoverInitializedName,
    /// Syntax the transfer does not model.
    Unmodeled,
}
