//! Process-independent MessagePack encoding of analysis output.
//!
//! SWC binding identity is `Id = (Atom, SyntaxContext)`, and a
//! `SyntaxContext` is an index into the process-local hygiene table, which
//! chunks parsed in parallel fill in no fixed order. The codec writes each
//! context as its ordinal in [`ContextOrdinals`]: the order of first
//! occurrence in a pre-order walk of the analyzed modules. The resolver's
//! scope structure is deterministic for given source text, so a fresh parse
//! of the same modules in any process numbers the same contexts the same way,
//! and the decoder maps each ordinal back to that process's context. Inner
//! scopes keep their own ordinal, so a closure-local `counter` never decodes
//! as the top-level `counter`.
//!
//! An `Id` serializes as a 2-tuple of a string and a `u32`. The encoder writes
//! the second element of every such tuple as `-(ordinal + 1)`; the decoder
//! maps a negative integer back wherever a 2-tuple's second element is read
//! as a `u32`. Analysis types therefore must not hold other `(string, u32)`
//! tuples. A context outside the ordinal table fails the encode, and serde
//! paths that buffer content (`flatten`, internally tagged and untagged
//! enums) bypass the decoder and fail on the negative integer, so neither
//! case can silently keep a process-local number.
//!
//! Spans need no mapping: every chunk is parsed into its own `SourceMap`, so a
//! `BytePos` is already an offset from that chunk's file start.
//!
//! Fields kept out of JSON reports (`#[serde(skip)]` there: spans, purity
//! causes, trusted dependencies) must still round-trip through the artifact.
//! They use [`outside_artifact`] as their `skip_serializing_if`, which is true
//! except while [`encode`] runs, and purity causes use [`shared_arc`], which
//! writes each shared value once so the cause DAG keeps its sharing.
//!
//! Structs are encoded with field names (`rmp_serde`'s struct maps), which the
//! existing `skip_serializing_if` and tagged-enum attributes require, then
//! zstd-compressed.

use std::any::Any;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde::de::{self, Deserialize, DeserializeOwned, DeserializeSeed, Deserializer, Visitor};
use serde::ser::{self, Serialize, Serializer};
use swc_common::SyntaxContext;
use swc_ecma_ast::Module;
use swc_ecma_visit::{Visit, VisitWith};

const ZSTD_LEVEL: i32 = 3;

/// First-occurrence ordinals of the hygiene contexts in a list of modules.
/// Ordinal 0 is the empty context.
pub struct ContextOrdinals {
    ordinals: HashMap<SyntaxContext, u32>,
    contexts: Vec<SyntaxContext>,
}

impl ContextOrdinals {
    /// Numbers contexts across `modules` in the given order; callers pass the
    /// same modules in the same order in every process.
    pub fn new<'m>(modules: impl IntoIterator<Item = &'m Module>) -> Self {
        let mut collector = Self {
            ordinals: HashMap::from([(SyntaxContext::empty(), 0)]),
            contexts: vec![SyntaxContext::empty()],
        };
        for module in modules {
            module.visit_with(&mut collector);
        }
        collector
    }

    fn ordinal(&self, raw: u32) -> Option<u32> {
        self.ordinals.get(&SyntaxContext::from_u32(raw)).copied()
    }

    fn context(&self, ordinal: u32) -> Option<u32> {
        self.contexts
            .get(ordinal as usize)
            .map(|context| context.as_u32())
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

/// zstd-compressed named MessagePack of `value`, contexts as ordinals.
pub fn encode<T: Serialize>(value: &T, ordinals: &ContextOrdinals) -> Result<Vec<u8>> {
    let _scope = ArtifactScope::enter();
    let mut encoder = zstd::Encoder::new(Vec::new(), ZSTD_LEVEL)?;
    let mut serializer = rmp_serde::Serializer::new(&mut encoder).with_struct_map();
    value.serialize(Encoder {
        inner: &mut serializer,
        ordinals,
        role: Role::Plain,
    })?;
    Ok(encoder.finish()?)
}

/// Inverse of [`encode`], given the ordinals of the same modules parsed in
/// this process.
pub fn decode<T: DeserializeOwned>(bytes: &[u8], ordinals: &ContextOrdinals) -> Result<T> {
    let bytes = zstd::decode_all(bytes).context("decompress analysis output")?;
    let _scope = ArtifactScope::enter();
    let mut deserializer = rmp_serde::Deserializer::from_read_ref(&bytes);
    Ok(T::deserialize(Decoder {
        inner: &mut deserializer,
        ordinals,
        context_slot: false,
    })?)
}

/// Shared values written or read so far by the running [`encode`] or
/// [`decode`]; `None` outside them.
#[derive(Default)]
struct SharedValues {
    written: HashMap<*const (), u32>,
    read: HashMap<u32, Arc<dyn Any + Send + Sync>>,
}

thread_local! {
    static ARTIFACT: RefCell<Option<SharedValues>> = const { RefCell::new(None) };
}

struct ArtifactScope;

impl ArtifactScope {
    fn enter() -> Self {
        ARTIFACT.with(|artifact| {
            let previous = artifact.replace(Some(SharedValues::default()));
            assert!(previous.is_none(), "analysis codec calls do not nest");
        });
        Self
    }
}

impl Drop for ArtifactScope {
    fn drop(&mut self) {
        ARTIFACT.with(|artifact| artifact.replace(None));
    }
}

/// `skip_serializing_if` for a field JSON reports leave out but the artifact
/// keeps; pair it with `default`.
pub fn outside_artifact<T: ?Sized>(_: &T) -> bool {
    ARTIFACT.with(|artifact| artifact.borrow().is_none())
}

/// `with` module for an `Option<Arc<T>>` whose sharing matters: each value
/// is written once, at its first occurrence, and later occurrences name it
/// by the order of that first occurrence. Only valid inside the artifact
/// (see [`outside_artifact`]).
pub mod shared_arc {
    use super::*;

    pub fn serialize<T: Serialize, S: Serializer>(
        value: &Option<Arc<T>>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        let Some(value) = value else {
            return serializer.serialize_none();
        };
        let (index, first) = ARTIFACT.with(|artifact| {
            let mut artifact = artifact.borrow_mut();
            let written = &mut artifact
                .as_mut()
                .expect("shared_arc serializes only inside the artifact")
                .written;
            let next = u32::try_from(written.len()).expect("fewer than 2^32 shared values");
            let index = *written
                .entry(Arc::as_ptr(value).cast::<()>())
                .or_insert(next);
            (index, index == next)
        });
        serializer.serialize_some(&(index, first.then_some(value.as_ref())))
    }

    pub fn deserialize<'de, T, D>(deserializer: D) -> Result<Option<Arc<T>>, D::Error>
    where
        T: Deserialize<'de> + Send + Sync + 'static,
        D: Deserializer<'de>,
    {
        let Some((index, value)) = Option::<(u32, Option<T>)>::deserialize(deserializer)? else {
            return Ok(None);
        };
        ARTIFACT.with(|artifact| {
            let mut artifact = artifact.borrow_mut();
            let read = &mut artifact
                .as_mut()
                .expect("shared_arc deserializes only inside the artifact")
                .read;
            let shared = match value {
                Some(value) => {
                    let shared = Arc::new(value);
                    read.insert(index, shared.clone());
                    shared
                }
                None => read
                    .get(&index)
                    .ok_or_else(|| {
                        de::Error::custom(format!("shared value {index} read before written"))
                    })?
                    .clone()
                    .downcast::<T>()
                    .map_err(|_| {
                        de::Error::custom(format!("shared value {index} has another type"))
                    })?,
            };
            Ok(Some(shared))
        })
    }
}

fn encode_ordinal(ordinal: u32) -> i64 {
    -i64::from(ordinal) - 1
}

fn decode_ordinal(value: i64) -> Option<u32> {
    u32::try_from(-(value + 1)).ok()
}

// --- Encoder ---------------------------------------------------------------

/// What the value being serialized is, as far as context mapping goes.
#[derive(Clone, Copy)]
enum Role<'r> {
    Plain,
    /// First element of a 2-tuple; records whether it was a string.
    PairHead(&'r Cell<bool>),
    /// Second element of a `(string, _)` 2-tuple: a `u32` is a context.
    Context,
}

struct Encoder<'o, 'r, S> {
    inner: S,
    ordinals: &'o ContextOrdinals,
    role: Role<'r>,
}

/// A value to serialize through an [`Encoder`] with `role`.
struct Encoded<'v, 'o, 'r, T: ?Sized> {
    value: &'v T,
    ordinals: &'o ContextOrdinals,
    role: Role<'r>,
}

impl<T: Serialize + ?Sized> Serialize for Encoded<'_, '_, '_, T> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.value.serialize(Encoder {
            inner: serializer,
            ordinals: self.ordinals,
            role: self.role,
        })
    }
}

/// Compound serializer whose children are all plain values.
struct Compound<'o, C> {
    inner: C,
    ordinals: &'o ContextOrdinals,
}

impl<'o, C> Compound<'o, C> {
    fn plain<'v, T: ?Sized>(&self, value: &'v T) -> Encoded<'v, 'o, 'static, T> {
        Encoded {
            value,
            ordinals: self.ordinals,
            role: Role::Plain,
        }
    }
}

/// Tuple serializer; maps the second element of a `(string, u32)` pair.
struct Tuple<'o, C> {
    inner: C,
    ordinals: &'o ContextOrdinals,
    pair: bool,
    index: usize,
    head_is_string: Cell<bool>,
}

impl<'o, S: Serializer> Encoder<'o, '_, S> {
    fn plain<'v, T: ?Sized>(&self, value: &'v T) -> Encoded<'v, 'o, 'static, T> {
        Encoded {
            value,
            ordinals: self.ordinals,
            role: Role::Plain,
        }
    }
}

macro_rules! forward_scalars {
    ($($method:ident($ty:ty)),* $(,)?) => {
        $(fn $method(self, value: $ty) -> Result<S::Ok, S::Error> {
            self.inner.$method(value)
        })*
    };
}

impl<'o, 'r, S: Serializer> Serializer for Encoder<'o, 'r, S> {
    type Ok = S::Ok;
    type Error = S::Error;
    type SerializeSeq = Compound<'o, S::SerializeSeq>;
    type SerializeTuple = Tuple<'o, S::SerializeTuple>;
    type SerializeTupleStruct = Compound<'o, S::SerializeTupleStruct>;
    type SerializeTupleVariant = Compound<'o, S::SerializeTupleVariant>;
    type SerializeMap = Compound<'o, S::SerializeMap>;
    type SerializeStruct = Compound<'o, S::SerializeStruct>;
    type SerializeStructVariant = Compound<'o, S::SerializeStructVariant>;

    forward_scalars!(
        serialize_bool(bool),
        serialize_i8(i8),
        serialize_i16(i16),
        serialize_i32(i32),
        serialize_i64(i64),
        serialize_i128(i128),
        serialize_u8(u8),
        serialize_u16(u16),
        serialize_u64(u64),
        serialize_u128(u128),
        serialize_f32(f32),
        serialize_f64(f64),
        serialize_char(char),
        serialize_bytes(&[u8]),
    );

    fn serialize_u32(self, value: u32) -> Result<S::Ok, S::Error> {
        match self.role {
            Role::Context => {
                let ordinal = self.ordinals.ordinal(value).ok_or_else(|| {
                    ser::Error::custom(format!(
                        "SyntaxContext {value} does not occur in the analyzed modules"
                    ))
                })?;
                self.inner.serialize_i64(encode_ordinal(ordinal))
            }
            Role::Plain | Role::PairHead(_) => self.inner.serialize_u32(value),
        }
    }

    fn serialize_str(self, value: &str) -> Result<S::Ok, S::Error> {
        if let Role::PairHead(head_is_string) = self.role {
            head_is_string.set(true);
        }
        self.inner.serialize_str(value)
    }

    fn serialize_none(self) -> Result<S::Ok, S::Error> {
        self.inner.serialize_none()
    }

    fn serialize_some<T: Serialize + ?Sized>(self, value: &T) -> Result<S::Ok, S::Error> {
        let value = self.plain(value);
        self.inner.serialize_some(&value)
    }

    fn serialize_unit(self) -> Result<S::Ok, S::Error> {
        self.inner.serialize_unit()
    }

    fn serialize_unit_struct(self, name: &'static str) -> Result<S::Ok, S::Error> {
        self.inner.serialize_unit_struct(name)
    }

    fn serialize_unit_variant(
        self,
        name: &'static str,
        index: u32,
        variant: &'static str,
    ) -> Result<S::Ok, S::Error> {
        self.inner.serialize_unit_variant(name, index, variant)
    }

    fn serialize_newtype_struct<T: Serialize + ?Sized>(
        self,
        name: &'static str,
        value: &T,
    ) -> Result<S::Ok, S::Error> {
        let value = self.plain(value);
        self.inner.serialize_newtype_struct(name, &value)
    }

    fn serialize_newtype_variant<T: Serialize + ?Sized>(
        self,
        name: &'static str,
        index: u32,
        variant: &'static str,
        value: &T,
    ) -> Result<S::Ok, S::Error> {
        let value = self.plain(value);
        self.inner
            .serialize_newtype_variant(name, index, variant, &value)
    }

    fn serialize_seq(self, len: Option<usize>) -> Result<Self::SerializeSeq, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self.inner.serialize_seq(len)?,
            ordinals,
        })
    }

    fn serialize_tuple(self, len: usize) -> Result<Self::SerializeTuple, S::Error> {
        Ok(Tuple {
            ordinals: self.ordinals,
            inner: self.inner.serialize_tuple(len)?,
            pair: len == 2,
            index: 0,
            head_is_string: Cell::new(false),
        })
    }

    fn serialize_tuple_struct(
        self,
        name: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleStruct, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self.inner.serialize_tuple_struct(name, len)?,
            ordinals,
        })
    }

    fn serialize_tuple_variant(
        self,
        name: &'static str,
        index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeTupleVariant, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self
                .inner
                .serialize_tuple_variant(name, index, variant, len)?,
            ordinals,
        })
    }

    fn serialize_map(self, len: Option<usize>) -> Result<Self::SerializeMap, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self.inner.serialize_map(len)?,
            ordinals,
        })
    }

    fn serialize_struct(
        self,
        name: &'static str,
        len: usize,
    ) -> Result<Self::SerializeStruct, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self.inner.serialize_struct(name, len)?,
            ordinals,
        })
    }

    fn serialize_struct_variant(
        self,
        name: &'static str,
        index: u32,
        variant: &'static str,
        len: usize,
    ) -> Result<Self::SerializeStructVariant, S::Error> {
        let ordinals = self.ordinals;
        Ok(Compound {
            inner: self
                .inner
                .serialize_struct_variant(name, index, variant, len)?,
            ordinals,
        })
    }

    fn is_human_readable(&self) -> bool {
        self.inner.is_human_readable()
    }
}

impl<C: ser::SerializeTuple> ser::SerializeTuple for Tuple<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_element<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), C::Error> {
        let role = match (self.pair, self.index) {
            (true, 0) => Role::PairHead(&self.head_is_string),
            (true, 1) if self.head_is_string.get() => Role::Context,
            _ => Role::Plain,
        };
        self.index += 1;
        self.inner.serialize_element(&Encoded {
            value,
            ordinals: self.ordinals,
            role,
        })
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeSeq> ser::SerializeSeq for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_element<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_element(&value)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeTupleStruct> ser::SerializeTupleStruct for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_field<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_field(&value)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeTupleVariant> ser::SerializeTupleVariant for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_field<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_field(&value)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeMap> ser::SerializeMap for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_key<T: Serialize + ?Sized>(&mut self, key: &T) -> Result<(), C::Error> {
        let key = self.plain(key);
        self.inner.serialize_key(&key)
    }

    fn serialize_value<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_value(&value)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeStruct> ser::SerializeStruct for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        key: &'static str,
        value: &T,
    ) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_field(key, &value)
    }

    fn skip_field(&mut self, key: &'static str) -> Result<(), C::Error> {
        self.inner.skip_field(key)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

impl<C: ser::SerializeStructVariant> ser::SerializeStructVariant for Compound<'_, C> {
    type Ok = C::Ok;
    type Error = C::Error;

    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        key: &'static str,
        value: &T,
    ) -> Result<(), C::Error> {
        let value = self.plain(value);
        self.inner.serialize_field(key, &value)
    }

    fn skip_field(&mut self, key: &'static str) -> Result<(), C::Error> {
        self.inner.skip_field(key)
    }

    fn end(self) -> Result<C::Ok, C::Error> {
        self.inner.end()
    }
}

// --- Decoder ---------------------------------------------------------------

struct Decoder<'o, D> {
    inner: D,
    ordinals: &'o ContextOrdinals,
    /// The second element of a 2-tuple: a negative integer read as `u32` is
    /// a context ordinal.
    context_slot: bool,
}

/// A visitor forwarding to `inner`, decoding nested values through
/// [`Decoder`].
struct Visiting<'o, V> {
    inner: V,
    ordinals: &'o ContextOrdinals,
    context_slot: bool,
    pair: bool,
}

struct Seeded<'o, T> {
    inner: T,
    ordinals: &'o ContextOrdinals,
    context_slot: bool,
}

struct Access<'o, A> {
    inner: A,
    ordinals: &'o ContextOrdinals,
    pair: bool,
    index: usize,
}

impl<'o, D> Decoder<'o, D> {
    fn visiting<V>(&self, inner: V) -> Visiting<'o, V> {
        Visiting {
            inner,
            ordinals: self.ordinals,
            context_slot: false,
            pair: false,
        }
    }
}

macro_rules! forward_deserialize {
    ($($method:ident),* $(,)?) => {
        $(fn $method<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, D::Error> {
            let visitor = self.visiting(visitor);
            self.inner.$method(visitor)
        })*
    };
}

impl<'de, D: Deserializer<'de>> Deserializer<'de> for Decoder<'_, D> {
    type Error = D::Error;

    forward_deserialize!(
        deserialize_any,
        deserialize_bool,
        deserialize_i8,
        deserialize_i16,
        deserialize_i32,
        deserialize_i64,
        deserialize_i128,
        deserialize_u8,
        deserialize_u16,
        deserialize_u64,
        deserialize_u128,
        deserialize_f32,
        deserialize_f64,
        deserialize_char,
        deserialize_str,
        deserialize_string,
        deserialize_bytes,
        deserialize_byte_buf,
        deserialize_option,
        deserialize_unit,
        deserialize_seq,
        deserialize_map,
        deserialize_identifier,
        deserialize_ignored_any,
    );

    fn deserialize_u32<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, D::Error> {
        if self.context_slot {
            self.inner.deserialize_any(Visiting {
                inner: visitor,
                ordinals: self.ordinals,
                context_slot: true,
                pair: false,
            })
        } else {
            let visitor = self.visiting(visitor);
            self.inner.deserialize_u32(visitor)
        }
    }

    fn deserialize_unit_struct<V: Visitor<'de>>(
        self,
        name: &'static str,
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        let visitor = self.visiting(visitor);
        self.inner.deserialize_unit_struct(name, visitor)
    }

    fn deserialize_newtype_struct<V: Visitor<'de>>(
        self,
        name: &'static str,
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        let visitor = self.visiting(visitor);
        self.inner.deserialize_newtype_struct(name, visitor)
    }

    fn deserialize_tuple<V: Visitor<'de>>(
        self,
        len: usize,
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        self.inner.deserialize_tuple(
            len,
            Visiting {
                inner: visitor,
                ordinals: self.ordinals,
                context_slot: false,
                pair: len == 2,
            },
        )
    }

    fn deserialize_tuple_struct<V: Visitor<'de>>(
        self,
        name: &'static str,
        len: usize,
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        let visitor = self.visiting(visitor);
        self.inner.deserialize_tuple_struct(name, len, visitor)
    }

    fn deserialize_struct<V: Visitor<'de>>(
        self,
        name: &'static str,
        fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        let visitor = self.visiting(visitor);
        self.inner.deserialize_struct(name, fields, visitor)
    }

    fn deserialize_enum<V: Visitor<'de>>(
        self,
        name: &'static str,
        variants: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, D::Error> {
        let visitor = self.visiting(visitor);
        self.inner.deserialize_enum(name, variants, visitor)
    }

    fn is_human_readable(&self) -> bool {
        self.inner.is_human_readable()
    }
}

impl<'o, V> Visiting<'o, V> {
    fn decoder<D>(&self, inner: D) -> Decoder<'o, D> {
        Decoder {
            inner,
            ordinals: self.ordinals,
            context_slot: false,
        }
    }

    fn access<A>(&self, inner: A) -> Access<'o, A> {
        Access {
            inner,
            ordinals: self.ordinals,
            pair: self.pair,
            index: 0,
        }
    }
}

macro_rules! forward_visit {
    ($($method:ident($ty:ty)),* $(,)?) => {
        $(fn $method<E: de::Error>(self, value: $ty) -> Result<V::Value, E> {
            self.inner.$method(value)
        })*
    };
}

impl<'de, V: Visitor<'de>> Visitor<'de> for Visiting<'_, V> {
    type Value = V::Value;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        self.inner.expecting(formatter)
    }

    forward_visit!(
        visit_bool(bool),
        visit_i128(i128),
        visit_u8(u8),
        visit_u16(u16),
        visit_u32(u32),
        visit_u64(u64),
        visit_u128(u128),
        visit_f32(f32),
        visit_f64(f64),
        visit_char(char),
        visit_str(&str),
        visit_borrowed_str(&'de str),
        visit_string(String),
        visit_bytes(&[u8]),
        visit_borrowed_bytes(&'de [u8]),
        visit_byte_buf(Vec<u8>),
    );

    fn visit_i8<E: de::Error>(self, value: i8) -> Result<V::Value, E> {
        self.visit_i64(i64::from(value))
    }

    fn visit_i16<E: de::Error>(self, value: i16) -> Result<V::Value, E> {
        self.visit_i64(i64::from(value))
    }

    fn visit_i32<E: de::Error>(self, value: i32) -> Result<V::Value, E> {
        self.visit_i64(i64::from(value))
    }

    fn visit_i64<E: de::Error>(self, value: i64) -> Result<V::Value, E> {
        if !self.context_slot || value >= 0 {
            return self.inner.visit_i64(value);
        }
        let context = decode_ordinal(value)
            .and_then(|ordinal| self.ordinals.context(ordinal))
            .ok_or_else(|| {
                E::custom(format!(
                    "context ordinal {value} does not occur in the analyzed modules"
                ))
            })?;
        self.inner.visit_u32(context)
    }

    fn visit_none<E: de::Error>(self) -> Result<V::Value, E> {
        self.inner.visit_none()
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<V::Value, D::Error> {
        let deserializer = self.decoder(deserializer);
        self.inner.visit_some(deserializer)
    }

    fn visit_unit<E: de::Error>(self) -> Result<V::Value, E> {
        self.inner.visit_unit()
    }

    fn visit_newtype_struct<D: Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<V::Value, D::Error> {
        let deserializer = self.decoder(deserializer);
        self.inner.visit_newtype_struct(deserializer)
    }

    fn visit_seq<A: de::SeqAccess<'de>>(self, seq: A) -> Result<V::Value, A::Error> {
        let seq = self.access(seq);
        self.inner.visit_seq(seq)
    }

    fn visit_map<A: de::MapAccess<'de>>(self, map: A) -> Result<V::Value, A::Error> {
        let map = self.access(map);
        self.inner.visit_map(map)
    }

    fn visit_enum<A: de::EnumAccess<'de>>(self, data: A) -> Result<V::Value, A::Error> {
        let data = self.access(data);
        self.inner.visit_enum(data)
    }
}

impl<'de, T: DeserializeSeed<'de>> DeserializeSeed<'de> for Seeded<'_, T> {
    type Value = T::Value;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<T::Value, D::Error> {
        self.inner.deserialize(Decoder {
            inner: deserializer,
            ordinals: self.ordinals,
            context_slot: self.context_slot,
        })
    }
}

impl<'o, A> Access<'o, A> {
    fn seeded<T>(&self, inner: T, context_slot: bool) -> Seeded<'o, T> {
        Seeded {
            inner,
            ordinals: self.ordinals,
            context_slot,
        }
    }
}

impl<'de, A: de::SeqAccess<'de>> de::SeqAccess<'de> for Access<'_, A> {
    type Error = A::Error;

    fn next_element_seed<T: DeserializeSeed<'de>>(
        &mut self,
        seed: T,
    ) -> Result<Option<T::Value>, A::Error> {
        let seed = self.seeded(seed, self.pair && self.index == 1);
        self.index += 1;
        self.inner.next_element_seed(seed)
    }

    fn size_hint(&self) -> Option<usize> {
        self.inner.size_hint()
    }
}

impl<'de, A: de::MapAccess<'de>> de::MapAccess<'de> for Access<'_, A> {
    type Error = A::Error;

    fn next_key_seed<K: DeserializeSeed<'de>>(
        &mut self,
        seed: K,
    ) -> Result<Option<K::Value>, A::Error> {
        let seed = self.seeded(seed, false);
        self.inner.next_key_seed(seed)
    }

    fn next_value_seed<T: DeserializeSeed<'de>>(&mut self, seed: T) -> Result<T::Value, A::Error> {
        let seed = self.seeded(seed, false);
        self.inner.next_value_seed(seed)
    }

    fn size_hint(&self) -> Option<usize> {
        self.inner.size_hint()
    }
}

impl<'de, 'o, A: de::EnumAccess<'de>> de::EnumAccess<'de> for Access<'o, A> {
    type Error = A::Error;
    type Variant = Access<'o, A::Variant>;

    fn variant_seed<T: DeserializeSeed<'de>>(
        self,
        seed: T,
    ) -> Result<(T::Value, Self::Variant), A::Error> {
        let seed = self.seeded(seed, false);
        let (value, variant) = self.inner.variant_seed(seed)?;
        Ok((
            value,
            Access {
                inner: variant,
                ordinals: self.ordinals,
                pair: false,
                index: 0,
            },
        ))
    }
}

impl<'de, A: de::VariantAccess<'de>> de::VariantAccess<'de> for Access<'_, A> {
    type Error = A::Error;

    fn unit_variant(self) -> Result<(), A::Error> {
        self.inner.unit_variant()
    }

    fn newtype_variant_seed<T: DeserializeSeed<'de>>(self, seed: T) -> Result<T::Value, A::Error> {
        let seed = self.seeded(seed, false);
        self.inner.newtype_variant_seed(seed)
    }

    fn tuple_variant<V: Visitor<'de>>(self, len: usize, visitor: V) -> Result<V::Value, A::Error> {
        self.inner.tuple_variant(
            len,
            Visiting {
                inner: visitor,
                ordinals: self.ordinals,
                context_slot: false,
                pair: false,
            },
        )
    }

    fn struct_variant<V: Visitor<'de>>(
        self,
        fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, A::Error> {
        self.inner.struct_variant(
            fields,
            Visiting {
                inner: visitor,
                ordinals: self.ordinals,
                context_slot: false,
                pair: false,
            },
        )
    }
}
