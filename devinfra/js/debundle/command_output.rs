//! Command output formatting and stdout emission.

use anyhow::Result;
use peel::OutputFormat;
use serde::Serialize;
use std::io::{self, Write};

/// A failure while rendering or emitting one command report.
#[derive(Debug, thiserror::Error)]
pub enum ReportOutputError {
    #[error("rendering report")]
    Render(#[source] anyhow::Error),
    #[error("serializing report")]
    Serialize(#[source] serde_json::Error),
    #[error("writing report")]
    Write(#[source] io::Error),
    #[error("flushing report output")]
    Flush(#[source] io::Error),
}

impl ReportOutputError {
    /// Whether this report failed because stdout's consumer closed the pipe.
    pub fn is_broken_pipe(&self) -> bool {
        matches!(
            self,
            Self::Write(error) | Self::Flush(error)
                if error.kind() == io::ErrorKind::BrokenPipe
        )
    }
}

/// Write `value` in the requested `format`. Text is rendered before any bytes
/// are written, so a rendering error cannot leave a partial successful report.
pub fn write_report<T, F, W>(
    value: &T,
    format: OutputFormat,
    output: &mut W,
    text_render: F,
) -> std::result::Result<(), ReportOutputError>
where
    T: Serialize,
    F: FnOnce(&T, &mut String) -> Result<()>,
    W: Write + ?Sized,
{
    match format {
        OutputFormat::Text => {
            let mut buffer = String::new();
            text_render(value, &mut buffer).map_err(ReportOutputError::Render)?;
            if !buffer.is_empty() && !buffer.ends_with('\n') {
                buffer.push('\n');
            }
            output
                .write_all(buffer.as_bytes())
                .map_err(ReportOutputError::Write)?;
        }
        OutputFormat::Json => {
            let mut bytes =
                serde_json::to_vec_pretty(value).map_err(ReportOutputError::Serialize)?;
            bytes.push(b'\n');
            output.write_all(&bytes).map_err(ReportOutputError::Write)?;
        }
        OutputFormat::Ndjson => {
            // For ndjson on a non-array shape, fall back to one compact JSON
            // document per call. Array commands use `write_ndjson_list`.
            let mut bytes = serde_json::to_vec(value).map_err(ReportOutputError::Serialize)?;
            bytes.push(b'\n');
            output.write_all(&bytes).map_err(ReportOutputError::Write)?;
        }
    }
    output.flush().map_err(ReportOutputError::Flush)
}

/// For a command whose primary output is an array: when `format` is `Ndjson`,
/// write one compact JSON line per item and return `true` (the caller should
/// stop). Otherwise emit nothing and return `false`, so the caller falls
/// through to [`write_report`] for the text/json rendering of the whole report.
pub fn write_ndjson_list<T: Serialize, W: Write + ?Sized>(
    items: &[T],
    format: OutputFormat,
    output: &mut W,
) -> std::result::Result<bool, ReportOutputError> {
    if format != OutputFormat::Ndjson {
        return Ok(false);
    }
    for item in items {
        let mut bytes = serde_json::to_vec(item).map_err(ReportOutputError::Serialize)?;
        bytes.push(b'\n');
        output.write_all(&bytes).map_err(ReportOutputError::Write)?;
    }
    output.flush().map_err(ReportOutputError::Flush)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::{ReportOutputError, write_ndjson_list, write_report};
    use anyhow::{Result, bail};
    use peel::OutputFormat;
    use serde::Serialize;
    use std::io::{self, ErrorKind, Write};

    #[derive(Default)]
    struct TestWriter {
        bytes: Vec<u8>,
        fail_write: Option<ErrorKind>,
        fail_flush: Option<ErrorKind>,
        flush_count: usize,
    }

    impl Write for TestWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if let Some(kind) = self.fail_write {
                return Err(io::Error::new(kind, "injected write failure"));
            }
            self.bytes.extend_from_slice(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            self.flush_count += 1;
            if let Some(kind) = self.fail_flush {
                return Err(io::Error::new(kind, "injected flush failure"));
            }
            Ok(())
        }
    }

    #[derive(Serialize)]
    struct Report {
        value: &'static str,
    }

    fn io_error_kind(error: &ReportOutputError) -> Option<ErrorKind> {
        match error {
            ReportOutputError::Write(error) | ReportOutputError::Flush(error) => Some(error.kind()),
            ReportOutputError::Render(_) | ReportOutputError::Serialize(_) => None,
        }
    }

    #[test]
    fn report_modes_keep_their_newline_and_json_shapes_and_flush() -> Result<()> {
        let report = Report { value: "ok" };
        let mut text = TestWriter::default();
        write_report(&report, OutputFormat::Text, &mut text, |report, out| {
            out.push_str(report.value);
            Ok(())
        })?;
        assert_eq!(text.bytes, b"ok\n");
        assert_eq!(text.flush_count, 1);

        let mut json = TestWriter::default();
        write_report(&report, OutputFormat::Json, &mut json, |_, _| Ok(()))?;
        assert_eq!(json.bytes, b"{\n  \"value\": \"ok\"\n}\n");
        assert_eq!(json.flush_count, 1);

        let mut ndjson = TestWriter::default();
        write_report(&report, OutputFormat::Ndjson, &mut ndjson, |_, _| Ok(()))?;
        assert_eq!(ndjson.bytes, b"{\"value\":\"ok\"}\n");
        assert_eq!(ndjson.flush_count, 1);
        Ok(())
    }

    #[test]
    fn report_propagates_write_and_final_flush_errors() {
        let report = Report { value: "ok" };
        let mut writer = TestWriter {
            fail_write: Some(ErrorKind::BrokenPipe),
            ..Default::default()
        };
        let error = write_report(&report, OutputFormat::Json, &mut writer, |_, _| Ok(()))
            .expect_err("writer error should propagate");
        assert_eq!(io_error_kind(&error), Some(ErrorKind::BrokenPipe));
        assert!(error.is_broken_pipe());

        let mut writer = TestWriter {
            fail_flush: Some(ErrorKind::Other),
            ..Default::default()
        };
        let error = write_report(&report, OutputFormat::Text, &mut writer, |_, out| {
            out.push_str("complete");
            Ok(())
        })
        .expect_err("flush error should propagate");
        assert_eq!(io_error_kind(&error), Some(ErrorKind::Other));
        assert!(!error.is_broken_pipe());
        assert_eq!(writer.bytes, b"complete\n");
    }

    #[test]
    fn render_error_propagates_before_any_output_is_written() {
        let report = Report { value: "unused" };
        let mut writer = TestWriter::default();
        let error = write_report(&report, OutputFormat::Text, &mut writer, |_, out| {
            out.push_str("partial");
            bail!("rendering failed")
        })
        .expect_err("render error should propagate");
        let ReportOutputError::Render(render_error) = &error else {
            panic!("render errors retain their typed category: {error:?}");
        };
        assert!(render_error.to_string().contains("rendering failed"));
        assert!(!error.is_broken_pipe());
        assert!(writer.bytes.is_empty());
        assert_eq!(writer.flush_count, 0);
    }

    #[test]
    fn serialization_error_propagates_without_writing() {
        struct FailingSerialize;

        impl Serialize for FailingSerialize {
            fn serialize<S>(&self, _serializer: S) -> std::result::Result<S::Ok, S::Error>
            where
                S: serde::Serializer,
            {
                Err(serde::ser::Error::custom("serialization failed"))
            }
        }

        let mut writer = TestWriter::default();
        let error = write_report(
            &FailingSerialize,
            OutputFormat::Json,
            &mut writer,
            |_, _| Ok(()),
        )
        .expect_err("serialization error should propagate");
        assert!(matches!(&error, ReportOutputError::Serialize(_)));
        assert!(!error.is_broken_pipe());
        assert!(writer.bytes.is_empty());
        assert_eq!(writer.flush_count, 0);
    }

    #[test]
    fn ndjson_list_propagates_write_and_flush_errors() {
        let items = [Report { value: "one" }, Report { value: "two" }];
        let mut writer = TestWriter {
            fail_write: Some(ErrorKind::Other),
            ..Default::default()
        };
        let error = write_ndjson_list(&items, OutputFormat::Ndjson, &mut writer)
            .expect_err("item write error should propagate");
        assert_eq!(io_error_kind(&error), Some(ErrorKind::Other));

        let mut writer = TestWriter {
            fail_flush: Some(ErrorKind::Other),
            ..Default::default()
        };
        let error = write_ndjson_list(&items, OutputFormat::Ndjson, &mut writer)
            .expect_err("list flush error should propagate");
        assert_eq!(io_error_kind(&error), Some(ErrorKind::Other));
        assert_eq!(writer.bytes, b"{\"value\":\"one\"}\n{\"value\":\"two\"}\n");
    }
}
