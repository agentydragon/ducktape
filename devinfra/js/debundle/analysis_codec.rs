//! Process-independent identities and compressed Protobuf for saved analysis.
//!
//! Saved artifact data represents SWC bindings with [`SavedBinding`]. Callers
//! convert runtime identities through [`ContextOrdinals`] before encoding and
//! restore them after decoding.
//!
//! Spans need no mapping: every chunk is parsed into its own `SourceMap`, so a
//! `BytePos` is already an offset from that chunk's file start.
//!

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
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

impl SavedBinding {
    pub fn symbol(&self) -> &Atom {
        &self.symbol
    }
    pub fn context_ordinal(&self) -> u32 {
        self.context_ordinal
    }
    pub fn from_parts(symbol: Atom, context_ordinal: u32) -> Self {
        Self {
            symbol,
            context_ordinal,
        }
    }
}

/// First-occurrence ordinals of the hygiene contexts in a list of modules.
/// Ordinal 0 is the empty context.
pub struct ContextOrdinals {
    ordinals: HashMap<SyntaxContext, u32>,
    contexts: Vec<SyntaxContext>,
}

impl ContextOrdinals {
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

    /// Numbers contexts across `modules`; callers use the same modules in the
    /// same order when writing and reading an artifact.
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

/// Compress one typed protobuf message; the checksum covers the entire frame.
pub fn encode_protobuf<T: prost::Message>(value: &T) -> Result<Vec<u8>> {
    use std::io::Write;
    let mut encoder = zstd::Encoder::new(Vec::new(), ZSTD_LEVEL)?;
    encoder.include_checksum(true)?;
    encoder.write_all(&value.encode_to_vec())?;
    Ok(encoder.finish()?)
}

/// Decode exactly one complete zstd frame before accepting the protobuf.
pub fn decode_protobuf<T: prost::Message + Default>(bytes: &[u8]) -> Result<T> {
    use std::io::Read;
    let mut decoder = zstd::Decoder::with_buffer(bytes)?.single_frame();
    let mut decoded = Vec::new();
    decoder
        .read_to_end(&mut decoded)
        .context("decompress analysis protobuf")?;
    anyhow::ensure!(decoder.finish().is_empty(), "trailing compressed data");
    T::decode(decoded.as_slice()).context("decode analysis protobuf")
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
            let mut writer = ContextOrdinals::new([]);
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
            let mut reader = ContextOrdinals::new([]);
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

    #[derive(Clone, PartialEq, prost::Message)]
    struct Record {
        #[prost(string, tag = "1")]
        value: String,
    }

    #[test]
    fn protobuf_decompression_checks_complete_frames() {
        let record = Record {
            value: "payload".into(),
        };
        let bytes = encode_protobuf(&record).unwrap();
        assert_eq!(decode_protobuf::<Record>(&bytes).unwrap(), record);
        assert!(decode_protobuf::<Record>(&bytes[..bytes.len() - 1]).is_err());
        let mut damaged = bytes.clone();
        *damaged.last_mut().unwrap() ^= 1;
        assert!(decode_protobuf::<Record>(&damaged).is_err());
        let mut trailing = bytes;
        trailing.extend([0xff; 4]);
        assert!(decode_protobuf::<Record>(&trailing).is_err());
        let mut extra_frame = encode_protobuf(&record).unwrap();
        extra_frame.extend(encode_protobuf(&record).unwrap());
        assert!(decode_protobuf::<Record>(&extra_frame).is_err());
    }
}
