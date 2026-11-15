//! Values of the abstract domain.
//!
//! One value lattice serves whole-chunk value flow, the synchronous
//! interpreter, purity's binding classes and cross-module export values. A
//! value set is a may-set: `Top` joins alongside the identities it does not
//! replace, so a consumer still sees every known allocation a site can hold.

use crate::saved_proto::{ProtoConvert, proto, required};
use analysis_model::PrimKinds;
use anyhow::Result;
use serde::{Deserialize, Serialize};

pub use crate::saved_proto::proto::Kind;

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

/// Analysis capabilities for allocation kinds.
pub trait KindProperties {
    /// Whether this allocation kind can use the engine's ordinary own-data
    /// read proof. This is a model capability, not a claim that other JavaScript
    /// objects lack ordinary own properties; their native/descriptor proofs
    /// must be handled before this default refusal.
    fn supports_ordinary_own_data_read_proof(&self) -> bool;

    /// Kinds whose application-time prefix tracks arbitrary own-property
    /// containment. This carries callback/escape references without implying
    /// that reads have ordinary-record or descriptor semantics.
    fn tracks_prefix_own_properties(&self) -> bool;

    /// Kinds whose retained references are expanded from prefix heap state.
    /// `Set` is included because its contents are tracked in a separate
    /// native lane; its property behavior does not use the arbitrary-own-
    /// property lane above.
    fn tracks_prefix_references(&self) -> bool;

    /// ECMA-262 § Keyed Collections: `Map`, `Set`, `WeakMap`, `WeakSet`.
    fn is_keyed_collection(&self) -> bool;
}

impl KindProperties for Kind {
    fn supports_ordinary_own_data_read_proof(&self) -> bool {
        *self == Self::Object
    }

    fn tracks_prefix_own_properties(&self) -> bool {
        matches!(self, Self::Object | Self::RegExp)
    }

    fn tracks_prefix_references(&self) -> bool {
        self.tracks_prefix_own_properties() || *self == Self::Set
    }

    fn is_keyed_collection(&self) -> bool {
        matches!(self, Self::Map | Self::Set | Self::WeakMap | Self::WeakSet)
    }
}

impl ProtoConvert<proto::Value> for Value {
    fn to_proto(&self) -> Result<proto::Value> {
        use proto::value::{Allocation, Value};
        Ok(proto::Value {
            value: Some(match self {
                Self::Alloc(kind, site) => Value::Allocation(Allocation {
                    kind: Some(*kind as i32),
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
                Kind::try_from(required(value.kind, "Value.allocation.kind")?)?,
                required(value.site, "Value.allocation.site")?,
            ),
            Value::PrimitiveKinds(kinds) => Self::Prim(PrimKinds::from_proto(kinds)?),
            Value::Unavailable(_) => Self::Unavailable,
            Value::Top(_) => Self::Top,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::Kind;

    #[test]
    fn generated_kind_keeps_serde_names() {
        let kinds = [
            (Kind::Function, "function"),
            (Kind::Class, "class"),
            (Kind::Object, "object"),
            (Kind::Set, "set"),
            (Kind::Array, "array"),
            (Kind::Map, "map"),
            (Kind::WeakMap, "weak_map"),
            (Kind::WeakSet, "weak_set"),
            (Kind::RegExp, "reg_exp"),
            (Kind::Registry, "registry"),
            (Kind::Proxy, "proxy"),
        ];
        for (kind, name) in kinds {
            let json = serde_json::to_string(&kind).unwrap();
            assert_eq!(json, format!("\"{name}\""));
            assert_eq!(serde_json::from_str::<Kind>(&json).unwrap(), kind);
        }
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
                    kind: None,
                    site: Some(17)
                }))
            })
            .is_err()
        );
        assert!(
            Value::from_proto(proto::Value {
                value: Some(proto::value::Value::Allocation(proto::value::Allocation {
                    kind: Some(i32::MAX),
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
