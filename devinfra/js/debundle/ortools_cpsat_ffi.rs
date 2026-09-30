//! OR-Tools' CP-SAT C API (`ortools/sat/c_api/cp_solver_c.h`), linked into this
//! process. The only module with `unsafe`; everything else talks to CP-SAT
//! through [`solve`].
//!
//! Hazards the process shares with the solver: CP-SAT `CHECK`-aborts on its own
//! internal failures, and a solve that exhausts memory terminates the whole
//! process (`std::bad_alloc` is not caught).

use std::error::Error;
use std::ffi::{c_int, c_void};
use std::fmt;
use std::panic;
use std::ptr::{self, NonNull};
use std::slice;
use std::thread;

use cp_model_proto::operations_research::sat::{CpModelProto, CpSolverResponse};
use prost::Message;
use sat_parameters_proto::operations_research::sat::SatParameters;

/// The stack a solve runs on, as large as the main thread's under the default
/// `ulimit -s`; a caller's own thread (a rayon worker's is 2 MiB) is not known
/// to be.
const SOLVER_STACK_BYTES: usize = 8 << 20;

unsafe extern "C" {
    fn SolveCpModelWithParameters(
        creq: *const c_void,
        creq_len: c_int,
        cparams: *const c_void,
        cparams_len: c_int,
        cres: *mut *mut c_void,
        cres_len: *mut c_int,
    );

    /// `free(3)`: the C API hands the response over as a `malloc`ed buffer
    /// (`strings::memdup`, `ortools/base/memutil.h`).
    fn free(ptr: *mut c_void);
}

#[derive(Debug)]
pub enum SolveError {
    /// A serialized message is longer than the C API's `int` length.
    MessageTooLarge {
        bytes: usize,
    },
    Response(prost::DecodeError),
}

impl fmt::Display for SolveError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MessageTooLarge { bytes } => write!(
                f,
                "CP-SAT message of {bytes} bytes exceeds the C API's int length"
            ),
            Self::Response(err) => write!(f, "failed to decode CP-SAT response: {err}"),
        }
    }
}

impl Error for SolveError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::MessageTooLarge { .. } => None,
            Self::Response(err) => Some(err),
        }
    }
}

/// A response buffer the C API allocated, freed once on drop.
struct MallocBuffer {
    data: NonNull<u8>,
    len: usize,
}

impl MallocBuffer {
    fn as_slice(&self) -> &[u8] {
        // SAFETY: the constructor's contract: `data` points to `len` bytes
        // that `self` owns.
        unsafe { slice::from_raw_parts(self.data.as_ptr(), self.len) }
    }
}

impl Drop for MallocBuffer {
    fn drop(&mut self) {
        // SAFETY: `data` came from `malloc` in the C API and is freed only here.
        unsafe { free(self.data.as_ptr().cast()) }
    }
}

fn c_len(bytes: &[u8]) -> Result<c_int, SolveError> {
    c_int::try_from(bytes.len()).map_err(|_| SolveError::MessageTooLarge { bytes: bytes.len() })
}

/// Solves the serialized `request` under the serialized `parameters`.
fn solve_serialized(request: &[u8], parameters: &[u8]) -> Result<CpSolverResponse, SolveError> {
    let (request_len, parameters_len) = (c_len(request)?, c_len(parameters)?);
    let mut response: *mut c_void = ptr::null_mut();
    let mut response_len: c_int = 0;
    // SAFETY: `request` and `parameters` are live for the call and hold exactly
    // the lengths passed; the call only reads them. It parses both as the protos
    // they were encoded from (it aborts on bytes that do not parse, which
    // `Message::encode_to_vec` cannot produce), builds its own `Model` and keeps
    // no state after returning, then stores a `malloc`ed serialized response of
    // `response_len` bytes through `response` (it aborts rather than return a
    // null buffer) and keeps no pointer into any of these.
    unsafe {
        SolveCpModelWithParameters(
            request.as_ptr().cast(),
            request_len,
            parameters.as_ptr().cast(),
            parameters_len,
            &mut response,
            &mut response_len,
        );
    }
    let response = MallocBuffer {
        data: NonNull::new(response.cast()).expect("CP-SAT returned a null response buffer"),
        len: usize::try_from(response_len).expect("CP-SAT returned a negative length"),
    };
    CpSolverResponse::decode(response.as_slice()).map_err(SolveError::Response)
}

/// Solves `model` under `parameters`; blocks until the solve returns.
///
/// Concurrent calls are independent: each gets its own CP-SAT `Model`, and the
/// C API keeps no other state.
pub fn solve(
    model: &CpModelProto,
    parameters: &SatParameters,
) -> Result<CpSolverResponse, SolveError> {
    let request = model.encode_to_vec();
    let parameters = parameters.encode_to_vec();
    thread::scope(|scope| {
        thread::Builder::new()
            .stack_size(SOLVER_STACK_BYTES)
            .spawn_scoped(scope, || solve_serialized(&request, &parameters))
            .expect("spawn CP-SAT solver thread")
            .join()
            .unwrap_or_else(|panic| panic::resume_unwind(panic))
    })
}

#[cfg(test)]
mod tests {
    use cp_model_proto::operations_research::sat::constraint_proto::Constraint;
    use cp_model_proto::operations_research::sat::{
        ConstraintProto, CpSolverStatus, IntegerVariableProto, LinearExpressionProto,
        TableConstraintProto,
    };

    use super::*;

    fn model_with_one_variable(domain: Vec<i64>, allowed_values: Vec<i64>) -> CpModelProto {
        CpModelProto {
            variables: vec![IntegerVariableProto {
                domain,
                ..Default::default()
            }],
            constraints: vec![ConstraintProto {
                constraint: Some(Constraint::Table(TableConstraintProto {
                    exprs: vec![LinearExpressionProto {
                        vars: vec![0],
                        coeffs: vec![1],
                        offset: 0,
                    }],
                    values: allowed_values,
                    negated: false,
                    ..Default::default()
                })),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    // Library spike: the working C API wiring (request and response bytes, the
    // freed buffer) pinned end to end.
    #[test]
    fn solves_a_table_constrained_variable() {
        let response = solve(
            &model_with_one_variable(vec![0, 9], vec![7]),
            &SatParameters::default(),
        )
        .unwrap();

        assert_eq!(response.status(), CpSolverStatus::Optimal);
        assert_eq!(response.solution, [7]);
    }

    // Library spike: an invalid model comes back as `MODEL_INVALID` with the
    // reason in `solution_info`; the C API does not abort on it.
    #[test]
    fn reports_an_invalid_model_instead_of_aborting() {
        let response = solve(
            &model_with_one_variable(vec![3], vec![3]),
            &SatParameters::default(),
        )
        .unwrap();

        assert_eq!(response.status(), CpSolverStatus::ModelInvalid);
        assert!(!response.solution_info.is_empty());
    }
}
