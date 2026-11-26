//! Typed projections of the shared provider interpreter, never an AST evaluator.

use crate::analysis_inputs::{
    ForeignInvocation, ForeignReturn, NativeIntrinsic, ProofDependencies,
};
use std::collections::BTreeSet;
use std::ops::Range;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DerivedForeignCall {
    pub invocation: ForeignInvocation,
    pub returns: BTreeSet<ForeignReturn>,
    pub retained_arguments: BTreeSet<usize>,
    pub retained_provider_captures: BTreeSet<String>,
    /// Every private provider var cell the body reads, retained or not.
    pub provider_cell_reads: BTreeSet<String>,
    pub native_constructions: BTreeSet<NativeIntrinsic>,
    pub(crate) dependencies: ProofDependencies,
    /// Set for a parameterless call that always returns one fresh record.
    pub(crate) record: Option<Vec<RecordMember>>,
    /// The source range of the closure literal every call returns a fresh
    /// instance of.
    pub(crate) closure: Option<Range<u32>>,
}

/// A method or getter of a returned record literal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RecordMember {
    pub(crate) name: String,
    pub(crate) function_start: u32,
}
