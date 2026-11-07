//! Typed projections of the shared provider interpreter, never an AST evaluator.

use super::builtins::{self, Invocation, Row};
use crate::analysis_inputs::{
    ForeignInvocation, ForeignNativeOperation, ForeignReturn, ProofDependencies,
};
use crate::analysis_provenance::InferenceRule;
use crate::facts::sync_effects::EffectCoverageFragment;
use std::collections::BTreeSet;
use std::ops::Range;

pub(crate) struct DerivedConditionalBody {
    pub(crate) input: crate::analysis_inputs::RecordInputShape,
    pub(crate) reads: BTreeSet<crate::analysis_inputs::ParameterHeapRead>,
    pub(crate) module_cells: crate::analysis_inputs::ModuleCellAccesses,
    pub(crate) native_operations: BTreeSet<ForeignNativeOperation>,
    pub(crate) effects: EffectCoverageFragment,
    pub(crate) dependencies: ProofDependencies,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DerivedForeignCall {
    pub(crate) invocation: ForeignInvocation,
    pub(crate) returns: BTreeSet<ForeignReturn>,
    pub(crate) retained_arguments: BTreeSet<usize>,
    pub(crate) retained_provider_captures: BTreeSet<String>,
    pub(crate) native_operations: BTreeSet<ForeignNativeOperation>,
    /// Consumed semantic derivations, independent of row identity requirements.
    pub(crate) native_derivation_rules: BTreeSet<InferenceRule>,
    pub(crate) dependencies: ProofDependencies,
    /// Actual body effects from the shared local interpreter. Allocation-only
    /// judgments and effect coverage remain independent evidence.
    pub(crate) body_effects: Option<EffectCoverageFragment>,
    /// Set for a parameterless call that always returns one fresh record.
    pub(crate) record: Option<Vec<RecordMember>>,
    /// The source range of the closure literal every call returns a fresh
    /// instance of.
    pub(crate) closure: Option<Range<u32>>,
}

impl ForeignNativeOperation {
    pub(crate) fn for_row(row: &Row, invocation: Invocation) -> Option<Self> {
        if row.requires.is_empty() || !row.invocation.contains(&invocation) {
            return None;
        }
        Some(Self {
            invocation: match invocation {
                Invocation::Call => ForeignInvocation::Call,
                Invocation::Construct => ForeignInvocation::Construct,
                Invocation::Tag => return None,
            },
            intrinsic: row.family()?,
            property: row.op.callee().1.map(str::to_owned),
        })
    }

    pub(crate) fn row(&self) -> Option<&'static Row> {
        let invocation = match self.invocation {
            ForeignInvocation::Call => Invocation::Call,
            ForeignInvocation::Construct => Invocation::Construct,
        };
        builtins::lookup(
            self.intrinsic.global_binding()?,
            self.property.as_deref(),
            invocation,
        )
        .filter(|row| row.family() == Some(self.intrinsic) && !row.requires.is_empty())
    }
}

/// A method or getter of a returned record literal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RecordMember {
    pub(crate) name: String,
    pub(crate) function_start: u32,
}
