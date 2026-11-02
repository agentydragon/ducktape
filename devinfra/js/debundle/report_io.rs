//! Streaming JSON report files, either plain or gzip encoded.

use std::fs::File;
use std::io::{BufReader, BufWriter, Write};
use std::path::Path;

use anyhow::{Context, Result};
use flate2::{Compression, GzBuilder, read::MultiGzDecoder};
use serde::{Serialize, de::DeserializeOwned};

/// Parse a plain JSON report.
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let file = File::open(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_reader(BufReader::new(file))
        .with_context(|| format!("parsing JSON report {}", path.display()))
}

/// Parse a gzip-encoded JSON report. The decoder consumes the complete stream,
/// checking gzip trailers and rejecting trailing garbage.
pub fn read_json_gzip<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let file = File::open(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_reader(MultiGzDecoder::new(BufReader::new(file)))
        .with_context(|| format!("parsing gzip JSON report {}", path.display()))
}

/// Write compact JSON directly through a bounded gzip stream. Completion
/// includes the compression footer and the underlying file's buffered flush.
pub fn write_json_gzip<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let file = File::create(path).with_context(|| format!("creating {}", path.display()))?;
    write_gzip(BufWriter::new(file), value)
        .with_context(|| format!("writing gzip JSON report {}", path.display()))
}

fn write_gzip<T: Serialize>(output: impl Write, value: &T) -> Result<()> {
    let encoder = GzBuilder::new().mtime(0).write(output, Compression::fast());
    // Coalesce serde's small writes before compression as well as buffering
    // the compressed file. Neither buffer grows with the graph's size.
    let mut buffer = BufWriter::new(encoder);
    serde_json::to_writer(&mut buffer, value)?;
    let encoder = buffer.into_inner().map_err(|error| error.into_error())?;
    let mut output = encoder.finish()?;
    output.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, Read};

    #[test]
    fn gzip_is_deterministic_and_preserves_exact_compact_json() {
        let value = serde_json::json!({"nodes": ["value", "☃"], "edges": [1, 2]});
        let mut first = Vec::new();
        write_gzip(&mut first, &value).unwrap();
        let mut second = Vec::new();
        write_gzip(&mut second, &value).unwrap();
        assert_eq!(first, second);
        assert_eq!(&first[..8], &[0x1f, 0x8b, 8, 0, 0, 0, 0, 0]);
        let mut decoded = Vec::new();
        MultiGzDecoder::new(first.as_slice())
            .read_to_end(&mut decoded)
            .unwrap();
        assert_eq!(decoded, serde_json::to_vec(&value).unwrap());
    }

    #[test]
    fn reads_plain_reports_and_validates_gzip_streams() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("report");
        let value = serde_json::json!({"nodes": [1, 2]});
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        assert_eq!(read_json::<serde_json::Value>(&path).unwrap(), value);
        write_json_gzip(&path, &value).unwrap();
        assert_eq!(read_json_gzip::<serde_json::Value>(&path).unwrap(), value);
        let encoded = std::fs::read(&path).unwrap();
        let mut damaged = encoded.clone();
        let checksum = damaged.len() - 8;
        damaged[checksum] ^= 1;
        let mut garbage = encoded.clone();
        garbage.extend_from_slice(b"garbage");
        let mut extra_json = encoded.clone();
        write_gzip(&mut extra_json, &value).unwrap();
        for invalid in [
            encoded[..encoded.len() - 1].to_vec(),
            damaged,
            garbage,
            extra_json,
            b"not JSON".to_vec(),
        ] {
            std::fs::write(&path, invalid).unwrap();
            assert!(read_json_gzip::<serde_json::Value>(&path).is_err());
        }
    }

    struct FailingWriter {
        remaining: usize,
        fail_flush: bool,
    }

    impl Write for FailingWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.remaining == 0 {
                return Err(io::Error::other("write failure"));
            }
            let length = bytes.len().min(self.remaining);
            self.remaining -= length;
            Ok(length)
        }

        fn flush(&mut self) -> io::Result<()> {
            if self.fail_flush {
                Err(io::Error::other("flush failure"))
            } else {
                Ok(())
            }
        }
    }

    #[test]
    fn propagates_serialization_write_finish_and_flush_errors() {
        let value = serde_json::json!({"nodes": [1, 2]});
        let mut complete = Vec::new();
        write_gzip(&mut complete, &value).unwrap();
        for remaining in 0..complete.len() {
            assert!(
                write_gzip(
                    FailingWriter {
                        remaining,
                        fail_flush: false,
                    },
                    &value,
                )
                .is_err()
            );
        }
        assert!(
            write_gzip(
                FailingWriter {
                    remaining: usize::MAX,
                    fail_flush: true,
                },
                &value,
            )
            .is_err()
        );
        struct InvalidJson;
        impl Serialize for InvalidJson {
            fn serialize<S: serde::Serializer>(&self, _: S) -> Result<S::Ok, S::Error> {
                Err(serde::ser::Error::custom("serialization failure"))
            }
        }
        assert!(write_gzip(Vec::new(), &InvalidJson).is_err());
    }
}
