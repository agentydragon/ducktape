//! Values of the abstract domain.
//!
//! One value lattice serves whole-chunk value flow, the synchronous
//! interpreter, purity's binding classes and cross-module export values. A
//! value set is a may-set: `Top` joins alongside the identities it does not
//! replace, so a consumer still sees every known allocation a site can hold.

use analysis_model::PrimKinds;
use serde::{Deserialize, Serialize};
use source_identity::ChunkId;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_LOCAL_HEAP_SCOPE: AtomicU64 = AtomicU64::new(1);

/// Unique scope for a standalone analysis that has no whole-program chunk
/// table. It permits local overlap reasoning while preventing unrelated
/// single-chunk analyses from comparing source offsets as if they were equal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LocalAnalysisScopeId(u64);

impl LocalAnalysisScopeId {
    pub fn fresh() -> Self {
        let next = NEXT_LOCAL_HEAP_SCOPE
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value.checked_add(1)
            })
            .expect("local heap-scope identity space exhausted");
        Self(next)
    }
}

/// Namespace of source allocation coordinates. A local scope can participate
/// only in its creating analysis; cross-module transport requires `Chunk`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum HeapModuleScope {
    LocalAnalysis(LocalAnalysisScopeId),
    Chunk(ChunkId),
}

impl Serialize for HeapModuleScope {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        #[derive(Serialize)]
        #[serde(tag = "scope", content = "identity", rename_all = "snake_case")]
        enum SavedScope {
            Chunk(ChunkId),
        }

        match self {
            Self::Chunk(chunk) => SavedScope::Chunk(*chunk).serialize(serializer),
            Self::LocalAnalysis(_) => Err(serde::ser::Error::custom(
                "local-analysis heap scope cannot be serialized as a durable identity",
            )),
        }
    }
}

impl<'de> Deserialize<'de> for HeapModuleScope {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(tag = "scope", content = "identity", rename_all = "snake_case")]
        enum SavedScope {
            Chunk(ChunkId),
        }

        match SavedScope::deserialize(deserializer)? {
            SavedScope::Chunk(chunk) => Ok(Self::Chunk(chunk)),
        }
    }
}

/// Identity of one class definition. This is a source identity, not an
/// instance identity: constructor executions have a separate pooled family.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ClassDefinitionId {
    /// Source module scope that owns the class definition.
    pub module: HeapModuleScope,
    pub origin: u32,
}

/// Source identity of a callable used to produce a provider-backed allocation.
/// Consumers must independently resolve this origin against current source;
/// this identity alone grants no execution authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ProviderCallableId {
    /// Source module that defines the callable.
    pub module: HeapModuleScope,
    /// Source-relative origin of the callable definition.
    pub origin: u32,
}

/// Immutable resolver for source coordinates in one prepared analysis. Source
/// syntax and local solver tables may keep compact `u32` coordinates, but a
/// coordinate becomes a semantic heap identity only through this resolver.
/// Its scope is shared by every stage participating in the prepared analysis.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SourceHeapResolver {
    scope: HeapModuleScope,
}

impl SourceHeapResolver {
    pub const fn new(scope: HeapModuleScope) -> Self {
        Self { scope }
    }

    pub const fn scope(self) -> HeapModuleScope {
        self.scope
    }

    /// Resolve a non-class allocation source coordinate. Classes use the
    /// separate class-definition and class-object lanes below; treating a
    /// matching integer as proof of either identity is forbidden.
    pub fn allocation(self, kind: Kind, source_origin: u32) -> HeapAddress {
        assert!(kind != Kind::Class, "class origins require class_object");
        HeapAddress::AllocationFamily {
            module: self.scope,
            origin: source_origin,
            kind,
        }
    }

    /// Resolve one pooled provider output family at this consumer call site.
    /// The producer stays separately qualified from the consumer address.
    pub fn provider_allocation(
        self,
        call_origin: u32,
        producer: ProviderCallableId,
        slot: u32,
        kind: Kind,
    ) -> HeapAddress {
        assert!(kind != Kind::Class, "class origins require class_object");
        HeapAddress::ProviderAllocationFamily {
            consumer: self.scope,
            call_origin,
            producer,
            producer_birth: None,
            slot,
            kind,
        }
    }

    pub const fn class_definition(self, source_class_origin: u32) -> ClassDefinitionId {
        ClassDefinitionId {
            module: self.scope,
            origin: source_class_origin,
        }
    }

    pub const fn class_object(self, class: ClassDefinitionId) -> HeapAddress {
        HeapAddress::ClassObject { class }
    }

    pub const fn constructor_instances(self, class: ClassDefinitionId) -> HeapAddress {
        HeapAddress::ConstructorInstances { class }
    }

    /// Look up the source coordinate only when an address belongs to this
    /// exact analysis scope. This is a checked bridge back to source indexes,
    /// never a cross-scope integer-equality fallback.
    pub fn source_origin(self, address: HeapAddress) -> Option<u32> {
        match address {
            HeapAddress::AllocationFamily { module, origin, .. } if module == self.scope => {
                Some(origin)
            }
            HeapAddress::AllocationFamily { .. }
            | HeapAddress::ProviderAllocationFamily { .. }
            | HeapAddress::ClassObject { .. }
            | HeapAddress::ConstructorInstances { .. }
            | HeapAddress::GlobalObject => None,
        }
    }

    /// Project a class identity back to local class-index coordinates only
    /// when it belongs to this exact analysis scope.
    pub fn source_class_origin(self, class: ClassDefinitionId) -> Option<u32> {
        if class.module != self.scope {
            return None;
        }
        Some(class.origin)
    }
}

/// A typed, pooled heap address. Source-site families aggregate executions of
/// one allocation site; class objects and constructor instances have separate
/// identities. Every address carries its source scope, and cross-module
/// transport requires a chunk-qualified identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum HeapAddress {
    /// The one host global object shared by all linked logical modules.
    /// It is not a source allocation and has no module or source origin.
    GlobalObject,
    AllocationFamily {
        module: HeapModuleScope,
        origin: u32,
        kind: Kind,
    },
    ClassObject {
        class: ClassDefinitionId,
    },
    ConstructorInstances {
        class: ClassDefinitionId,
    },
    ProviderAllocationFamily {
        consumer: HeapModuleScope,
        call_origin: u32,
        producer: ProviderCallableId,
        /// Runtime-only proof that this provider result corresponds to one
        /// current producer allocation site. Only attach it after the
        /// provider registry validates the producer-module source coordinate;
        /// wire and saved forms omit it.
        producer_birth: Option<u32>,
        slot: u32,
        kind: Kind,
    },
}

impl Serialize for HeapAddress {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        #[derive(Serialize)]
        #[serde(tag = "family", rename_all = "snake_case")]
        enum WireAddress {
            GlobalObject,
            AllocationFamily {
                module: HeapModuleScope,
                origin: u32,
                kind: Kind,
            },
            ClassObject {
                class: ClassDefinitionId,
            },
            ConstructorInstances {
                class: ClassDefinitionId,
            },
            ProviderAllocationFamily {
                consumer: HeapModuleScope,
                call_origin: u32,
                producer: ProviderCallableId,
                slot: u32,
                kind: Kind,
            },
        }

        match *self {
            Self::GlobalObject => WireAddress::GlobalObject.serialize(serializer),
            Self::AllocationFamily {
                kind: Kind::Class, ..
            } => Err(<S::Error as serde::ser::Error>::custom(
                "class definitions cannot use allocation-family addresses",
            )),
            Self::AllocationFamily {
                module,
                origin,
                kind,
            } => WireAddress::AllocationFamily {
                module,
                origin,
                kind,
            }
            .serialize(serializer),
            Self::ClassObject { class } => WireAddress::ClassObject { class }.serialize(serializer),
            Self::ConstructorInstances { class } => {
                WireAddress::ConstructorInstances { class }.serialize(serializer)
            }
            Self::ProviderAllocationFamily {
                kind: Kind::Class, ..
            } => Err(<S::Error as serde::ser::Error>::custom(
                "class definitions cannot use provider allocation-family addresses",
            )),
            Self::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                slot,
                kind,
                ..
            } => WireAddress::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                slot,
                kind,
            }
            .serialize(serializer),
        }
    }
}

impl<'de> Deserialize<'de> for HeapAddress {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(tag = "family", rename_all = "snake_case")]
        enum WireAddress {
            GlobalObject,
            AllocationFamily {
                module: HeapModuleScope,
                origin: u32,
                kind: Kind,
            },
            ClassObject {
                class: ClassDefinitionId,
            },
            ConstructorInstances {
                class: ClassDefinitionId,
            },
            ProviderAllocationFamily {
                consumer: HeapModuleScope,
                call_origin: u32,
                producer: ProviderCallableId,
                slot: u32,
                kind: Kind,
            },
        }

        match WireAddress::deserialize(deserializer)? {
            WireAddress::GlobalObject => Ok(Self::GlobalObject),
            WireAddress::AllocationFamily {
                kind: Kind::Class, ..
            } => Err(<D::Error as serde::de::Error>::custom(
                "class definitions cannot use allocation-family addresses",
            )),
            WireAddress::AllocationFamily {
                module,
                origin,
                kind,
            } => Ok(Self::AllocationFamily {
                module,
                origin,
                kind,
            }),
            WireAddress::ClassObject { class } => Ok(Self::ClassObject { class }),
            WireAddress::ConstructorInstances { class } => Ok(Self::ConstructorInstances { class }),
            WireAddress::ProviderAllocationFamily {
                kind: Kind::Class, ..
            } => Err(<D::Error as serde::de::Error>::custom(
                "class definitions cannot use provider allocation-family addresses",
            )),
            WireAddress::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                slot,
                kind,
            } => Ok(Self::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                producer_birth: None,
                slot,
                kind,
            }),
        }
    }
}

impl HeapAddress {
    /// A source family in a self-contained per-chunk analysis.
    pub const fn local_family(scope: LocalAnalysisScopeId, kind: Kind, origin: u32) -> Self {
        Self::in_scope(HeapModuleScope::LocalAnalysis(scope), kind, origin)
    }

    /// A source family in any explicitly carried analysis scope.
    pub const fn in_scope(module: HeapModuleScope, kind: Kind, origin: u32) -> Self {
        assert!(
            !matches!(kind, Kind::Class),
            "class origins require an explicit class identity"
        );
        Self::AllocationFamily {
            module,
            origin,
            kind,
        }
    }

    /// A source family in a prepared whole-program chunk.
    pub const fn module_family(module: ChunkId, kind: Kind, origin: u32) -> Self {
        Self::in_scope(HeapModuleScope::Chunk(module), kind, origin)
    }

    pub const fn constructor_instances(class: ClassDefinitionId) -> Self {
        Self::ConstructorInstances { class }
    }

    pub const fn class_definition(module: HeapModuleScope, origin: u32) -> ClassDefinitionId {
        ClassDefinitionId { module, origin }
    }

    /// Qualify a local family at the boundary that knows both its local scope
    /// and source chunk. An already-qualified address can only be reused for
    /// that same chunk; callers cannot silently rename a foreign source.
    pub fn qualify(self, local_scope: LocalAnalysisScopeId, module: ChunkId) -> Option<Self> {
        match self {
            Self::GlobalObject => Some(Self::GlobalObject),
            Self::AllocationFamily {
                module: HeapModuleScope::LocalAnalysis(actual_scope),
                origin,
                kind,
            } if actual_scope == local_scope => Some(Self::module_family(module, kind, origin)),
            Self::ConstructorInstances { class }
                if class.module == HeapModuleScope::LocalAnalysis(local_scope) =>
            {
                Some(Self::constructor_instances(ClassDefinitionId {
                    module: HeapModuleScope::Chunk(module),
                    origin: class.origin,
                }))
            }
            Self::ClassObject { class }
                if class.module == HeapModuleScope::LocalAnalysis(local_scope) =>
            {
                Some(Self::ClassObject {
                    class: ClassDefinitionId {
                        module: HeapModuleScope::Chunk(module),
                        origin: class.origin,
                    },
                })
            }
            Self::ProviderAllocationFamily {
                consumer: HeapModuleScope::LocalAnalysis(actual_scope),
                call_origin,
                producer:
                    ProviderCallableId {
                        module: HeapModuleScope::Chunk(producer_module),
                        origin: producer_origin,
                    },
                producer_birth,
                slot,
                kind,
            } if actual_scope == local_scope => Some(Self::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(module),
                call_origin,
                producer: ProviderCallableId {
                    module: HeapModuleScope::Chunk(producer_module),
                    origin: producer_origin,
                },
                producer_birth,
                slot,
                kind,
            }),
            Self::AllocationFamily {
                module: HeapModuleScope::Chunk(existing),
                ..
            }
            | Self::ConstructorInstances {
                class:
                    ClassDefinitionId {
                        module: HeapModuleScope::Chunk(existing),
                        ..
                    },
            } if existing == module => Some(self),
            Self::ClassObject {
                class:
                    ClassDefinitionId {
                        module: HeapModuleScope::Chunk(existing),
                        ..
                    },
            } if existing == module => Some(self),
            Self::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(existing),
                producer:
                    ProviderCallableId {
                        module: HeapModuleScope::Chunk(_),
                        ..
                    },
                ..
            } if existing == module => Some(self),
            _ => None,
        }
    }

    pub fn is_module_qualified(self) -> bool {
        match self {
            Self::GlobalObject => false,
            Self::AllocationFamily { module, .. } => matches!(module, HeapModuleScope::Chunk(_)),
            Self::ConstructorInstances { class } => {
                matches!(class.module, HeapModuleScope::Chunk(_))
            }
            Self::ClassObject { class } => matches!(class.module, HeapModuleScope::Chunk(_)),
            Self::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(_),
                producer:
                    ProviderCallableId {
                        module: HeapModuleScope::Chunk(_),
                        ..
                    },
                ..
            } => true,
            Self::ProviderAllocationFamily { .. } => false,
        }
    }

    pub const fn module(self) -> Option<HeapModuleScope> {
        match self {
            Self::GlobalObject => None,
            Self::AllocationFamily { module, .. } => Some(module),
            Self::ConstructorInstances { class } => Some(class.module),
            Self::ClassObject { class } => Some(class.module),
            Self::ProviderAllocationFamily { consumer, .. } => Some(consumer),
        }
    }

    pub const fn source_origin(self) -> Option<u32> {
        match self {
            Self::GlobalObject => None,
            Self::AllocationFamily { origin, .. } => Some(origin),
            Self::ConstructorInstances { class } => Some(class.origin),
            Self::ClassObject { class } => Some(class.origin),
            Self::ProviderAllocationFamily { .. } => None,
        }
    }

    pub const fn kind(self) -> Option<Kind> {
        match self {
            Self::GlobalObject => None,
            Self::AllocationFamily { kind, .. } => Some(kind),
            Self::ClassObject { .. } => Some(Kind::Class),
            Self::ConstructorInstances { .. } => Some(Kind::Object),
            Self::ProviderAllocationFamily { kind, .. } => Some(kind),
        }
    }

    pub const fn class_definition_id(self) -> Option<ClassDefinitionId> {
        match self {
            Self::ClassObject { class } | Self::ConstructorInstances { class } => Some(class),
            Self::AllocationFamily { .. }
            | Self::ProviderAllocationFamily { .. }
            | Self::GlobalObject => None,
        }
    }

    /// Attach the producer-module `AllocationFamily.origin` after the current
    /// provider registry has verified the returned slot against its source
    /// program. This runtime proof is intentionally absent from wire/saved
    /// forms.
    pub(crate) fn with_verified_producer_birth(self, birth_origin: u32) -> Self {
        match self {
            Self::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                producer_birth: None,
                slot,
                kind,
            } => Self::ProviderAllocationFamily {
                consumer,
                call_origin,
                producer,
                producer_birth: Some(birth_origin),
                slot,
                kind,
            },
            Self::ProviderAllocationFamily {
                producer_birth: Some(_),
                ..
            } => panic!("provider producer-birth proof may only be attached once"),
            _ => panic!("producer-birth proof requires a provider allocation address"),
        }
    }

    /// Conservative aliasing for pooled provider output families. A callable
    /// origin and formal slot are descriptive identities, not proof that two
    /// runtime object families are disjoint. Without a birth witness, every
    /// family with the same producer module and kind may alias. Two verified
    /// witnesses refine that relation only by producer allocation origin.
    pub(crate) fn may_overlap_with_provider_family(self, other: Self) -> bool {
        if self == other {
            return true;
        }
        match (self, other) {
            (
                Self::ProviderAllocationFamily {
                    producer: left,
                    producer_birth: left_birth,
                    kind: left_kind,
                    ..
                },
                Self::ProviderAllocationFamily {
                    producer: right,
                    producer_birth: right_birth,
                    kind: right_kind,
                    ..
                },
            ) => {
                left.module == right.module
                    && left_kind == right_kind
                    && match (left_birth, right_birth) {
                        (Some(left), Some(right)) => left == right,
                        _ => true,
                    }
            }
            (
                Self::ProviderAllocationFamily {
                    producer,
                    producer_birth,
                    kind: provider_kind,
                    ..
                },
                Self::AllocationFamily {
                    module,
                    origin,
                    kind: source_kind,
                    ..
                },
            )
            | (
                Self::AllocationFamily {
                    module,
                    origin,
                    kind: source_kind,
                    ..
                },
                Self::ProviderAllocationFamily {
                    producer,
                    producer_birth,
                    kind: provider_kind,
                    ..
                },
            ) => {
                producer.module == module
                    && provider_kind == source_kind
                    && producer_birth.is_none_or(|birth| birth == origin)
            }
            _ => false,
        }
    }
}

/// One abstract value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Value {
    /// The runtime object(s) one source allocation site creates, keyed by the
    /// site's start offset. Multiplicity is not tracked: one site may stand
    /// for several runtime objects.
    Alloc(Kind, HeapAddress),
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

impl Serialize for Value {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        #[derive(Serialize)]
        enum WireValue {
            Alloc(Kind, HeapAddress),
            Prim(PrimKinds),
            Unavailable,
            Top,
        }

        match *self {
            Self::Alloc(kind, address) if Some(kind) != address.kind() => {
                Err(<S::Error as serde::ser::Error>::custom(
                    "allocation value kind must match its typed heap address",
                ))
            }
            Self::Alloc(kind, address) => WireValue::Alloc(kind, address).serialize(serializer),
            Self::Prim(kinds) => WireValue::Prim(kinds).serialize(serializer),
            Self::Unavailable => WireValue::Unavailable.serialize(serializer),
            Self::Top => WireValue::Top.serialize(serializer),
        }
    }
}

impl<'de> Deserialize<'de> for Value {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        enum WireValue {
            Alloc(Kind, HeapAddress),
            Prim(PrimKinds),
            Unavailable,
            Top,
        }

        match WireValue::deserialize(deserializer)? {
            WireValue::Alloc(kind, address) if Some(kind) != address.kind() => {
                Err(<D::Error as serde::de::Error>::custom(
                    "allocation value kind must match its typed heap address",
                ))
            }
            WireValue::Alloc(kind, address) => Ok(Self::Alloc(kind, address)),
            WireValue::Prim(kinds) => Ok(Self::Prim(kinds)),
            WireValue::Unavailable => Ok(Self::Unavailable),
            WireValue::Top => Ok(Self::Top),
        }
    }
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

#[cfg(test)]
mod provider_allocation_tests {
    use super::*;

    #[test]
    fn provider_identity_keeps_foreign_source_coordinates_out_of_consumer_scope() {
        let local_scope = LocalAnalysisScopeId::fresh();
        let local_module = HeapModuleScope::LocalAnalysis(local_scope);
        let producer_module = HeapModuleScope::Chunk(ChunkId(4));
        let resolver = SourceHeapResolver::new(local_module);
        let producer = ProviderCallableId {
            module: producer_module,
            origin: 17,
        };
        let recipe = resolver.provider_allocation(29, producer, 2, Kind::Object);

        assert_eq!(recipe.module(), Some(local_module));
        assert_eq!(recipe.source_origin(), None);
        assert_eq!(resolver.source_origin(recipe), None);
        assert_eq!(recipe.kind(), Some(Kind::Object));
        assert!(!recipe.is_module_qualified());
        let qualified = recipe
            .qualify(local_scope, ChunkId(9))
            .expect("only the known consumer should be qualified");
        assert_eq!(
            qualified,
            HeapAddress::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(ChunkId(9)),
                call_origin: 29,
                producer,
                producer_birth: None,
                slot: 2,
                kind: Kind::Object,
            }
        );
        assert!(qualified.is_module_qualified());
        let qualified_verified = resolver
            .provider_allocation(29, producer, 2, Kind::Object)
            .with_verified_producer_birth(41)
            .qualify(local_scope, ChunkId(9))
            .expect("qualification preserves current producer verification");
        assert_eq!(
            qualified_verified,
            HeapAddress::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(ChunkId(9)),
                call_origin: 29,
                producer,
                producer_birth: Some(41),
                slot: 2,
                kind: Kind::Object,
            }
        );

        let unresolved_producer = HeapAddress::ProviderAllocationFamily {
            consumer: local_module,
            call_origin: 29,
            producer: ProviderCallableId {
                module: local_module,
                origin: 17,
            },
            producer_birth: None,
            slot: 2,
            kind: Kind::Object,
        };
        assert!(
            unresolved_producer
                .qualify(local_scope, ChunkId(9))
                .is_none(),
            "consumer qualification cannot invent a producer module"
        );
        let chunk_consumer_local_producer = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(9)),
            call_origin: 29,
            producer: ProviderCallableId {
                module: local_module,
                origin: 17,
            },
            producer_birth: None,
            slot: 2,
            kind: Kind::Object,
        };
        assert!(
            chunk_consumer_local_producer
                .qualify(local_scope, ChunkId(9))
                .is_none(),
            "a chunk consumer cannot silently rebind a local producer"
        );
    }

    #[test]
    fn verified_provider_birth_refines_overlap_without_using_recipe_labels() {
        let consumer = HeapModuleScope::Chunk(ChunkId(9));
        let producer_module = HeapModuleScope::Chunk(ChunkId(4));
        let first = SourceHeapResolver::new(consumer)
            .provider_allocation(
                31,
                ProviderCallableId {
                    module: producer_module,
                    origin: 10,
                },
                0,
                Kind::Object,
            )
            .with_verified_producer_birth(42);
        let same_birth_different_labels =
            SourceHeapResolver::new(HeapModuleScope::Chunk(ChunkId(12)))
                .provider_allocation(
                    81,
                    ProviderCallableId {
                        module: producer_module,
                        origin: 20,
                    },
                    3,
                    Kind::Object,
                )
                .with_verified_producer_birth(42);
        let different_birth = SourceHeapResolver::new(consumer)
            .provider_allocation(
                31,
                ProviderCallableId {
                    module: producer_module,
                    origin: 10,
                },
                0,
                Kind::Object,
            )
            .with_verified_producer_birth(43);
        let unverified = SourceHeapResolver::new(consumer).provider_allocation(
            31,
            ProviderCallableId {
                module: producer_module,
                origin: 10,
            },
            0,
            Kind::Object,
        );

        assert!(first.may_overlap_with_provider_family(same_birth_different_labels));
        assert!(!first.may_overlap_with_provider_family(different_birth));
        assert!(first.may_overlap_with_provider_family(unverified));
        assert!(
            first.may_overlap_with_provider_family(HeapAddress::module_family(
                source_identity::ChunkId(4),
                Kind::Object,
                42
            ))
        );
        assert!(
            !first.may_overlap_with_provider_family(HeapAddress::module_family(
                source_identity::ChunkId(4),
                Kind::Object,
                43
            ))
        );
        assert!(
            !first.may_overlap_with_provider_family(HeapAddress::module_family(
                source_identity::ChunkId(5),
                Kind::Object,
                42
            ))
        );
        assert!(
            !first.may_overlap_with_provider_family(HeapAddress::module_family(
                source_identity::ChunkId(4),
                Kind::Array,
                42
            ))
        );
    }
}
