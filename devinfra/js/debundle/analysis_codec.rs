//! Typed, process-independent analysis artifacts, separate from report Serde.
//!
//! Records include all fields; only `SyntaxContext` maps through parser ordinals.
//! An explicit context preserves shared `Arc` values within one artifact. Reports
//! keep their own Serde attributes and never observe an artifact's context.

use std::any::{Any, TypeId};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt;
use std::hash::{BuildHasher, Hash};
use std::marker::PhantomData;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::ser::{SerializeMap, SerializeSeq, SerializeTuple};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use swc_common::SyntaxContext;
use swc_ecma_ast::Module;
use swc_ecma_visit::{Visit, VisitWith};

/// First-occurrence hygiene-context ordinals across the analyzed modules.
pub struct ContextOrdinals {
    ordinals: HashMap<SyntaxContext, u32>,
    contexts: Vec<SyntaxContext>,
}

impl ContextOrdinals {
    pub fn new<'m>(modules: impl IntoIterator<Item = &'m Module>) -> Self {
        let mut result = Self {
            ordinals: HashMap::from([(SyntaxContext::empty(), 0)]),
            contexts: vec![SyntaxContext::empty()],
        };
        for module in modules {
            module.visit_with(&mut result);
        }
        result
    }
}

impl Visit for ContextOrdinals {
    fn visit_syntax_context(&mut self, context: &SyntaxContext) {
        if !self.ordinals.contains_key(context) {
            let ordinal = u32::try_from(self.contexts.len()).expect("fewer than 2^32 contexts");
            self.ordinals.insert(*context, ordinal);
            self.contexts.push(*context);
        }
    }
}

pub struct ArtifactContext<'a> {
    ordinals: &'a ContextOrdinals,
    written: RefCell<HashMap<(TypeId, *const ()), u32>>,
    read: RefCell<HashMap<u32, Arc<dyn Any + Send + Sync>>>,
}

impl<'a> ArtifactContext<'a> {
    fn new(ordinals: &'a ContextOrdinals) -> Self {
        Self {
            ordinals,
            written: RefCell::default(),
            read: RefCell::default(),
        }
    }
}

/// Snapshot encoding requires explicit implementations for every stored type.
/// The derive uses tuple records independent of report names, tags, and skips.
pub trait Artifact: Sized {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error>;
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error>;
}

/// Generated record fields; callers normally derive `Artifact` instead.
pub trait ArtifactRecord: Sized {
    fn tuple_len(&self) -> usize;
    fn write_fields<S: SerializeTuple>(
        &self,
        tuple: &mut S,
        context: &ArtifactContext<'_>,
    ) -> Result<(), S::Error>;
    fn read_fields<'de, A: SeqAccess<'de>>(
        sequence: &mut A,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, A::Error>;
}

pub struct Encoded<'a, 'o, T> {
    value: &'a T,
    context: &'a ArtifactContext<'o>,
}

impl<'a, 'o, T> Encoded<'a, 'o, T> {
    pub fn new(value: &'a T, context: &'a ArtifactContext<'o>) -> Self {
        Self { value, context }
    }
}

impl<T: Artifact> Serialize for Encoded<'_, '_, T> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.value.encode(serializer, self.context)
    }
}

struct Seed<'a, 'o, T> {
    context: &'a ArtifactContext<'o>,
    marker: PhantomData<fn() -> T>,
}

impl<'a, 'o, T> Seed<'a, 'o, T> {
    fn new(context: &'a ArtifactContext<'o>) -> Self {
        Self {
            context,
            marker: PhantomData,
        }
    }
}

impl<'de, T: Artifact> DeserializeSeed<'de> for Seed<'_, '_, T> {
    type Value = T;
    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<T, D::Error> {
        T::decode(deserializer, self.context)
    }
}

pub fn encode<T: Artifact>(value: &T, ordinals: &ContextOrdinals) -> Result<Vec<u8>> {
    let context = ArtifactContext::new(ordinals);
    let mut encoder = zstd::Encoder::new(Vec::new(), 3)?;
    value.encode(&mut rmp_serde::Serializer::new(&mut encoder), &context)?;
    Ok(encoder.finish()?)
}

pub fn decode<T: Artifact>(bytes: &[u8], ordinals: &ContextOrdinals) -> Result<T> {
    let context = ArtifactContext::new(ordinals);
    let mut reader = zstd::Decoder::new(bytes).context("decompress analysis output")?;
    let value = T::decode(&mut rmp_serde::Deserializer::new(&mut reader), &context)?;
    // Reach the frame checksum even when the deserializer stops at its value.
    std::io::copy(&mut reader, &mut std::io::sink()).context("decompress analysis output")?;
    Ok(value)
}

pub fn encode_record<T: ArtifactRecord, S: Serializer>(
    value: &T,
    serializer: S,
    context: &ArtifactContext<'_>,
) -> Result<S::Ok, S::Error> {
    let mut tuple = serializer.serialize_tuple(value.tuple_len())?;
    value.write_fields(&mut tuple, context)?;
    tuple.end()
}

pub fn read_field<'de, T: Artifact, A: SeqAccess<'de>>(
    sequence: &mut A,
    context: &ArtifactContext<'_>,
    index: usize,
) -> Result<T, A::Error> {
    sequence
        .next_element_seed(Seed::new(context))?
        .ok_or_else(|| de::Error::invalid_length(index, &"an artifact record"))
}

pub fn read_named_field<'de, T: Artifact, A: SeqAccess<'de>>(
    sequence: &mut A,
    context: &ArtifactContext<'_>,
    index: usize,
    name: &str,
) -> Result<T, A::Error> {
    sequence
        .next_element_seed(Seed::new(context))?
        .ok_or_else(|| de::Error::custom(format!("missing artifact field {name} at index {index}")))
}
pub fn decode_record<'de, T: ArtifactRecord, D: Deserializer<'de>>(
    deserializer: D,
    context: &ArtifactContext<'_>,
) -> Result<T, D::Error> {
    struct Record<'a, 'o, T>(Seed<'a, 'o, T>);
    impl<'de, T: ArtifactRecord> Visitor<'de> for Record<'_, '_, T> {
        type Value = T;
        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("an artifact record")
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<T, A::Error> {
            let value = T::read_fields(&mut sequence, self.0.context)?;
            if sequence.next_element::<de::IgnoredAny>()?.is_some() {
                return Err(de::Error::custom("extra artifact record fields"));
            }
            Ok(value)
        }
    }
    deserializer.deserialize_seq(Record(Seed::new(context)))
}

/// Leaf types whose complete Serde representation has no process-local state.
#[macro_export]
macro_rules! plain_artifact {
    ($($ty:ty),* $(,)?) => { $(
        impl $crate::Artifact for $ty {
            fn encode<S: ::serde::Serializer>(&self, serializer: S, _: &$crate::ArtifactContext<'_>) -> Result<S::Ok, S::Error> {
                ::serde::Serialize::serialize(self, serializer)
            }
            fn decode<'de, D: ::serde::Deserializer<'de>>(deserializer: D, _: &$crate::ArtifactContext<'_>) -> Result<Self, D::Error> {
                ::serde::Deserialize::deserialize(deserializer)
            }
        }
    )* };
}

plain_artifact!(
    (),
    bool,
    char,
    u8,
    u16,
    u32,
    u64,
    u128,
    usize,
    i8,
    i16,
    i32,
    i64,
    i128,
    isize,
    f32,
    f64,
    String,
    std::path::PathBuf,
    swc_atoms::Atom,
    swc_common::Span,
    source_identity::StatementId,
    source_identity::SourceUnitId,
    source_identity::SourceUnitOrdinal
);

impl Artifact for SyntaxContext {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        let ordinal = context.ordinals.ordinals.get(self).ok_or_else(|| {
            serde::ser::Error::custom("binding context is absent from the analyzed modules")
        })?;
        ordinal.serialize(serializer)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        let ordinal = u32::deserialize(deserializer)?;
        context
            .ordinals
            .contexts
            .get(ordinal as usize)
            .copied()
            .ok_or_else(|| {
                de::Error::custom("binding context ordinal is absent from the analyzed modules")
            })
    }
}

impl<T: Artifact> Artifact for Option<T> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        match self {
            None => serializer.serialize_none(),
            Some(value) => serializer.serialize_some(&Encoded::new(value, context)),
        }
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        struct Optional<'a, 'o, T>(Seed<'a, 'o, T>);
        impl<'de, T: Artifact> Visitor<'de> for Optional<'_, '_, T> {
            type Value = Option<T>;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("an optional artifact value")
            }
            fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(None)
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(None)
            }
            fn visit_some<D: Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
                T::decode(d, self.0.context).map(Some)
            }
        }
        deserializer.deserialize_option(Optional(Seed::new(context)))
    }
}

impl<T: Artifact> Artifact for Vec<T> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_sequence(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        decode_sequence(deserializer, context)
    }
}

fn decode_sequence<'de, T: Artifact, C: Default + Extend<T>, D: Deserializer<'de>>(
    deserializer: D,
    context: &ArtifactContext<'_>,
) -> Result<C, D::Error> {
    struct Sequence<'a, 'o, T, C>(Seed<'a, 'o, T>, PhantomData<C>);
    impl<'de, T: Artifact, C: Default + Extend<T>> Visitor<'de> for Sequence<'_, '_, T, C> {
        type Value = C;
        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("an artifact sequence")
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<C, A::Error> {
            let mut result = C::default();
            while let Some(value) = sequence.next_element_seed(Seed::new(self.0.context))? {
                result.extend(std::iter::once(value));
            }
            Ok(result)
        }
    }
    deserializer.deserialize_seq(Sequence(Seed::<T>::new(context), PhantomData))
}

fn encode_sequence<'a, T: Artifact + 'a, S: Serializer>(
    values: impl ExactSizeIterator<Item = &'a T>,
    serializer: S,
    context: &ArtifactContext<'_>,
) -> Result<S::Ok, S::Error> {
    let mut sequence = serializer.serialize_seq(Some(values.len()))?;
    for value in values {
        sequence.serialize_element(&Encoded::new(value, context))?;
    }
    sequence.end()
}

impl<T: Artifact + Ord> Artifact for BTreeSet<T> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_sequence(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        decode_sequence(deserializer, context)
    }
}

impl<T: Artifact + Eq + Hash, H: BuildHasher + Default> Artifact for HashSet<T, H> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_sequence(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        decode_sequence(deserializer, context)
    }
}

fn decode_map<'de, K: Artifact, V: Artifact, C: Default + Extend<(K, V)>, D: Deserializer<'de>>(
    deserializer: D,
    context: &ArtifactContext<'_>,
) -> Result<C, D::Error> {
    struct Map<'a, 'o, K, V, C>(Seed<'a, 'o, (K, V)>, PhantomData<C>);
    impl<'de, K: Artifact, V: Artifact, C: Default + Extend<(K, V)>> Visitor<'de>
        for Map<'_, '_, K, V, C>
    {
        type Value = C;
        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("an artifact map")
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<C, A::Error> {
            let mut result = C::default();
            while let Some(key) = map.next_key_seed(Seed::new(self.0.context))? {
                let value = map.next_value_seed(Seed::new(self.0.context))?;
                result.extend(std::iter::once((key, value)));
            }
            Ok(result)
        }
    }
    deserializer.deserialize_map(Map(Seed::<(K, V)>::new(context), PhantomData))
}

fn encode_map<'a, K: Artifact + 'a, V: Artifact + 'a, S: Serializer>(
    values: impl ExactSizeIterator<Item = (&'a K, &'a V)>,
    serializer: S,
    context: &ArtifactContext<'_>,
) -> Result<S::Ok, S::Error> {
    let mut map = serializer.serialize_map(Some(values.len()))?;
    for (key, value) in values {
        map.serialize_entry(&Encoded::new(key, context), &Encoded::new(value, context))?;
    }
    map.end()
}

impl<K: Artifact + Ord, V: Artifact> Artifact for BTreeMap<K, V> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_map(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        decode_map(deserializer, context)
    }
}

impl<K: Artifact + Eq + Hash, V: Artifact, H: BuildHasher + Default> Artifact for HashMap<K, V, H> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_map(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        decode_map(deserializer, context)
    }
}

impl<T: Artifact> Artifact for Box<T> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        (**self).encode(serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        T::decode(deserializer, context).map(Box::new)
    }
}

impl<T: Artifact + Any + Send + Sync> Artifact for Arc<T> {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        let key = (TypeId::of::<T>(), Arc::as_ptr(self).cast::<()>());
        let (index, first) = {
            let mut written = context.written.borrow_mut();
            match written.get(&key) {
                Some(index) => (*index, false),
                None => {
                    let index = u32::try_from(written.len()).map_err(serde::ser::Error::custom)?;
                    written.insert(key, index);
                    (index, true)
                }
            }
        };
        let mut tuple = serializer.serialize_tuple(2)?;
        tuple.serialize_element(&index)?;
        tuple.serialize_element(&first.then(|| Encoded::new(self.as_ref(), context)))?;
        tuple.end()
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        let (index, value) = <(u32, Option<T>)>::decode(deserializer, context)?;
        if let Some(value) = value {
            let value = Arc::new(value);
            if context
                .read
                .borrow_mut()
                .insert(index, value.clone())
                .is_some()
            {
                return Err(de::Error::custom("duplicate artifact shared-value index"));
            }
            return Ok(value);
        }
        context
            .read
            .borrow()
            .get(&index)
            .cloned()
            .ok_or_else(|| de::Error::custom("artifact shared value precedes its definition"))?
            .downcast::<T>()
            .map_err(|_| de::Error::custom("artifact shared-value type mismatch"))
    }
}

macro_rules! tuple_artifact {
    ($($name:ident:$index:tt),+) => {
        impl<$($name: Artifact),+> ArtifactRecord for ($($name,)+) {
            fn tuple_len(&self) -> usize { [$(stringify!($name)),+].len() }
            fn write_fields<S: SerializeTuple>(&self, tuple: &mut S, context: &ArtifactContext<'_>) -> Result<(), S::Error> {
                $(tuple.serialize_element(&Encoded::new(&self.$index, context))?;)+
                Ok(())
            }
            fn read_fields<'de, A: SeqAccess<'de>>(sequence: &mut A, context: &ArtifactContext<'_>) -> Result<Self, A::Error> { Ok(($(read_field::<$name, _>(sequence, context, $index)?,)+)) }
        }
        impl<$($name: Artifact),+> Artifact for ($($name,)+) {
            fn encode<S: Serializer>(&self, serializer: S, context: &ArtifactContext<'_>) -> Result<S::Ok, S::Error> { encode_record(self, serializer, context) }
            fn decode<'de, D: Deserializer<'de>>(deserializer: D, context: &ArtifactContext<'_>) -> Result<Self, D::Error> { decode_record(deserializer, context) }
        }
    };
}

tuple_artifact!(T0:0);
tuple_artifact!(T0:0, T1:1);
tuple_artifact!(T0:0, T1:1, T2:2);
tuple_artifact!(T0:0, T1:1, T2:2, T3:3);

impl<T: Artifact, const N: usize> Artifact for [T; N] {
    fn encode<S: Serializer>(
        &self,
        serializer: S,
        context: &ArtifactContext<'_>,
    ) -> Result<S::Ok, S::Error> {
        encode_sequence(self.iter(), serializer, context)
    }
    fn decode<'de, D: Deserializer<'de>>(
        deserializer: D,
        context: &ArtifactContext<'_>,
    ) -> Result<Self, D::Error> {
        Vec::<T>::decode(deserializer, context)?
            .try_into()
            .map_err(|_| de::Error::custom("artifact array length mismatch"))
    }
}
