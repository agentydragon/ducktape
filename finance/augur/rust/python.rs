//! In-process Python bindings for the Augur Rust simulator.
//!
//! Callers supply the authoritative typed prepared run. Serialization is private to this
//! boundary; policy and reporting code do not read or construct wire documents.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod action_bindings;

use augur_rust_simulator::execution::ExecutionInput;

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

#[pymodule]
fn _simulator(module: &Bound<'_, PyModule>) -> PyResult<()> {
    action_bindings::register(module)?;
    Ok(())
}
