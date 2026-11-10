//! Values of the abstract domain.
//!
//! One value lattice serves whole-chunk value flow, the synchronous
//! interpreter, purity's binding classes and cross-module export values. A
//! value set is a may-set: `Top` joins alongside the identities it does not
//! replace, so a consumer still sees every known allocation a site can hold.

use analysis_model::PrimKinds;
use serde::{Deserialize, Serialize};

/// One abstract value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
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
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
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
    /// Whether this allocation kind can use the engine's ordinary own-data
    /// read proof. This is a model capability, not a claim that other JavaScript
    /// objects lack ordinary own properties; their native/descriptor proofs
    /// must be handled before this default refusal.
    pub fn supports_ordinary_own_data_read_proof(self) -> bool {
        self == Self::Object
    }

    /// Kinds whose application-time prefix tracks arbitrary own-property
    /// containment. This carries callback/escape references without implying
    /// that reads have ordinary-record or descriptor semantics.
    pub fn tracks_prefix_own_properties(self) -> bool {
        matches!(self, Self::Object | Self::RegExp)
    }

    /// Kinds whose retained references are expanded from prefix heap state.
    /// `Set` is included because its contents are tracked in a separate
    /// native lane; its property behavior does not use the arbitrary-own-
    /// property lane above.
    pub fn tracks_prefix_references(self) -> bool {
        self.tracks_prefix_own_properties() || self == Self::Set
    }

    /// ECMA-262 § Keyed Collections: `Map`, `Set`, `WeakMap`, `WeakSet`.
    pub fn is_keyed_collection(self) -> bool {
        matches!(self, Self::Map | Self::Set | Self::WeakMap | Self::WeakSet)
    }
}
