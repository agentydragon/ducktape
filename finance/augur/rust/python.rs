//! In-process Python bindings for the Augur Rust simulator.
//!
//! Callers supply the authoritative typed prepared run. Serialization is private to this
//! boundary; policy and reporting code do not read or construct wire documents.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod action_bindings;

use augur_rust_simulator::engine::{
    ValidatedInput, simulate_dense_validated, simulate_product_metrics_validated,
    simulate_validated,
};
use augur_rust_simulator::event_frames::FramedOutput;
use augur_rust_simulator::execution::ExecutionInput;
use augur_rust_simulator::product::BASE_METRIC_NAMES;

fn parse(run: &Bound<'_, PyAny>) -> PyResult<ExecutionInput> {
    let encoded: String = run
        .py()
        .import("finance.augur.rust.prepared")?
        .getattr("_encode")?
        .call1((run,))?
        .extract()?;
    serde_json::from_str(&encoded)
        .map_err(|error| PyValueError::new_err(format!("invalid prepared input: {error}")))
}

fn to_py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

/// The seven base product metric series for one population.
#[pyclass(frozen, module = "finance.augur.rust.simulator")]
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

/// Run every rollout and return only the base product metric series.
#[pyfunction]
fn simulate_product_metrics(
    run: &Bound<'_, PyAny>,
    primary_agent_id: &str,
) -> PyResult<ProductMetrics> {
    let fixture = parse(run)?;
    let series = Python::attach(|py| {
        py.detach(|| {
            let validated = ValidatedInput::new(&fixture)?;
            simulate_product_metrics_validated(validated, primary_agent_id)
        })
    })
    .map_err(to_py_err)?;
    Ok(ProductMetrics {
        rollout_count: series.rollout_count,
        snapshot_count: series.snapshot_count,
        base_series: series.base_series,
        failed_month: series.failed_month,
        metric_names: BASE_METRIC_NAMES.iter().map(|&name| name.into()).collect(),
    })
}

/// Run every rollout with dense monthly state and the canonical event frames.
///
/// This is the trace path: everything `simulate_forensic_json` carries except the balanced
/// journal, which is Rust's own double-entry invariant and has no reader on the Python side.
#[pyfunction]
fn simulate_dense_json(run: &Bound<'_, PyAny>) -> PyResult<String> {
    let fixture = parse(run)?;
    let output = Python::attach(|py| {
        py.detach(|| {
            let validated = ValidatedInput::new(&fixture)?;
            simulate_dense_validated(validated)
        })
    })
    .map_err(to_py_err)?;
    serde_json::to_string(&FramedOutput::new(&output)).map_err(to_py_err)
}

/// Run every rollout retaining dense state, the balanced journal, and the event frames.
///
/// The journal is the double-entry invariant made checkable: every entry's signed postings
/// sum to zero. No canonical channel carries it, which is why `simulate_dense_json` leaves
/// it out.
#[pyfunction]
fn simulate_forensic_json(run: &Bound<'_, PyAny>) -> PyResult<String> {
    let fixture = parse(run)?;
    let output = Python::attach(|py| {
        py.detach(|| {
            let validated = ValidatedInput::new(&fixture)?;
            simulate_validated(validated)
        })
    })
    .map_err(to_py_err)?;
    serde_json::to_string(&FramedOutput::new(&output)).map_err(to_py_err)
}

#[pymodule]
fn simulator(module: &Bound<'_, PyModule>) -> PyResult<()> {
    action_bindings::register(module)?;
    module.add_class::<ProductMetrics>()?;
    module.add_function(wrap_pyfunction!(simulate_product_metrics, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_dense_json, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_forensic_json, module)?)?;
    Ok(())
}
