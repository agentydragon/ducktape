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
    let mut bytes = Vec::new();
    write_protobuf(value, &mut bytes)?;
    Ok(bytes)
}

/// Write one typed protobuf message as a checksummed zstd frame.
///
/// Prost encodes into an uncompressed `Vec`; the compressed frame is written
/// incrementally to `writer` instead of being collected as a second full-size
/// artifact buffer.
pub fn write_protobuf<T: prost::Message, W: std::io::Write>(value: &T, writer: W) -> Result<()> {
    use std::io::Write;
    let mut encoder = zstd::Encoder::new(writer, ZSTD_LEVEL)?;
    encoder.include_checksum(true)?;
    encoder
        .write_all(&value.encode_to_vec())
        .context("compress analysis protobuf")?;
    encoder.finish()?;
    Ok(())
}

/// Decode exactly one complete zstd frame before accepting the protobuf.
pub fn decode_protobuf<T: prost::Message + Default>(bytes: &[u8]) -> Result<T> {
    decode_protobuf_from(bytes)
}

/// Decode exactly one complete zstd frame from a reader before accepting the
/// protobuf. The frame and checksum are validated before any trailing bytes
/// are rejected.
pub fn decode_protobuf_from<T: prost::Message + Default, R: std::io::Read>(reader: R) -> Result<T> {
    use std::io::{BufRead, BufReader, Read};
    let mut decoder = zstd::Decoder::with_buffer(BufReader::new(reader))?.single_frame();
    let mut decoded = Vec::new();
    decoder
        .read_to_end(&mut decoded)
        .context("decompress analysis protobuf")?;
    let mut reader = decoder.finish();
    anyhow::ensure!(reader.fill_buf()?.is_empty(), "trailing compressed data");
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

    struct ShortReader<R> {
        inner: R,
        max_read: usize,
    }

    impl<R: std::io::Read> std::io::Read for ShortReader<R> {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            let length = buffer.len().min(self.max_read);
            self.inner.read(&mut buffer[..length])
        }
    }

    struct ShortWriter<W> {
        inner: W,
        max_write: usize,
    }

    impl<W: std::io::Write> std::io::Write for ShortWriter<W> {
        fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
            self.inner
                .write(&buffer[..buffer.len().min(self.max_write)])
        }

        fn flush(&mut self) -> std::io::Result<()> {
            self.inner.flush()
        }
    }

    struct FailingWriter;

    impl std::io::Write for FailingWriter {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other("fixture writer failure"))
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    struct FailingReader<R> {
        inner: R,
        remaining: usize,
    }

    impl<R: std::io::Read> std::io::Read for FailingReader<R> {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            if self.remaining == 0 {
                return Err(std::io::Error::other("fixture reader failure"));
            }
            let length = buffer.len().min(self.remaining);
            let read = self.inner.read(&mut buffer[..length])?;
            self.remaining -= read;
            Ok(read)
        }
    }

    #[test]
    fn protobuf_streaming_io_handles_partial_reads_and_writes() {
        let record = Record {
            value: "payload repeated enough to span multiple short writes".repeat(8),
        };
        let mut bytes = ShortWriter {
            inner: Vec::new(),
            max_write: 3,
        };
        write_protobuf(&record, &mut bytes).expect("short writer accepts the full frame");
        let reader = ShortReader {
            inner: bytes.inner.as_slice(),
            max_read: 2,
        };
        assert_eq!(
            decode_protobuf_from::<Record, _>(reader)
                .expect("short reader supplies the full frame"),
            record
        );
    }

    #[test]
    fn protobuf_streaming_io_surfaces_reader_and_writer_errors() {
        let record = Record {
            value: "payload".into(),
        };
        assert!(write_protobuf(&record, FailingWriter).is_err());

        let bytes = encode_protobuf(&record).expect("encode fixture");
        let reader = FailingReader {
            inner: bytes.as_slice(),
            remaining: bytes.len() / 2,
        };
        assert!(decode_protobuf_from::<Record, _>(reader).is_err());
    }

    #[test]
    fn streaming_protobuf_decode_rejects_truncated_corrupt_and_trailing_data() {
        let record = Record {
            value: "payload".into(),
        };
        let bytes = encode_protobuf(&record).expect("encode fixture");
        assert!(decode_protobuf_from::<Record, _>(&bytes[..bytes.len() - 1]).is_err());

        let mut damaged = bytes.clone();
        *damaged.last_mut().expect("nonempty frame") ^= 1;
        assert!(decode_protobuf_from::<Record, _>(damaged.as_slice()).is_err());

        let mut trailing = bytes.clone();
        trailing.extend([0xff; 4]);
        assert!(decode_protobuf_from::<Record, _>(trailing.as_slice()).is_err());

        let mut extra_frame = bytes.clone();
        extra_frame.extend_from_slice(&bytes);
        assert!(decode_protobuf_from::<Record, _>(extra_frame.as_slice()).is_err());
    }
}
