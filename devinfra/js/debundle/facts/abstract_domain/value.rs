//! Value kinds of the abstract domain.

/// Kind of a native allocation. Only kinds a builtin row allocates are defined.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Object,
    Array,
    Map,
    Set,
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
