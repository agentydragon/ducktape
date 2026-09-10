//! In-process Python bindings for the Augur Rust simulator.
//!
//! Callers supply the authoritative typed prepared run. Serialization is private to this
//! boundary; policy and reporting code do not read or construct wire documents.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod action_bindings;

use augur_rust_simulator::execution::ExecutionInput;
use augur_rust_simulator::product::BASE_METRIC_NAMES;

fn parse(run: &Bound<'_, PyAny>) -> PyResult<ExecutionInput> {
    let encoded: String = run
        .py()
        .import("finance.augur.rust.prepared")?
        .getattr("_encode_native")?
        .call1((run,))?
        .extract()?;
    serde_json::from_str(&encoded)
        .map_err(|error| PyValueError::new_err(format!("invalid prepared input: {error}")))
}

fn to_py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

/// The seven base product metric series for one population.
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
pub struct ProductMetrics {
    #[pyo3(get)]
    rollout_count: u32,
    #[pyo3(get)]
    snapshot_count: u32,
    /// `BASE_METRIC_NAMES` order; each entry is a flat row-major `[snapshot][rollout]` block.
    #[pyo3(get)]
    base_series: Vec<Vec<i64>>,
    #[pyo3(get)]
    failed_month: Vec<i64>,
    #[pyo3(get)]
    metric_names: Vec<String>,
}

impl From<augur_rust_simulator::product::ProductMetricSeries> for ProductMetrics {
    fn from(series: augur_rust_simulator::product::ProductMetricSeries) -> Self {
        Self {
            rollout_count: series.rollout_count,
            snapshot_count: series.snapshot_count,
            base_series: series.base_series,
            failed_month: series.failed_month,
            metric_names: BASE_METRIC_NAMES.iter().map(|&name| name.into()).collect(),
        }
    }
}

#[pymodule]
fn _simulator(module: &Bound<'_, PyModule>) -> PyResult<()> {
    action_bindings::register(module)?;
    module.add_class::<ProductMetrics>()?;
    Ok(())
}
