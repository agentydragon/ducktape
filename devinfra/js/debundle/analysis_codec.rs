//! Process-independent MessagePack encoding of analysis output.
//!
//! Saved artifact data represents SWC bindings with [`SavedBinding`]. Callers
//! convert runtime identities through [`ContextOrdinals`] before encoding and
//! restore them after decoding. The codec itself uses ordinary named
//! MessagePack and zstd; tuples and integers have no special meaning here.
//!
//! Spans need no mapping: every chunk is parsed into its own `SourceMap`, so a
//! `BytePos` is already an offset from that chunk's file start.
//!

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use source_identity::{ChunkId, ChunkTable, ModuleCellId};
use swc_atoms::Atom;
use swc_common::SyntaxContext;
use swc_ecma_ast::{Id, Module};
use swc_ecma_visit::{Visit, VisitWith};

const ZSTD_LEVEL: i32 = 3;

/// A binding's saved identity, independent of the current SWC hygiene table.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct SavedBinding {
    symbol: Atom,
    context_ordinal: u32,
}

/// A chunk's persistent identity. Interned [`ChunkId`] values belong to one
/// artifact's `ChunkTable`, so saved values carry the checked source name.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SavedChunk(pub String);

/// A persistent module-cell address. The declaration origin is scoped by a
/// checked source chunk name, never by an artifact-local `ChunkId`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct SavedModuleCell {
    pub chunk: SavedChunk,
    pub declaration_origin: u32,
}

/// First-occurrence ordinals of the hygiene contexts in a list of modules.
/// Ordinal 0 is the empty context.
pub struct ContextOrdinals<'c> {
    ordinals: HashMap<SyntaxContext, u32>,
    contexts: Vec<SyntaxContext>,
    chunk_table: &'c ChunkTable,
}

impl<'c> ContextOrdinals<'c> {
    pub fn snapshot_binding(&self, binding: &Id) -> Result<SavedBinding> {
        Ok(SavedBinding {
            symbol: binding.0.clone(),
            context_ordinal: self
                .ordinal(binding.1.as_u32())
                .context("binding context is outside the analyzed modules")?,
        })
    }

    pub fn restore_binding(&self, binding: SavedBinding) -> Result<Id> {
        let context = self.context(binding.context_ordinal).with_context(|| {
            format!(
                "unknown binding context ordinal {}",
                binding.context_ordinal
            )
        })?;
        Ok((binding.symbol, SyntaxContext::from_u32(context)))
    }

    pub fn snapshot_chunk(&self, chunk: ChunkId) -> Result<SavedChunk> {
        let name = self
            .chunk_table
            .try_name(chunk)
            .with_context(|| format!("chunk ID {chunk:?} is outside the current ChunkTable"))?;
        Ok(SavedChunk(name.to_owned()))
    }

    pub fn restore_chunk(&self, chunk: SavedChunk) -> Result<ChunkId> {
        self.chunk_table.get(&chunk.0).with_context(|| {
            format!(
                "saved chunk name {:?} is absent from the current ChunkTable",
                chunk.0
            )
        })
    }

    pub fn snapshot_module_cell(&self, cell: ModuleCellId) -> Result<SavedModuleCell> {
        Ok(SavedModuleCell {
            chunk: self.snapshot_chunk(cell.chunk)?,
            declaration_origin: cell.declaration_origin,
        })
    }

    pub fn restore_module_cell(&self, cell: SavedModuleCell) -> Result<ModuleCellId> {
        Ok(ModuleCellId {
            chunk: self.restore_chunk(cell.chunk)?,
            declaration_origin: cell.declaration_origin,
        })
    }

    /// Numbers contexts across `modules`; the table supplies the namespace for
    /// checked, persistent chunk identities.
    pub fn new<'m>(
        modules: impl IntoIterator<Item = &'m Module>,
        chunk_table: &'c ChunkTable,
    ) -> Self {
        let mut collector = Self {
            ordinals: HashMap::from([(SyntaxContext::empty(), 0)]),
            contexts: vec![SyntaxContext::empty()],
            chunk_table,
        };
        for module in modules {
            module.visit_with(&mut collector);
        }
        collector
    }

    /// Number contexts from retained `modules`, then from canonical binding
    /// identities whose source AST was discarded before analysis saving.
    /// Callers must provide bindings in stable source/module order on both
    /// snapshot and restore; duplicate contexts keep their first ordinal.
    pub fn new_with_bindings<'m, 'b>(
        modules: impl IntoIterator<Item = &'m Module>,
        chunk_table: &'c ChunkTable,
        bindings: impl IntoIterator<Item = &'b Id>,
    ) -> Self {
        let mut collector = Self::new(modules, chunk_table);
        for binding in bindings {
            collector.intern_context(&binding.1);
        }
        collector
    }

    fn intern_context(&mut self, context: &SyntaxContext) {
        if !self.ordinals.contains_key(context) {
            let ordinal = u32::try_from(self.contexts.len()).expect("fewer than 2^32 contexts");
            self.ordinals.insert(*context, ordinal);
            self.contexts.push(*context);
        }
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

impl Visit for ContextOrdinals<'_> {
    fn visit_syntax_context(&mut self, context: &SyntaxContext) {
        self.intern_context(context);
    }
}

/// zstd-compressed named MessagePack of an already process-independent value.
pub fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    let mut encoder = zstd::Encoder::new(Vec::new(), ZSTD_LEVEL)?;
    let mut serializer = rmp_serde::Serializer::new(&mut encoder).with_struct_map();
    value.serialize(&mut serializer)?;
    Ok(encoder.finish()?)
}

/// Inverse of [`encode`]. The saved value's caller restores bindings with the
/// corresponding [`ContextOrdinals`].
pub fn decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    let mut reader = zstd::Decoder::new(bytes).context("decompress analysis output")?;
    let value = {
        let mut deserializer = rmp_serde::Deserializer::new(&mut reader);
        T::deserialize(&mut deserializer)?
    };
    // Deserializing one value may stop before the compressed frame's checksum
    // or a later frame. Consume the remainder so corruption still fails.
    std::io::copy(&mut reader, &mut std::io::sink()).context("decompress analysis output")?;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{GLOBALS, Globals, Mark};

    #[test]
    fn saved_bindings_restore_into_a_fresh_globals_table() {
        let saved = GLOBALS.set(&Globals::new(), || {
            let context = SyntaxContext::empty().apply_mark(Mark::new());
            let unknown = SyntaxContext::empty().apply_mark(Mark::new());
            let chunks = ChunkTable::default();
            let mut writer = ContextOrdinals::new([], &chunks);
            writer.visit_syntax_context(&context);
            let saved = writer
                .snapshot_binding(&("captured".into(), context))
                .expect("known binding");
            assert!(
                writer
                    .snapshot_binding(&("captured".into(), unknown))
                    .is_err()
            );
            saved
        });

        GLOBALS.set(&Globals::new(), || {
            let context = SyntaxContext::empty().apply_mark(Mark::new());
            let chunks = ChunkTable::default();
            let mut reader = ContextOrdinals::new([], &chunks);
            reader.visit_syntax_context(&context);
            assert_eq!(
                reader.restore_binding(saved).expect("known ordinal"),
                ("captured".into(), context),
            );
            assert!(
                reader
                    .restore_binding(SavedBinding {
                        symbol: "captured".into(),
                        context_ordinal: 2,
                    })
                    .is_err()
            );
        });
    }

    #[test]
    fn ordinary_tuples_round_trip_without_context_interpretation() {
        let values = vec![("counter".to_string(), 0_u32), ("ordinary".to_string(), 7)];
        let encoded = encode(&values).expect("encode tuple data");
        assert_eq!(decode::<Vec<(String, u32)>>(&encoded).unwrap(), values);
    }
}
