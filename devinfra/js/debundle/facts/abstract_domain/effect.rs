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
}
