//! Values of the abstract domain.
//!
//! One value lattice serves whole-chunk value flow, the synchronous
//! interpreter, purity's binding classes and cross-module export values. A
//! value set is a may-set: `Top` joins alongside the identities it does not
//! replace, so a consumer still sees every known allocation a site can hold.

use std::fmt;

/// One abstract value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Value {
    /// The runtime object(s) one source allocation site creates, keyed by the
    /// site's start offset. Multiplicity is not tracked: one site may stand
    /// for several runtime objects.
    Alloc(Kind, u32),
    /// A primitive whose type is one of the set. A primitive has no own
    /// accessors and reaches no user code through `ToPrimitive` or
    /// `ToPropertyKey`.
    Prim(PrimKinds),
    /// Evaluation throws before producing a value (a lexical binding read in
    /// its temporal dead zone).
    Unavailable,
    /// Any value, including objects with user-defined hooks.
    Top,
}

/// Kind of an allocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Kind {
    /// A function, arrow or method body.
    Function,
    Class,
    /// An object, array or regular-expression literal: value flow gives them
    /// one identity lane. Builtin rows allocate `Object`, `Array` and `RegExp`
    /// by their own kinds.
    Object,
    /// A native `Set`.
    Set,
    Array,
    Map,
    WeakMap,
    WeakSet,
    RegExp,
    /// A `FinalizationRegistry`.
    Registry,
    Proxy,
}

impl Kind {
    /// ECMA-262 § Keyed Collections: `Map`, `Set`, `WeakMap`, `WeakSet`.
    pub fn is_keyed_collection(self) -> bool {
        matches!(self, Self::Map | Self::Set | Self::WeakMap | Self::WeakSet)
    }
}

/// A set of ECMAScript primitive types; primitive values join by union. The
/// empty set is the result of an operation that always throws.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PrimKinds(u8);

impl PrimKinds {
    pub const NONE: Self = Self(0);
    pub const UNDEFINED: Self = Self(1);
    pub const NULL: Self = Self(1 << 1);
    pub const BOOLEAN: Self = Self(1 << 2);
    pub const NUMBER: Self = Self(1 << 3);
    pub const BIGINT: Self = Self(1 << 4);
    pub const STRING: Self = Self(1 << 5);
    pub const SYMBOL: Self = Self(1 << 6);
    pub const ANY: Self = Self((1 << 7) - 1);

    pub const fn union(self, other: Self) -> Self {
        Self(self.0 | other.0)
    }

    pub const fn without(self, other: Self) -> Self {
        Self(self.0 & !other.0)
    }

    pub const fn is_empty(self) -> bool {
        self.0 == 0
    }

    pub const fn intersects(self, other: Self) -> bool {
        self.0 & other.0 != 0
    }

    /// Nonempty and within `other`: evaluation can complete, and when it
    /// does the value has one of `other`'s types.
    pub const fn is_only(self, other: Self) -> bool {
        self.0 != 0 && self.0 & !other.0 == 0
    }
}

impl fmt::Debug for PrimKinds {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        const NAMES: [&str; 7] = [
            "undefined",
            "null",
            "boolean",
            "number",
            "bigint",
            "string",
            "symbol",
        ];
        if *self == Self::ANY {
            return f.write_str("primitive");
        }
        let mut names = NAMES
            .iter()
            .enumerate()
            .filter(|(bit, _)| self.0 & (1 << bit) != 0)
            .map(|(_, name)| *name);
        f.write_str(names.next().unwrap_or("never"))?;
        names.try_for_each(|name| write!(f, "|{name}"))
    }
}
