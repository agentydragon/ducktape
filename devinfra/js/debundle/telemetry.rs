//! Opt-in OpenTelemetry metrics for Debundle operations.
//!
//! A session owns its SDK meter provider, so embedding this library does not
//! change the process-wide OpenTelemetry provider. Pass `None` to disable
//! export. Successful periodic and final exports are written as one flushed
//! OTLP JSON request per line. If a write fails, `finish` reports the failure;
//! the file may retain partial bytes at its trailing record.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use opentelemetry::metrics::{Meter, MeterProvider};
use opentelemetry_proto::tonic::collector::metrics::v1::ExportMetricsServiceRequest;
use opentelemetry_sdk::error::{OTelSdkError, OTelSdkResult};
use opentelemetry_sdk::metrics::exporter::PushMetricExporter;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider, Temporality};
use opentelemetry_sdk::{Resource, metrics::data::ResourceMetrics};

/// Owns an optional meter provider for one top-level operation.
pub struct Session {
    provider: Option<SdkMeterProvider>,
    export_error: Option<ExportErrorState>,
}

type ExportErrorState = Arc<Mutex<Option<String>>>;

impl Session {
    /// Start metrics export when `path` is present; otherwise return a disabled
    /// session without constructing an SDK provider.
    ///
    /// The SDK reads `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_SERVICE_NAME`, and
    /// `OTEL_RESOURCE_ATTRIBUTES` using its standard environment handling.
    pub fn start(path: Option<&Path>) -> Result<Self> {
        let Some(path) = path else {
            return Ok(Self {
                provider: None,
                export_error: None,
            });
        };

        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .with_context(|| format!("opening Debundle metrics output {}", path.display()))?;

        let (provider, export_error) = provider_with_writer(file, None, None);
        Ok(Self {
            provider: Some(provider),
            export_error: Some(export_error),
        })
    }

    /// Return a meter tied to this session's provider, if metrics are enabled.
    pub fn meter(&self) -> Option<Meter> {
        self.provider
            .as_ref()
            .map(|provider| provider.meter("devinfra.js.debundle"))
    }

    /// Flush remaining measurements and stop the provider.
    ///
    /// SDK shutdown performs a final collection and export. Any collection,
    /// serialization, or output error is returned to the caller.
    pub fn finish(self) -> Result<()> {
        let shutdown_error = self.provider.and_then(|provider| provider.shutdown().err());
        let export_error = self
            .export_error
            .map(|state| {
                state
                    .lock()
                    .map(|error| error.clone())
                    .map_err(|_| anyhow::anyhow!("Debundle metrics error state was poisoned"))
            })
            .transpose()?
            .flatten();

        match (shutdown_error, export_error) {
            (None, None) => {}
            (Some(error), None) => {
                return Err(error).context("flushing and shutting down Debundle metrics");
            }
            (None, Some(error)) => {
                return Err(anyhow::anyhow!("Debundle metrics export failed: {error}"));
            }
            (Some(shutdown), Some(export)) => {
                return Err(anyhow::anyhow!(
                    "Debundle metrics export failed: {export}; SDK shutdown also failed: {shutdown}"
                ));
            }
        }
        Ok(())
    }
}

fn provider_with_writer<W>(
    writer: W,
    test_interval: Option<Duration>,
    resource: Option<Resource>,
) -> (SdkMeterProvider, ExportErrorState)
where
    W: Write + Send + 'static,
{
    let export_error = Arc::new(Mutex::new(None));
    let exporter = JsonLinesExporter::new(writer, Arc::clone(&export_error));
    let reader = match test_interval {
        Some(interval) => PeriodicReader::builder(exporter)
            .with_interval(interval)
            .build(),
        None => PeriodicReader::builder(exporter).build(),
    };

    let resource = resource.unwrap_or_else(|| Resource::builder().build());
    let provider = SdkMeterProvider::builder()
        .with_reader(reader)
        .with_resource(resource)
        .build();
    (provider, export_error)
}

struct JsonLinesExporter<W> {
    writer: Mutex<W>,
    first_error: ExportErrorState,
}

impl<W> JsonLinesExporter<W> {
    fn new(writer: W, first_error: ExportErrorState) -> Self {
        Self {
            writer: Mutex::new(writer),
            first_error,
        }
    }
}

impl<W> JsonLinesExporter<W>
where
    W: Write,
{
    fn write_metrics(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        let result = self.write_metrics_inner(metrics);
        self.remember_error(&result);
        result
    }

    fn write_metrics_inner(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        let request = ExportMetricsServiceRequest::from(metrics);
        // Serde writes many small tokens. Format one bounded-cardinality
        // record before touching the file, rather than making a syscall per
        // token on every periodic export.
        let mut line = serde_json::to_vec(&request)
            .map_err(|error| export_error("serializing OTLP metrics JSON", error))?;
        line.push(b'\n');
        let mut writer = self
            .writer
            .lock()
            .map_err(|error| OTelSdkError::InternalFailure(error.to_string()))?;

        writer
            .write_all(&line)
            .and_then(|()| writer.flush())
            .map_err(|error| export_error("writing OTLP metrics JSON line", error))
    }

    fn flush(&self) -> OTelSdkResult {
        let result = self.flush_inner();
        self.remember_error(&result);
        result
    }

    fn flush_inner(&self) -> OTelSdkResult {
        self.writer
            .lock()
            .map_err(|error| OTelSdkError::InternalFailure(error.to_string()))?
            .flush()
            .map_err(|error| export_error("flushing OTLP metrics output", error))
    }

    fn remember_error(&self, result: &OTelSdkResult) {
        if let Err(error) = result
            && let Ok(mut first_error) = self.first_error.lock()
        {
            first_error.get_or_insert_with(|| error.to_string());
        }
    }
}

impl<W> PushMetricExporter for JsonLinesExporter<W>
where
    W: Write + Send + 'static,
{
    async fn export(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        self.write_metrics(metrics)
    }

    fn force_flush(&self) -> OTelSdkResult {
        self.flush()
    }

    fn shutdown_with_timeout(&self, _timeout: Duration) -> OTelSdkResult {
        self.flush()
    }

    fn temporality(&self) -> Temporality {
        Temporality::Cumulative
    }
}

fn export_error(context: &str, error: impl std::fmt::Display) -> OTelSdkError {
    OTelSdkError::InternalFailure(format!("{context}: {error}"))
}

#[cfg(test)]
mod tests {
    use std::io;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Condvar, Mutex};
    use std::time::{Duration, Instant};

    use opentelemetry::{KeyValue, metrics::Meter};
    use opentelemetry_sdk::Resource;

    use super::{Session, provider_with_writer};

    #[derive(Clone, Default)]
    struct SharedBuffer(Arc<(Mutex<Vec<u8>>, Condvar)>);

    impl SharedBuffer {
        fn bytes(&self) -> Vec<u8> {
            self.0.0.lock().expect("buffer lock").clone()
        }

        fn wait_for_line(&self, timeout: Duration) -> bool {
            let deadline = Instant::now() + timeout;
            let mut bytes = self.0.0.lock().expect("buffer lock");
            while !bytes.contains(&b'\n') {
                let now = Instant::now();
                if now >= deadline {
                    return false;
                }
                let remaining = deadline.saturating_duration_since(now);
                let (next_bytes, wait) = self
                    .0
                    .1
                    .wait_timeout(bytes, remaining)
                    .expect("buffer lock");
                bytes = next_bytes;
                if wait.timed_out() && !bytes.contains(&b'\n') {
                    return false;
                }
            }
            true
        }

        fn first_request(&self) -> serde_json::Value {
            let bytes = self.bytes();
            let line = std::str::from_utf8(&bytes)
                .expect("JSON is UTF-8")
                .lines()
                .next()
                .expect("one exported request");
            serde_json::from_str(line).expect("valid OTLP JSON request")
        }
    }

    impl io::Write for SharedBuffer {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0
                .0
                .lock()
                .expect("buffer lock")
                .extend_from_slice(bytes);
            self.0.1.notify_all();
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    struct FailedWriter;

    impl io::Write for FailedWriter {
        fn write(&mut self, _bytes: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("injected writer failure"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("injected flush failure"))
        }
    }

    struct FailFirstWrite {
        first_write_failed: Arc<AtomicBool>,
        output: SharedBuffer,
    }

    impl io::Write for FailFirstWrite {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if !self.first_write_failed.swap(true, Ordering::SeqCst) {
                return Err(io::Error::other("injected first-write failure"));
            }
            self.output.write(bytes)
        }

        fn flush(&mut self) -> io::Result<()> {
            self.output.flush()
        }
    }

    fn counter(meter: Meter) -> opentelemetry::metrics::Counter<u64> {
        meter.u64_counter("debundle.test.operations").build()
    }

    fn record_one(session: &Session) {
        counter(session.meter().expect("metrics enabled")).add(1, &[]);
    }

    fn session_with_writer<W>(
        writer: W,
        interval: Option<Duration>,
        resource: Option<Resource>,
    ) -> Session
    where
        W: io::Write + Send + 'static,
    {
        let (provider, export_error) = provider_with_writer(writer, interval, resource);
        Session {
            provider: Some(provider),
            export_error: Some(export_error),
        }
    }

    fn resource_value<'a>(request: &'a serde_json::Value, key: &str) -> &'a serde_json::Value {
        request["resourceMetrics"][0]["resource"]["attributes"]
            .as_array()
            .expect("OTLP resource attributes")
            .iter()
            .find(|attribute| attribute["key"] == key)
            .unwrap_or_else(|| panic!("missing resource attribute {key}"))
            .get("value")
            .expect("OTLP AnyValue")
    }

    #[test]
    fn no_path_disables_metrics_without_a_provider() {
        let session = Session::start(None).expect("disabled session");
        assert!(session.meter().is_none());
        session.finish().expect("disabled finish");
    }

    #[test]
    fn periodic_reader_exports_while_operation_is_active() {
        let output = SharedBuffer::default();
        let session = session_with_writer(output.clone(), Some(Duration::from_millis(20)), None);
        record_one(&session);

        assert!(
            output.wait_for_line(Duration::from_secs(2)),
            "periodic collection did not write a completed OTLP JSON line while active"
        );
        let request = output.first_request();
        assert_eq!(
            request["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["name"],
            "debundle.test.operations"
        );

        session.finish().expect("final shutdown");
    }

    #[test]
    fn shutdown_writes_official_otlp_json_with_resource_attributes() {
        let output = SharedBuffer::default();
        let resource = Resource::builder_empty()
            .with_attributes([
                KeyValue::new("service.name", "debundle-tests"),
                KeyValue::new("deployment.environment.name", "test"),
            ])
            .build();
        let session = session_with_writer(output.clone(), None, Some(resource));
        record_one(&session);

        session
            .finish()
            .expect("shutdown exports final measurement");

        let request = output.first_request();
        assert_eq!(
            resource_value(&request, "service.name")["stringValue"],
            "debundle-tests"
        );
        assert_eq!(
            resource_value(&request, "deployment.environment.name")["stringValue"],
            "test"
        );
        let metric = &request["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0];
        assert_eq!(metric["name"], "debundle.test.operations");
        assert!(
            metric.get("sum").is_some(),
            "OTLP sum oneof field is present"
        );
        assert_eq!(metric["sum"]["dataPoints"][0]["asInt"], "1");
    }

    #[test]
    fn finish_propagates_export_and_flush_errors() {
        let session = session_with_writer(FailedWriter, None, None);
        record_one(&session);

        assert!(session.finish().is_err());
    }

    #[test]
    fn finish_reports_an_earlier_periodic_export_failure() {
        let output = SharedBuffer::default();
        let first_write_failed = Arc::new(AtomicBool::new(false));
        let writer = FailFirstWrite {
            first_write_failed: Arc::clone(&first_write_failed),
            output: output.clone(),
        };
        let session = session_with_writer(writer, Some(Duration::from_millis(100)), None);
        record_one(&session);

        let deadline = Instant::now() + Duration::from_secs(2);
        while !first_write_failed.load(Ordering::SeqCst) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(
            first_write_failed.load(Ordering::SeqCst),
            "no periodic export attempted a write"
        );

        let error = session.finish().expect_err("earlier periodic failure");
        assert!(format!("{error:#}").contains("injected first-write failure"));
        assert!(output.wait_for_line(Duration::from_secs(1)));
    }

    #[test]
    fn configured_path_opens_a_line_oriented_output_file() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let path = temp.path().join("metrics.jsonl");
        let session = Session::start(Some(&path)).expect("enabled session");
        record_one(&session);
        session
            .finish()
            .expect("shutdown exports final measurement");

        let contents = std::fs::read_to_string(path).expect("metrics file");
        assert_eq!(contents.lines().count(), 1);
        assert!(contents.ends_with('\n'));
        let request: serde_json::Value =
            serde_json::from_str(contents.lines().next().expect("one request"))
                .expect("valid OTLP JSON request");
        assert!(request["resourceMetrics"].is_array());
    }
}
