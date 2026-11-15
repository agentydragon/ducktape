//! Values of the abstract domain.
//!
//! One value lattice serves whole-chunk value flow, the synchronous
//! interpreter, purity's binding classes and cross-module export values. A
//! value set is a may-set: `Top` joins alongside the identities it does not
//! replace, so a consumer still sees every known allocation a site can hold.

use crate::saved_proto::{ProtoConvert, proto, required};
use analysis_model::PrimKinds;
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};

/// One abstract value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
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
#[serde(rename_all = "snake_case")]
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

impl ProtoConvert<i32> for Kind {
    fn to_proto(&self) -> Result<i32> {
        Ok(match self {
            Self::Function => proto::Kind::Function as i32,
            Self::Class => proto::Kind::Class as i32,
            Self::Object => proto::Kind::Object as i32,
            Self::Set => proto::Kind::Set as i32,
            Self::Array => proto::Kind::Array as i32,
            Self::Map => proto::Kind::Map as i32,
            Self::WeakMap => proto::Kind::WeakMap as i32,
            Self::WeakSet => proto::Kind::WeakSet as i32,
            Self::RegExp => proto::Kind::RegExp as i32,
            Self::Registry => proto::Kind::Registry as i32,
            Self::Proxy => proto::Kind::Proxy as i32,
        })
    }
    fn from_proto(value: i32) -> Result<Self> {
        Ok(match proto::Kind::try_from(value)? {
            proto::Kind::Function => Self::Function,
            proto::Kind::Class => Self::Class,
            proto::Kind::Object => Self::Object,
            proto::Kind::Set => Self::Set,
            proto::Kind::Array => Self::Array,
            proto::Kind::Map => Self::Map,
            proto::Kind::WeakMap => Self::WeakMap,
            proto::Kind::WeakSet => Self::WeakSet,
            proto::Kind::RegExp => Self::RegExp,
            proto::Kind::Registry => Self::Registry,
            proto::Kind::Proxy => Self::Proxy,
            proto::Kind::Unspecified => bail!("unspecified saved-analysis allocation kind"),
        })
    }
}
impl ProtoConvert<proto::Value> for Value {
    fn to_proto(&self) -> Result<proto::Value> {
        use proto::value::{Allocation, Value};
        Ok(proto::Value {
            value: Some(match self {
                Self::Alloc(kind, site) => Value::Allocation(Allocation {
                    kind: Some(kind.to_proto()?),
                    site: Some(*site),
                }),
                Self::Prim(kinds) => Value::PrimitiveKinds(kinds.to_proto()?),
                Self::Unavailable => Value::Unavailable(Default::default()),
                Self::Top => Value::Top(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::Value) -> Result<Self> {
        use proto::value::Value;
        Ok(match required(value.value, "Value.value")? {
            Value::Allocation(value) => Self::Alloc(
                Kind::from_proto(required(value.kind, "Value.allocation.kind")?)?,
                required(value.site, "Value.allocation.site")?,
            ),
            Value::PrimitiveKinds(kinds) => Self::Prim(PrimKinds::from_proto(kinds)?),
            Value::Unavailable(_) => Self::Unavailable,
            Value::Top(_) => Self::Top,
        })
    }
}

#[cfg(test)]
mod protobuf_tests {
    use super::*;

    #[test]
    fn saved_values_preserve_allocation_and_primitive_lattices() {
        for original in [
            Value::Alloc(Kind::RegExp, 17),
            Value::Prim(PrimKinds::NUMBER.union(PrimKinds::STRING)),
            Value::Unavailable,
            Value::Top,
        ] {
            assert_eq!(
                Value::from_proto(original.to_proto().unwrap()).unwrap(),
                original
            );
        }
        assert!(Value::from_proto(proto::Value { value: None }).is_err());
        assert!(
            Value::from_proto(proto::Value {
                value: Some(proto::value::Value::Allocation(proto::value::Allocation {
                    kind: Some(0),
                    site: Some(17)
                }))
            })
            .is_err()
        );
        assert!(
            Value::from_proto(proto::Value {
                value: Some(proto::value::Value::PrimitiveKinds(128))
            })
            .is_err()
        );
    }
}
